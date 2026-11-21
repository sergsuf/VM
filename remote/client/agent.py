"""Client Agent runtime executing the 7-stage 3ds Max deployment pipeline."""

from __future__ import annotations

from collections import deque
import json
import os
from pathlib import Path
import re
import socket
import threading
import time
from typing import Any, Callable
import uuid

from remote import RUNTIME_VERSION, __version__ as PACKAGE_VERSION
from remote.auth import PairingConfig
from remote.drivers.base import DriverBundle
from remote.drivers.simulated import create_simulated_bundle
from remote.iso_transfer import (
    IsoTransferClient,
    TransferCancelledError,
    TransferPausedError,
)
from remote.protocol import (
    Command,
    CommandAck,
    CommandType,
    IsoSpec,
    LogEntry,
    SessionState,
    Stage,
    StageStatus,
    SystemStatus,
    TransferProgress,
    UpdateManifest,
    utc_now_iso,
)
from remote.state_machine import DeploymentStateMachine, InvalidTransitionError
from remote.transport import (
    DirectTransport,
    TransportAuthError,
    TransportError,
    create_transport,
)
from remote.updater import (
    UPDATE_RESTART_EXIT_CODE,
    AppUpdater,
    atomic_write_json,
    mark_healthy_boot,
)

_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


def _validate_safe_version(version: str) -> str:
    if not isinstance(version, str) or not _SAFE_VERSION_RE.match(version) or ".." in version:
        raise ValueError(f"Unsafe target version: {version!r}")
    return version


def _validate_safe_filename(filename: str) -> str:
    if (
        not isinstance(filename, str)
        or not filename
        or "\x00" in filename
        or "/" in filename
        or "\\" in filename
        or re.match(r"^[A-Za-z]:", filename)
        or filename in (".", "..")
        or ".." in filename
        or Path(filename).name != filename
    ):
        raise ValueError(f"Unsafe filename: {filename!r}")
    return filename


def _default_data_dir() -> Path:
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "MaxAgent" / "data"
    return Path.cwd() / "data" / "client"


def _default_iso_dir() -> Path:
    if os.name == "nt":
        return Path("C:/Install")
    return _default_data_dir() / "isos"


def _resolve_versions(app_root: Path | None) -> tuple[str, str]:
    agent_ver = PACKAGE_VERSION
    runtime_ver = RUNTIME_VERSION

    release_dir_env = os.environ.get("MAX_APP_RELEASE_DIR")
    candidates: list[Path] = []
    if release_dir_env:
        candidates.append(Path(release_dir_env) / "VERSION.txt")
    candidates.append(Path(__file__).resolve().parents[2] / "VERSION.txt")
    for p in candidates:
        if p.is_file():
            txt = p.read_text(encoding="utf-8").strip()
            if txt:
                agent_ver = txt
                break

    if app_root is not None:
        active_file = app_root / "app" / "active.json"
        if active_file.is_file():
            try:
                data = json.loads(active_file.read_text(encoding="utf-8"))
                if data.get("version") and not release_dir_env:
                    agent_ver = str(data["version"])
            except Exception:
                pass
        rt_file = app_root / "runtime" / "version.json"
        if rt_file.is_file():
            try:
                rt_data = json.loads(rt_file.read_text(encoding="utf-8"))
                if rt_data.get("runtime_version"):
                    runtime_ver = str(rt_data["runtime_version"])
            except Exception:
                pass

    return agent_ver, runtime_ver


def _load_or_create_client_id(data_dir: Path, explicit: str | None) -> str:
    if explicit:
        return explicit
    cid_file = data_dir / "client_id.txt"
    if cid_file.is_file():
        stored = cid_file.read_text(encoding="utf-8").strip()
        if stored:
            return stored
    host = socket.gethostname() or "client"
    cid = f"{host}-{uuid.uuid4().hex[:6]}"
    cid_file.write_text(cid, encoding="utf-8")
    return cid


class ClientAgent:
    """Autonomous client deployment agent with persistent state machine."""

    def __init__(
        self,
        pairing: PairingConfig | None = None,
        drivers: DriverBundle | None = None,
        data_dir: Path | str | None = None,
        iso_dir: Path | str | None = None,
        assets_dir: Path | str | None = None,
        client_id: str | None = None,
        transport: DirectTransport | None = None,
        heartbeat_interval: float = 5.0,
        poll_wait: int = 25,
        auto_resume_download: bool = True,
        app_root: Path | str | None = None,
        on_update_restart: Callable[[int], None] | None = None,
        driver_bundle: DriverBundle | None = None,
    ) -> None:
        self.data_dir = Path(data_dir) if data_dir is not None else _default_data_dir()
        self.data_dir.mkdir(parents=True, exist_ok=True)

        self.iso_dir = Path(iso_dir) if iso_dir is not None else _default_iso_dir()
        self.iso_dir.mkdir(parents=True, exist_ok=True)

        self.assets_dir = Path(assets_dir) if assets_dir is not None else None
        self.client_id = _load_or_create_client_id(self.data_dir, client_id)

        env_app_root = os.environ.get("MAX_APP_ROOT")
        self.app_root: Path | None = (
            Path(app_root)
            if app_root is not None
            else (Path(env_app_root) if env_app_root else None)
        )
        self._agent_version, self._runtime_version = _resolve_versions(self.app_root)

        self.drivers: DriverBundle = (
            drivers
            or driver_bundle
            or create_simulated_bundle(work_dir=self.data_dir / "sim")
        )
        self.heartbeat_interval = max(0.2, float(heartbeat_interval))
        self.poll_wait = max(1, int(poll_wait))
        self.auto_resume_download = bool(auto_resume_download)
        self._on_update_restart = on_update_restart

        self._pairing: PairingConfig | None = pairing
        self._transport: DirectTransport | None = transport
        if self._transport is None and self._pairing is not None:
            self._transport = create_transport(self._pairing, client_id=self.client_id)

        state_path = self.data_dir / "session_state.json"
        installer_running = False
        max_exists = False
        try:
            installer_running = self.drivers.installer.is_installer_running()
        except Exception:
            pass
        if state_path.is_file():
            try:
                raw_state = json.loads(state_path.read_text(encoding="utf-8"))
                tver = raw_state.get("target_version")
                if tver:
                    max_exists = self.drivers.installer.is_max_installed(str(tver))
            except Exception:
                pass

        session_id = self._pairing.session_id if self._pairing else ""
        self.sm = DeploymentStateMachine.load_or_create(
            state_path,
            session_id=session_id,
            client_id=self.client_id,
            installer_still_running=installer_running,
            max_exe_exists=max_exists,
            auto_resume_download=self.auto_resume_download,
        )

        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()
        self._cancel_event = threading.Event()
        self._worker_lock = threading.Lock()
        self._worker_thread: threading.Thread | None = None

        self._pending_logs: deque[LogEntry] = deque(maxlen=2000)
        self._recent_logs: deque[dict[str, Any]] = deque(maxlen=200)
        self._log_lock = threading.Lock()

        self._connected = False
        self._registered = False
        self._healthy_boot_marked = False
        self._seq_path = self.data_dir / "agent_seq.json"
        self._last_seq = 0
        self._processed_cmd_ids: deque[str] = deque(maxlen=500)
        self._load_seq_state()
        self._exit_code = 0
        self._started = False

        self._system_status: SystemStatus = self._collect_status_safe()
        self._threads: list[threading.Thread] = []

    @property
    def exit_code(self) -> int:
        return self._exit_code

    @property
    def state(self) -> SessionState:
        return self.sm.state

    @property
    def system_status(self) -> SystemStatus:
        with self._lock:
            return self._system_status

    @property
    def recent_logs(self) -> list[dict[str, Any]]:
        with self._log_lock:
            return list(self._recent_logs)

    @property
    def connected(self) -> bool:
        with self._lock:
            return self._connected

    @property
    def pairing(self) -> PairingConfig | None:
        with self._lock:
            return self._pairing

    def set_pairing(self, pairing: PairingConfig) -> None:
        with self._lock:
            self._pairing = pairing
            self._transport = create_transport(pairing, client_id=self.client_id)
            self._registered = False
            self._connected = False
        self._log("INFO", f"Updated pairing to {pairing.mode} ({pairing.server_url})")
        self._register_and_sync_once()

    def _collect_status_safe(self) -> SystemStatus:
        try:
            st = self.drivers.system.collect_status(
                self._agent_version, self._runtime_version
            )
            with self._lock:
                self._system_status = st
            return st
        except Exception as exc:
            self._log("WARN", f"Failed to collect system status: {exc}")
            with self._lock:
                return getattr(
                    self,
                    "_system_status",
                    SystemStatus(
                        hostname=socket.gethostname() or "client",
                        os_version="unknown",
                        is_admin=False,
                        session_id=1,
                        agent_version=self._agent_version,
                        runtime_version=self._runtime_version,
                    ),
                )

    def _log(self, level: str, msg: str, stage: str | None = None) -> None:
        lvl = level.upper()
        if lvl not in {"DEBUG", "INFO", "WARN", "ERROR"}:
            lvl = "INFO"
        stg = stage or self.sm.state.stage.value
        entry = LogEntry(ts=utc_now_iso(), level=lvl, stage=stg, msg=str(msg))
        with self._log_lock:
            self._pending_logs.append(entry)
            self._recent_logs.append(entry.to_dict())

    def _get_transport(self) -> DirectTransport | None:
        with self._lock:
            return self._transport

    def _post_state_safe(self) -> None:
        tr = self._get_transport()
        if tr is None:
            return
        try:
            tr.post_state(self.sm.state)
            with self._lock:
                self._connected = True
        except TransportError:
            pass

    def _register_and_sync_once(self) -> bool:
        tr = self._get_transport()
        if tr is None:
            return False
        try:
            status = self._collect_status_safe()
            tr.register(
                {
                    "client_id": self.client_id,
                    "system_status": status.to_dict(),
                    "state": self.sm.state.to_dict(),
                }
            )
            tr.heartbeat(status)
            tr.post_state(self.sm.state)
            with self._lock:
                self._registered = True
                self._connected = True
            self._maybe_mark_healthy_boot()
            return True
        except TransportError as exc:
            with self._lock:
                self._connected = False
            self._log("WARN", f"Operator registration/sync failed: {exc}")
            return False

    def _maybe_mark_healthy_boot(self) -> None:
        if self._healthy_boot_marked or self.app_root is None:
            return
        app_dir = self.app_root / "app"
        active_path = app_dir / "active.json"
        if not active_path.is_file():
            return
        try:
            data = json.loads(active_path.read_text(encoding="utf-8"))
            if data.get("pending_verify"):
                mark_healthy_boot(app_dir)
                self._log("INFO", "Marked active app release as healthy boot.")
            self._healthy_boot_marked = True
        except Exception as exc:
            self._log("WARN", f"Could not mark healthy boot: {exc}")

    def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._stop_event.clear()
        self._log("INFO", f"Starting ClientAgent {self.client_id} (v{self._agent_version})")
        self._register_and_sync_once()
        # Even when offline / unpaired, clear pending_verify if active.json exists
        if self.app_root is not None and not self._healthy_boot_marked:
            self._maybe_mark_healthy_boot()

        self._threads = [
            threading.Thread(target=self._heartbeat_loop, name="agent-heartbeat", daemon=True),
            threading.Thread(target=self._log_flush_loop, name="agent-logs", daemon=True),
            threading.Thread(target=self._command_poll_loop, name="agent-poll", daemon=True),
        ]
        for t in self._threads:
            t.start()

        # Auto-resume download if state was restored as DOWNLOAD_ISO/running
        st = self.sm.state
        if (
            self.auto_resume_download
            and st.stage == Stage.DOWNLOAD_ISO
            and st.status == StageStatus.RUNNING
            and st.iso_spec is not None
        ):
            self._log("INFO", "Auto-resuming interrupted ISO download.")
            self._launch_worker(self._run_download_and_install)

    def stop(self, timeout: float = 5.0) -> None:
        self._stop_event.set()
        self._cancel_event.set()
        self._flush_logs_once()
        with self._worker_lock:
            wt = self._worker_thread
        if wt is not None and wt.is_alive() and wt is not threading.current_thread():
            wt.join(timeout=timeout)
        for t in self._threads:
            if t.is_alive() and t is not threading.current_thread():
                t.join(timeout=min(timeout, 2.0))
        self._started = False

    def wait(self, timeout: float | None = None) -> bool:
        return self._stop_event.wait(timeout=timeout)

    def run_forever(self) -> int:
        self.start()
        try:
            while not self._stop_event.is_set():
                self._stop_event.wait(timeout=0.5)
        except KeyboardInterrupt:
            pass
        finally:
            self.stop()
        return self._exit_code

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------
    def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            tr = self._get_transport()
            if tr is not None:
                if not self._registered:
                    self._register_and_sync_once()
                else:
                    try:
                        status = self._collect_status_safe()
                        tr.heartbeat(status)
                        with self._lock:
                            self._connected = True
                    except TransportError:
                        with self._lock:
                            self._connected = False
            self._stop_event.wait(timeout=self.heartbeat_interval)

    def _flush_logs_once(self) -> None:
        tr = self._get_transport()
        if tr is None:
            return
        with self._log_lock:
            if not self._pending_logs:
                return
            batch = list(self._pending_logs)
            self._pending_logs.clear()
        try:
            tr.post_logs(batch)
        except TransportError:
            with self._log_lock:
                for item in reversed(batch):
                    if len(self._pending_logs) < self._pending_logs.maxlen:
                        self._pending_logs.appendleft(item)

    def _log_flush_loop(self) -> None:
        while not self._stop_event.is_set():
            self._flush_logs_once()
            self._stop_event.wait(timeout=0.5)

    def _load_seq_state(self) -> None:
        if not self._seq_path.is_file():
            return
        try:
            raw = json.loads(self._seq_path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._last_seq = max(0, int(raw.get("last_seq") or 0))
                ids = raw.get("processed_cmd_ids")
                if isinstance(ids, list):
                    for cid in ids[-500:]:
                        if isinstance(cid, str) and cid:
                            self._processed_cmd_ids.append(cid)
        except Exception:
            pass

    def _save_seq_state(self) -> None:
        try:
            atomic_write_json(
                self._seq_path,
                {
                    "last_seq": self._last_seq,
                    "processed_cmd_ids": list(self._processed_cmd_ids),
                },
            )
        except Exception:
            pass

    def _command_poll_loop(self) -> None:
        while not self._stop_event.is_set():
            tr = self._get_transport()
            if tr is None:
                self._stop_event.wait(timeout=1.0)
                continue
            if not self._registered:
                self._register_and_sync_once()
            try:
                cmds = tr.poll_commands(after_seq=self._last_seq, wait=self.poll_wait)
                with self._lock:
                    self._connected = True
                for cmd in cmds:
                    if cmd.cmd_id and cmd.cmd_id in self._processed_cmd_ids:
                        if cmd.seq > self._last_seq:
                            self._last_seq = cmd.seq
                            self._save_seq_state()
                        continue
                    if cmd.cmd_id:
                        self._processed_cmd_ids.append(cmd.cmd_id)
                    self._last_seq = cmd.seq
                    self._save_seq_state()
                    self._handle_command(cmd)
                    if self._stop_event.is_set():
                        break
            except TransportAuthError as exc:
                with self._lock:
                    self._connected = False
                self._log("ERROR", f"Auth error polling commands: {exc}")
                self._stop_event.wait(timeout=3.0)
            except TransportError:
                with self._lock:
                    self._connected = False
                self._stop_event.wait(timeout=1.5)
            except Exception as exc:
                self._log("ERROR", f"Unexpected poll error: {exc}")
                self._stop_event.wait(timeout=1.5)

    # ------------------------------------------------------------------
    # Command dispatch (all 16 CommandType members + local UI actions)
    # ------------------------------------------------------------------
    def _send_ack(
        self,
        cmd_id: str,
        status: str,
        detail: str = "",
        result: dict[str, Any] | None = None,
    ) -> None:
        tr = self._get_transport()
        if tr is None or not cmd_id:
            return
        try:
            tr.post_ack(
                CommandAck(
                    cmd_id=cmd_id,
                    status=status,
                    detail=detail,
                    result=result or {},
                )
            )
        except TransportError:
            pass

    def execute_local_action(
        self, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Execute a command locally (used by ClientStatusServer)."""
        payload = dict(payload or {})
        try:
            cmd_type = CommandType(action)
        except ValueError as exc:
            return {"ok": False, "error": f"Unknown action: {action} ({exc})"}
        cmd = Command(
            cmd_id=f"local-{uuid.uuid4().hex[:8]}",
            seq=0,
            cmd_type=cmd_type,
            payload=payload,
        )
        return self._dispatch_command(cmd, send_remote_ack=False)

    def _handle_command(self, cmd: Command) -> None:
        self._send_ack(cmd.cmd_id, "accepted", f"Received {cmd.cmd_type.value}")
        self._dispatch_command(cmd, send_remote_ack=True)

    def _dispatch_command(
        self, cmd: Command, *, send_remote_ack: bool
    ) -> dict[str, Any]:
        ctype = cmd.cmd_type
        payload = dict(cmd.payload or {})
        try:
            if ctype == CommandType.REFRESH_STATUS:
                status = self._collect_status_safe()
                tr = self._get_transport()
                if tr is not None:
                    try:
                        tr.heartbeat(status)
                        tr.post_state(self.sm.state)
                    except TransportError:
                        pass
                res = {"ok": True, "system_status": status.to_dict()}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", "Status refreshed", res)
                return res

            if ctype == CommandType.REQUEST_ELEVATION:
                import sys

                res = self.drivers.system.request_elevation(
                    sys.executable, sys.argv, os.getcwd()
                )
                status = self._collect_status_safe()
                res["system_status"] = status.to_dict()
                self._log("INFO", f"Elevation request: {res.get('detail', '')}")
                tr = self._get_transport()
                if tr is not None:
                    try:
                        tr.heartbeat(status)
                    except TransportError:
                        pass
                if send_remote_ack:
                    self._send_ack(
                        cmd.cmd_id,
                        "completed" if res.get("ok", True) else "failed",
                        str(res.get("detail", "")),
                        res,
                    )
                return res

            if ctype == CommandType.SMART_APP_CONTROL:
                mode = str(payload.get("mode", "guided"))
                res = self.drivers.system.disable_smart_app_control(mode=mode)
                status = self._collect_status_safe()
                res["system_status"] = status.to_dict()
                self._log("INFO", f"Smart App Control ({mode}): {res.get('detail', '')}")
                tr = self._get_transport()
                if tr is not None:
                    try:
                        tr.heartbeat(status)
                    except TransportError:
                        pass
                if send_remote_ack:
                    self._send_ack(
                        cmd.cmd_id,
                        "completed" if res.get("ok", True) else "failed",
                        str(res.get("detail", "")),
                        res,
                    )
                return res

            if ctype == CommandType.DEFENDER_CONTROL:
                action = str(payload.get("action", "disable"))
                exclusions = payload.get("exclusion_paths")
                res = self.drivers.system.configure_defender(
                    action, exclusion_paths=exclusions
                )
                status = self._collect_status_safe()
                res["system_status"] = status.to_dict()
                self._log("INFO", f"Defender ({action}): {res.get('detail', '')}")
                tr = self._get_transport()
                if tr is not None:
                    try:
                        tr.heartbeat(status)
                    except TransportError:
                        pass
                if send_remote_ack:
                    self._send_ack(
                        cmd.cmd_id,
                        "completed" if res.get("ok", True) else "failed",
                        str(res.get("detail", "")),
                        res,
                    )
                return res

            if ctype == CommandType.START_INSTALL:
                version = _validate_safe_version(str(payload.get("version") or "2025"))
                iso_spec = self._resolve_iso_spec(payload, version)
                self._pause_event.clear()
                self._cancel_event.clear()
                self.sm.start_install(version, iso_spec)
                self._log("INFO", f"Starting deployment for 3ds Max {version}")
                self._post_state_safe()
                self._launch_worker(self._run_download_and_install)
                res = {"ok": True, "version": version}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", "Install started", res)
                return res

            if ctype == CommandType.CONFIRM_LICENSE:
                self._pause_event.clear()
                self._cancel_event.clear()
                self.sm.confirm_license()
                self._log("INFO", "License confirmed by operator; starting FIRST_RUN.")
                self._post_state_safe()
                self._launch_worker(self._run_first_run)
                res = {"ok": True}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", "First run started", res)
                return res

            if ctype == CommandType.TRIGGER_FIRST_RUN:
                self._pause_event.clear()
                self._cancel_event.clear()
                st = self.sm.state
                if st.stage == Stage.WAIT_LICENSE:
                    self.sm.confirm_license()
                elif st.stage == Stage.FIRST_RUN and st.status == StageStatus.PAUSED:
                    self.sm.resume()
                elif st.stage == Stage.FIRST_RUN and st.status in (
                    StageStatus.FAILED,
                    StageStatus.ABORTED,
                ):
                    self.sm.retry()
                self._post_state_safe()
                self._launch_worker(self._run_first_run)
                res = {"ok": True}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", "First run triggered", res)
                return res

            if ctype == CommandType.TRIGGER_POST_CONFIG:
                self._pause_event.clear()
                self._cancel_event.clear()
                st = self.sm.state
                version = st.target_version or str(payload.get("version") or "2025")
                steps = None
                if not st.post_config_steps or st.status == StageStatus.WAITING_OPERATOR:
                    steps = self.drivers.post_config.load_steps(version=version)
                if st.stage == Stage.POST_CONFIG and st.status == StageStatus.PAUSED:
                    self.sm.resume()
                elif st.stage == Stage.POST_CONFIG and st.status in (
                    StageStatus.FAILED,
                    StageStatus.ABORTED,
                ):
                    self.sm.retry()
                else:
                    self.sm.start_post_config(steps)
                self._log("INFO", "Starting 1-click POST_CONFIG pipeline.")
                self._post_state_safe()
                selected = payload.get("steps")
                self._launch_worker(lambda: self._run_post_config(selected_steps=selected))
                res = {"ok": True}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", "Post-config started", res)
                return res

            if ctype == CommandType.SKIP_STAGE:
                raw_target = payload.get("target_stage")
                if not raw_target:
                    raise ValueError("Missing required 'target_stage' in SKIP_STAGE payload")
                target = Stage(str(raw_target))
                self.sm.skip_stage(target)
                self._log("INFO", f"Skipped to stage {target.value}")
                self._post_state_safe()
                res = {"ok": True, "stage": target.value}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", f"Skipped to {target.value}", res)
                return res

            if ctype == CommandType.PAUSE:
                reason = str(payload.get("reason") or "Paused by operator")
                self._pause_event.set()
                self.sm.pause(reason)
                self._log("WARN", reason)
                self._post_state_safe()
                res = {"ok": True}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", reason, res)
                return res

            if ctype == CommandType.RESUME:
                self._pause_event.clear()
                self._cancel_event.clear()
                st = self.sm.resume()
                self._log("INFO", f"Resumed stage {st.stage.value}")
                self._post_state_safe()
                self._relaunch_for_stage(st.stage)
                res = {"ok": True, "stage": st.stage.value}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", f"Resumed {st.stage.value}", res)
                return res

            if ctype == CommandType.RETRY:
                self._pause_event.clear()
                self._cancel_event.clear()
                st = self.sm.retry()
                self._log("INFO", f"Retrying stage {st.stage.value} (attempt {st.retry_count})")
                self._post_state_safe()
                self._relaunch_for_stage(st.stage)
                res = {"ok": True, "stage": st.stage.value}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", f"Retrying {st.stage.value}", res)
                return res

            if ctype == CommandType.ABORT:
                reason = str(payload.get("reason") or "Aborted by operator")
                self._cancel_event.set()
                self.sm.abort(reason)
                self._log("WARN", reason)
                self._post_state_safe()
                res = {"ok": True}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", reason, res)
                return res

            if ctype == CommandType.RESET:
                target = Stage(str(payload.get("target_stage") or Stage.PREP.value))
                self._cancel_event.set()
                self._wait_worker(timeout=2.0)
                self._pause_event.clear()
                self._cancel_event.clear()
                self.sm.reset(target)
                self._log("INFO", f"State reset to {target.value}")
                self._post_state_safe()
                res = {"ok": True, "stage": target.value}
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", f"Reset to {target.value}", res)
                return res

            if ctype == CommandType.PUSH_UPDATE:
                return self._handle_push_update(cmd, payload, send_remote_ack=send_remote_ack)

            if ctype == CommandType.CAPTURE_SCREENSHOT:
                shots_dir = self.data_dir / "screenshots"
                shots_dir.mkdir(parents=True, exist_ok=True)
                dest = shots_dir / f"shot_{int(time.time())}.png"
                saved = self.drivers.system.capture_screenshot(dest)
                res = {"ok": saved is not None, "path": str(saved) if saved else None}
                self._log("INFO", f"Captured screenshot: {res['path']}")
                if send_remote_ack:
                    self._send_ack(cmd.cmd_id, "completed", "Screenshot captured", res)
                return res

            res = {"ok": False, "error": f"Unhandled command: {ctype.value}"}
            if send_remote_ack:
                self._send_ack(cmd.cmd_id, "rejected", res["error"], res)
            return res

        except (InvalidTransitionError, ValueError) as exc:
            msg = f"Invalid command or state transition for {ctype.value}: {exc}"
            self._log("WARN", msg)
            if send_remote_ack:
                self._send_ack(cmd.cmd_id, "rejected", msg)
            return {"ok": False, "error": msg}
        except Exception as exc:
            msg = f"Command {ctype.value} failed: {exc}"
            self._log("ERROR", msg)
            if send_remote_ack:
                self._send_ack(cmd.cmd_id, "failed", msg)
            return {"ok": False, "error": msg}

    def _resolve_iso_spec(self, payload: dict[str, Any], version: str) -> IsoSpec | None:
        if payload.get("iso_spec"):
            return IsoSpec.from_dict(payload["iso_spec"])
        file_id = payload.get("file_id")
        tr = self._get_transport()
        if file_id and tr is not None:
            meta = tr.get_file_meta(str(file_id))
            return IsoSpec(
                file_id=str(meta["file_id"]),
                filename=str(meta["filename"]),
                version=str(meta.get("version") or version),
                size_bytes=int(meta["size_bytes"]),
                sha256=str(meta["sha256"]),
                download_url=meta.get("download_url"),
            )
        return None

    def _handle_push_update(
        self, cmd: Command, payload: dict[str, Any], *, send_remote_ack: bool
    ) -> dict[str, Any]:
        if not self.sm.can_accept_update():
            st = self.sm.state
            detail = (
                f"Update deferred: stage {st.stage.value}/{st.status.value} is active"
            )
            self._log("WARN", detail)
            res = {
                "ok": False,
                "deferred": True,
                "stage": st.stage.value,
                "status": st.status.value,
            }
            if send_remote_ack:
                self._send_ack(cmd.cmd_id, "rejected", detail, res)
            return res

        manifest: UpdateManifest | None = None
        if payload.get("manifest"):
            manifest = UpdateManifest.from_dict(payload["manifest"])
        else:
            tr = self._get_transport()
            if tr is not None:
                manifest = tr.get_latest_update()

        if manifest is None:
            res = {"ok": True, "updated": False, "detail": "No update available"}
            if send_remote_ack:
                self._send_ack(cmd.cmd_id, "completed", "No update available", res)
            return res

        app_dir = (self.app_root / "app") if self.app_root else (self.data_dir / "app")
        updater = AppUpdater(
            app_dir=app_dir,
            runtime_version=self._runtime_version,
            current_version=self._agent_version,
        )
        try:
            _validate_safe_filename(manifest.filename)
            _validate_safe_version(manifest.version)
            ok, reason = updater.check_manifest(manifest, state_machine=self.sm)
        except Exception as exc:
            ok, reason = False, str(exc)
        if not ok:
            res = {"ok": False, "updated": False, "detail": reason}
            if send_remote_ack:
                self._send_ack(cmd.cmd_id, "rejected", reason, res)
            return res

        tr = self._get_transport()
        if tr is None:
            res = {"ok": False, "detail": "No transport available to download update"}
            if send_remote_ack:
                self._send_ack(cmd.cmd_id, "failed", res["detail"], res)
            return res

        updater.staging_dir.mkdir(parents=True, exist_ok=True)
        staging_resolved = updater.staging_dir.resolve()
        local_archive = updater.staging_dir / manifest.filename
        if (
            local_archive.resolve().parent != staging_resolved
            or not local_archive.resolve().is_relative_to(staging_resolved)
        ):
            reason = f"Unsafe update filename: {manifest.filename!r}"
            res = {"ok": False, "updated": False, "detail": reason}
            if send_remote_ack:
                self._send_ack(cmd.cmd_id, "rejected", reason, res)
            return res
        with tr.open_file_stream(
            manifest.file_id, download_url=manifest.download_url
        ) as resp:
            with local_archive.open("wb") as fh:
                for chunk in resp.iter_chunks():
                    fh.write(chunk)

        apply_res = updater.apply_update(
            manifest, archive_path=local_archive, state_machine=self.sm
        )
        self._log("INFO", f"Applied update {manifest.version}; restarting with exit 42.")
        res = {"ok": True, "restarting": True, "version": manifest.version, **apply_res}
        if send_remote_ack:
            self._send_ack(
                cmd.cmd_id,
                "completed",
                f"Updated to {manifest.version}; restarting",
                res,
            )
        self._flush_logs_once()
        self._post_state_safe()
        self._exit_code = UPDATE_RESTART_EXIT_CODE
        if self._on_update_restart is not None:
            try:
                self._on_update_restart(UPDATE_RESTART_EXIT_CODE)
            except Exception:
                pass
        self._stop_event.set()
        return res

    # ------------------------------------------------------------------
    # Stage worker execution
    # ------------------------------------------------------------------
    def _launch_worker(self, target: Callable[[], None]) -> bool:
        with self._worker_lock:
            if self._worker_thread is not None and self._worker_thread.is_alive():
                self._cancel_event.set()
                self._worker_thread.join(timeout=2.0)
                if self._worker_thread.is_alive():
                    self._log(
                        "WARN",
                        "Previous stage worker is still running; refusing to launch duplicate worker.",
                    )
                    return False
            self._cancel_event.clear()
            self._worker_thread = threading.Thread(
                target=target, name="agent-stage-worker", daemon=True
            )
            self._worker_thread.start()
            return True

    def _wait_worker(self, timeout: float = 2.0) -> None:
        with self._worker_lock:
            wt = self._worker_thread
        if wt is not None and wt.is_alive() and wt is not threading.current_thread():
            wt.join(timeout=timeout)

    def _relaunch_for_stage(self, stage: Stage) -> None:
        if stage == Stage.DOWNLOAD_ISO:
            self._launch_worker(self._run_download_and_install)
        elif stage == Stage.INSTALL_MAX:
            self._launch_worker(self._run_install_only)
        elif stage == Stage.FIRST_RUN:
            self._launch_worker(self._run_first_run)
        elif stage == Stage.POST_CONFIG:
            self._launch_worker(self._run_post_config)

    def _run_download_and_install(self) -> None:
        st = self.sm.state
        version = st.target_version or "2025"
        iso_spec = st.iso_spec
        iso_path: Path

        if iso_spec is not None:
            last_push = 0.0
            last_phase = ""

            def progress_cb(tp: TransferProgress) -> None:
                nonlocal last_push, last_phase
                try:
                    self.sm.update_transfer_progress(tp)
                except InvalidTransitionError:
                    return
                now = time.monotonic()
                if tp.phase != last_phase or (now - last_push) >= 0.25:
                    last_phase = tp.phase
                    last_push = now
                    self._post_state_safe()

            try:
                client = IsoTransferClient(transport=self._get_transport())
                result = client.download(
                    iso_spec,
                    self.iso_dir,
                    progress_cb=progress_cb,
                    pause_event=self._pause_event,
                    cancel_event=self._cancel_event,
                )
                iso_path = result.local_path
                self.sm.complete_iso_download(str(iso_path), skipped=result.skipped)
                msg = (
                    f"ISO verified locally (skipped download): {iso_path}"
                    if result.skipped
                    else f"ISO downloaded & verified: {iso_path}"
                )
                self._log("INFO", msg)
                self._post_state_safe()
            except TransferPausedError:
                if self.sm.state.status == StageStatus.RUNNING:
                    try:
                        self.sm.pause("Download paused")
                    except InvalidTransitionError:
                        pass
                self._post_state_safe()
                return
            except TransferCancelledError:
                if self.sm.state.status == StageStatus.RUNNING:
                    try:
                        self.sm.abort("Download cancelled")
                    except InvalidTransitionError:
                        pass
                self._post_state_safe()
                return
            except Exception as exc:
                if self.sm.state.status == StageStatus.RUNNING:
                    try:
                        self.sm.fail(f"ISO transfer failed: {exc}")
                    except InvalidTransitionError:
                        pass
                self._log("ERROR", f"ISO transfer failed: {exc}")
                self._post_state_safe()
                return
        else:
            iso_path = self.iso_dir / f"3dsMax_{version}.iso"
            try:
                self.sm.complete_iso_download(str(iso_path), skipped=True)
                self._post_state_safe()
            except InvalidTransitionError:
                return

        self._run_install_only(iso_path_override=iso_path)

    def _run_install_only(self, iso_path_override: Path | None = None) -> None:
        st = self.sm.state
        version = st.target_version or "2025"
        iso_path = iso_path_override or (
            Path(st.iso_local_path)
            if st.iso_local_path
            else (self.iso_dir / f"3dsMax_{version}.iso")
        )
        last_push = 0.0
        last_pct = -1

        def progress_cb(percent: int, message: str) -> None:
            nonlocal last_push, last_pct
            try:
                self.sm.update_install_progress(percent, message)
            except InvalidTransitionError:
                return
            now = time.monotonic()
            if percent != last_pct and (percent in (0, 100) or (now - last_push) >= 0.25):
                last_pct = percent
                last_push = now
                self._post_state_safe()

        def log_cb(level: str, msg: str) -> None:
            self._log(level, msg, stage=Stage.INSTALL_MAX.value)

        try:
            res = self.drivers.installer.install_3dsmax(
                version,
                iso_path,
                progress_cb=progress_cb,
                log_cb=log_cb,
                cancel_event=self._cancel_event,
            )
            if self._pause_event.is_set() or self.sm.state.status == StageStatus.PAUSED:
                return
            if self._cancel_event.is_set() or res.get("status") == "aborted":
                if self.sm.state.status == StageStatus.RUNNING:
                    self.sm.abort(str(res.get("detail") or "Install aborted"))
                self._post_state_safe()
                return
            if not res.get("ok"):
                if self.sm.state.status == StageStatus.RUNNING:
                    self.sm.fail(str(res.get("detail") or "3ds Max installation failed"))
                self._post_state_safe()
                return
            self.sm.complete_install()
            self._log(
                "INFO",
                "3ds Max installed. Please complete licensing on Client PC and click Confirm License.",
            )
            self._post_state_safe()
        except InvalidTransitionError:
            pass
        except Exception as exc:
            if self.sm.state.status == StageStatus.RUNNING:
                try:
                    self.sm.fail(str(exc))
                except InvalidTransitionError:
                    pass
            self._log("ERROR", f"Install failed: {exc}")
            self._post_state_safe()

    def _run_first_run(self) -> None:
        st = self.sm.state
        version = st.target_version or "2025"
        dump_dir = self.data_dir / "dumps"
        dump_dir.mkdir(parents=True, exist_ok=True)

        def log_cb(level: str, msg: str) -> None:
            self._log(level, msg, stage=Stage.FIRST_RUN.value)

        try:
            res = self.drivers.first_run.run_first_run(
                version,
                log_cb=log_cb,
                cancel_event=self._cancel_event,
                dump_dir=dump_dir,
            )
            outcome = str(res.get("status") or "failed")
            detail = str(res.get("detail") or "")
            if self._pause_event.is_set() or self.sm.state.status == StageStatus.PAUSED:
                return
            if self._cancel_event.is_set() or outcome == "aborted":
                if self.sm.state.status == StageStatus.RUNNING:
                    self.sm.abort(detail or "First run aborted")
                self._post_state_safe()
                return
            if outcome == "licensing_required":
                self.sm.return_to_wait_license(
                    detail or "Licensing dialog detected during first run"
                )
                self._log("WARN", f"Returned to WAIT_LICENSE: {detail}")
                self._post_state_safe()
                return
            if outcome == "ready":
                self.sm.complete_first_run(auto_post_config=False)
                self._log("INFO", "First run completed; ready for 1-click POST_CONFIG.")
                self._post_state_safe()
                return
            if self.sm.state.status == StageStatus.RUNNING:
                self.sm.fail(detail or f"First run ended with status={outcome}")
            self._post_state_safe()
        except InvalidTransitionError:
            pass
        except Exception as exc:
            if self.sm.state.status == StageStatus.RUNNING:
                try:
                    self.sm.fail(str(exc))
                except InvalidTransitionError:
                    pass
            self._log("ERROR", f"First run error: {exc}")
            self._post_state_safe()

    def _run_post_config(self, selected_steps: list[str] | None = None) -> None:
        st = self.sm.state
        version = st.target_version or "2025"

        def log_cb(level: str, msg: str) -> None:
            self._log(level, msg, stage=Stage.POST_CONFIG.value)

        try:
            steps = list(st.post_config_steps)
            if not steps:
                steps = self.drivers.post_config.load_steps(version=version)
                self.sm.start_post_config(steps)
                self._post_state_safe()

            selected_set = set(selected_steps) if selected_steps else None
            for step in steps:
                if self._pause_event.is_set() or self.sm.state.status == StageStatus.PAUSED:
                    return
                if self._cancel_event.is_set():
                    if self.sm.state.status == StageStatus.RUNNING:
                        self.sm.abort("Post-config aborted")
                    self._post_state_safe()
                    return
                if step.status == "done":
                    continue
                if selected_set is not None and step.step_id not in selected_set:
                    self.sm.update_post_config_step(step.step_id, "skipped", "Not selected")
                    self._post_state_safe()
                    continue

                self.sm.update_post_config_step(step.step_id, "running", "Executing...")
                self._post_state_safe()

                step_res = self.drivers.post_config.run_step(
                    step.step_id,
                    version,
                    assets_dir=self.assets_dir,
                    log_cb=log_cb,
                    cancel_event=self._cancel_event,
                )
                step_status = str(
                    step_res.get("status")
                    or ("done" if step_res.get("ok") else "failed")
                )
                detail = str(step_res.get("detail") or "")
                if step_status == "aborted" or self._cancel_event.is_set():
                    self.sm.update_post_config_step(step.step_id, "failed", detail or "Aborted")
                    if self.sm.state.status == StageStatus.RUNNING:
                        self.sm.abort(detail or "Post-config aborted")
                    self._post_state_safe()
                    return
                if not step_res.get("ok") and step_status != "skipped":
                    self.sm.update_post_config_step(step.step_id, "failed", detail)
                    if self.sm.state.status == StageStatus.RUNNING:
                        self.sm.fail(detail or f"Step {step.step_id} failed")
                    self._post_state_safe()
                    return
                final_step_status = "skipped" if step_status == "skipped" else "done"
                self.sm.update_post_config_step(step.step_id, final_step_status, detail)
                self._post_state_safe()

            verify_report = self.drivers.post_config.verify_installation(version)
            final_state = self.sm.state
            summary = {
                "version": version,
                "iso_local_path": final_state.iso_local_path,
                "iso_skipped_download": final_state.iso_skipped_download,
                "verify": verify_report,
                "steps": [s.to_dict() for s in final_state.post_config_steps],
                "completed_at": utc_now_iso(),
            }
            self.sm.complete_post_config(summary)
            self._log("INFO", f"3ds Max {version} deployment completed (DONE).")
            self._post_state_safe()
        except InvalidTransitionError:
            pass
        except Exception as exc:
            if self.sm.state.status == StageStatus.RUNNING:
                try:
                    self.sm.fail(str(exc))
                except InvalidTransitionError:
                    pass
            self._log("ERROR", f"Post-config error: {exc}")
            self._post_state_safe()
