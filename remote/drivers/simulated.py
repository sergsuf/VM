"""Deterministic pure-stdlib simulated platform drivers for cross-platform testing."""

from __future__ import annotations

import json
import os
import shutil
import struct
import tempfile
import threading
import time
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from remote.drivers.base import DriverBundle
from remote.protocol import (
    VALID_SAC_STATES,
    PostConfigStepState,
    SystemStatus,
    utc_now_iso,
)

SAC_STATE_TO_REGISTRY: dict[str, int | None] = {
    "off": 0,
    "enforced": 1,
    "evaluation": 2,
    "unknown": None,
}

VALID_FIRST_RUN_OUTCOMES: frozenset[str] = frozenset(
    {"ready", "licensing_required", "timeout", "failed"}
)

SAC_GUIDED_URI = "windowsdefender://smartapp/"
DEFENDER_THREAT_URI = "windowsdefender://threatsettings/"
DEFENDER_EXCLUSIONS_URI = "windowsdefender://exclusions/"

DEFAULT_POST_CONFIG_STEPS: tuple[tuple[str, str, str], ...] = (
    ("renderer_corona", "Install Chaos Corona Renderer", "renderer"),
    ("renderer_vray", "Install Chaos V-Ray Renderer", "renderer"),
    ("ui_theme", "Apply 3ds Max UI Theme", "theme"),
    ("ui_quad_menu", "Install Custom Quad Menus", "quad_menu"),
    ("ui_hotkeys", "Apply Custom Hotkeys", "hotkey"),
    ("scripts_deploy", "Deploy MAXScripts, Macros & Startup Scripts", "scripts"),
    ("verify_install", "Verify 3ds Max Installation & Renderers", "verify"),
)


def _interruptible_wait(cancel_event: threading.Event, delay_sec: float) -> bool:
    """Wait up to delay_sec; return True if cancelled."""
    if cancel_event.is_set():
        return True
    if delay_sec <= 0:
        return False
    return cancel_event.wait(delay_sec)


def _make_minimal_png(
    width: int = 16,
    height: int = 16,
    rgb: tuple[int, int, int] = (30, 41, 59),
) -> bytes:
    """Build a valid, spec-compliant RGB PNG image using Python stdlib only."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_data = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    row = b"\x00" + bytes(rgb) * width
    raw_scanlines = row * height
    idat_data = zlib.compress(raw_scanlines, level=6)
    return (
        signature
        + _chunk(b"IHDR", ihdr_data)
        + _chunk(b"IDAT", idat_data)
        + _chunk(b"IEND", b"")
    )


def _write_png(dest_path: Path, rgb: tuple[int, int, int] = (30, 41, 59)) -> Path:
    dest = Path(dest_path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(_make_minimal_png(rgb=rgb))
    return dest


def _safe_extract_zip(archive: Path, dest_dir: Path) -> list[Path]:
    """Extract a ZIP/.mzp archive after rejecting any Zip-Slip traversal entries."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    extracted: list[Path] = []
    with zipfile.ZipFile(archive, "r") as zf:
        for info in zf.infolist():
            normalized_name = info.filename.replace("\\", "/")
            if normalized_name.startswith("/") or (
                len(normalized_name) >= 2 and normalized_name[1] == ":"
            ):
                raise ValueError(f"Unsafe archive entry (absolute path): {info.filename}")
            member_path = (dest_dir / normalized_name).resolve()
            if not member_path.is_relative_to(dest_resolved):
                raise ValueError(f"Unsafe archive entry (Zip-Slip): {info.filename}")
        for info in zf.infolist():
            normalized_name = info.filename.replace("\\", "/")
            target = (dest_dir / normalized_name).resolve()
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, target.open("wb") as dst:
                shutil.copyfileobj(src, dst)
            extracted.append(target)
    return extracted


def _is_legacy_menu_version(version: str) -> bool:
    try:
        return int(version) <= 2024
    except ValueError:
        return version == "2024"


def _is_2026_or_newer(version: str) -> bool:
    try:
        return int(version) >= 2026
    except ValueError:
        return version >= "2026"


def _sim_max_exe_path(work_dir: Path, version: str) -> Path:
    return work_dir / "ProgramFiles" / "Autodesk" / f"3ds Max {version}" / "3dsmax.exe"


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.parent / f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
    payload = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


@dataclass
class _SimulatedState:
    """Shared mutable environment state synchronized across all four simulated drivers."""

    work_dir: Path | None = None
    is_admin: bool = True
    session_id: int = 1
    hostname: str = "SIM-CLIENT-PC"
    os_version: str = "Windows 11 Pro 24H2 (Simulated)"
    smart_app_control_state: str = "evaluation"
    smart_app_control_reboot_required: bool = False
    defender_rtp_enabled: bool = True
    defender_tamper_protected: bool = False
    defender_exclusions: list[str] = field(default_factory=list)
    added_exclusions_ledger: list[str] = field(default_factory=list)
    third_party_av: list[str] = field(default_factory=list)
    free_disk_bytes: int = 120 * 1024**3
    ahk_available: bool = True
    installed_max_versions: list[str] = field(default_factory=list)
    installer_running: bool = False
    fail_install: bool = False
    first_run_outcome: str = "ready"
    fail_post_config_steps: set[str] = field(default_factory=set)
    step_delay_sec: float = 0.01
    elevation_history: list[dict[str, Any]] = field(default_factory=list)
    lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        if self.smart_app_control_state not in VALID_SAC_STATES:
            raise ValueError(
                f"Invalid smart_app_control_state: {self.smart_app_control_state!r}; "
                f"expected one of {sorted(VALID_SAC_STATES)}"
            )
        if self.first_run_outcome not in VALID_FIRST_RUN_OUTCOMES:
            raise ValueError(
                f"Invalid first_run_outcome: {self.first_run_outcome!r}; "
                f"expected one of {sorted(VALID_FIRST_RUN_OUTCOMES)}"
            )
        if isinstance(self.step_delay_sec, bool) or self.step_delay_sec < 0:
            raise ValueError(
                f"step_delay_sec must be >= 0, got {self.step_delay_sec!r}"
            )
        if self.work_dir is None:
            self.work_dir = Path(tempfile.mkdtemp(prefix="max_sim_"))
        else:
            self.work_dir = Path(self.work_dir)
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.installed_max_versions = sorted(
            {str(v) for v in (self.installed_max_versions or [])}
        )
        self.defender_exclusions = list(self.defender_exclusions or [])
        self.added_exclusions_ledger = list(self.added_exclusions_ledger or [])
        self.third_party_av = list(self.third_party_av or [])
        self.fail_post_config_steps = set(self.fail_post_config_steps or ())


class SimulatedSystemDriver:
    """Simulated OS/UAC/Smart App Control/Defender/screenshot driver."""

    def __init__(self, state: _SimulatedState | None = None) -> None:
        self._state = state if state is not None else _SimulatedState()

    def collect_status(
        self, agent_version: str, runtime_version: str
    ) -> SystemStatus:
        with self._state.lock:
            return SystemStatus(
                hostname=self._state.hostname,
                os_version=self._state.os_version,
                is_admin=self._state.is_admin,
                session_id=self._state.session_id,
                smart_app_control_state=self._state.smart_app_control_state,
                smart_app_control_reboot_required=self._state.smart_app_control_reboot_required,
                defender_rtp_enabled=self._state.defender_rtp_enabled,
                defender_tamper_protected=self._state.defender_tamper_protected,
                defender_exclusions=list(self._state.defender_exclusions),
                third_party_av=list(self._state.third_party_av),
                free_disk_bytes=self._state.free_disk_bytes,
                installed_max_versions=list(self._state.installed_max_versions),
                ahk_available=self._state.ahk_available,
                agent_version=agent_version,
                runtime_version=runtime_version,
            )

    def request_elevation(
        self, executable: str, args: list[str], cwd: str
    ) -> dict[str, Any]:
        with self._state.lock:
            already_admin = self._state.is_admin
            self._state.is_admin = True
            result: dict[str, Any] = {
                "ok": True,
                "elevated": True,
                "already_admin": already_admin,
                "is_admin": True,
                "executable": str(executable),
                "args": list(args),
                "cwd": str(cwd),
                "method": "simulated_runas",
                "detail": (
                    "Already running with Administrator privileges."
                    if already_admin
                    else "Simulated UAC elevation granted."
                ),
            }
            self._state.elevation_history.append(dict(result))
            return result

    def get_smart_app_control_status(self) -> dict[str, Any]:
        with self._state.lock:
            state = self._state.smart_app_control_state
            return {
                "state": state,
                "registry_value": SAC_STATE_TO_REGISTRY[state],
                "reboot_required": self._state.smart_app_control_reboot_required,
                "ci_enforced": state == "enforced",
                "guided_uri": SAC_GUIDED_URI,
                "source": "simulated",
            }

    def disable_smart_app_control(self, mode: str = "guided") -> dict[str, Any]:
        with self._state.lock:
            prev = self._state.smart_app_control_state
            curr_rb = self._state.smart_app_control_reboot_required

            if mode not in ("guided", "scripted"):
                return {
                    "ok": False,
                    "changed": False,
                    "mode": mode,
                    "previous_state": prev,
                    "state": prev,
                    "registry_value": SAC_STATE_TO_REGISTRY[prev],
                    "reboot_required": curr_rb,
                    "guided_uri": SAC_GUIDED_URI,
                    "detail": f"Unsupported mode: {mode!r}; expected 'guided' or 'scripted'.",
                }

            if prev == "off":
                return {
                    "ok": True,
                    "changed": False,
                    "mode": mode,
                    "previous_state": "off",
                    "state": "off",
                    "registry_value": 0,
                    "reboot_required": False,
                    "guided_uri": SAC_GUIDED_URI,
                    "detail": "Smart App Control is already off.",
                    "warning": "Smart App Control cannot be re-enabled without reinstalling Windows.",
                }

            if mode == "guided":
                self._state.smart_app_control_state = "off"
                self._state.smart_app_control_reboot_required = False
                return {
                    "ok": True,
                    "changed": True,
                    "mode": "guided",
                    "previous_state": prev,
                    "state": "off",
                    "registry_value": 0,
                    "reboot_required": False,
                    "guided_uri": SAC_GUIDED_URI,
                    "detail": "Smart App Control turned off via guided Windows Security UI.",
                    "warning": "Smart App Control cannot be re-enabled without reinstalling Windows.",
                }

            # mode == "scripted"
            if not self._state.is_admin:
                return {
                    "ok": False,
                    "changed": False,
                    "mode": "scripted",
                    "previous_state": prev,
                    "state": prev,
                    "registry_value": SAC_STATE_TO_REGISTRY[prev],
                    "reboot_required": curr_rb,
                    "guided_uri": SAC_GUIDED_URI,
                    "detail": "Administrator privileges required for scripted Smart App Control disable.",
                }

            if prev == "enforced":
                self._state.smart_app_control_reboot_required = True
                return {
                    "ok": False,
                    "changed": False,
                    "mode": "scripted",
                    "previous_state": "enforced",
                    "state": "enforced",
                    "registry_value": 1,
                    "reboot_required": True,
                    "guided_uri": SAC_GUIDED_URI,
                    "detail": (
                        "Kernel Code Integrity protects enforced Smart App Control; "
                        "open windowsdefender://smartapp/ or reboot to complete."
                    ),
                }

            # evaluation or unknown with admin
            self._state.smart_app_control_state = "off"
            self._state.smart_app_control_reboot_required = True
            return {
                "ok": True,
                "changed": True,
                "mode": "scripted",
                "previous_state": prev,
                "state": "off",
                "registry_value": 0,
                "reboot_required": True,
                "guided_uri": SAC_GUIDED_URI,
                "detail": "Smart App Control registry policy set to Off (reboot recommended).",
                "warning": "Smart App Control cannot be re-enabled without reinstalling Windows.",
            }

    def get_defender_status(self) -> dict[str, Any]:
        with self._state.lock:
            return {
                "antivirus_enabled": True,
                "am_service_enabled": True,
                "rtp_enabled": self._state.defender_rtp_enabled,
                "tamper_protected": self._state.defender_tamper_protected,
                "exclusions": list(self._state.defender_exclusions),
                "third_party_av": list(self._state.third_party_av),
                "guided_uri": DEFENDER_THREAT_URI,
                "exclusions_uri": DEFENDER_EXCLUSIONS_URI,
                "source": "simulated",
            }

    def _add_exclusions_locked(self, paths: list[str]) -> list[str]:
        newly_added: list[str] = []
        for raw in paths:
            if not isinstance(raw, str):
                continue
            cleaned = raw.strip()
            if not cleaned:
                continue
            if cleaned not in self._state.defender_exclusions:
                self._state.defender_exclusions.append(cleaned)
                self._state.added_exclusions_ledger.append(cleaned)
                newly_added.append(cleaned)
        return newly_added

    def configure_defender(
        self, action: str, exclusion_paths: list[str] | None = None
    ) -> dict[str, Any]:
        with self._state.lock:
            if action == "open_settings":
                return {
                    "ok": True,
                    "action": "open_settings",
                    "rtp_enabled": self._state.defender_rtp_enabled,
                    "tamper_protected": self._state.defender_tamper_protected,
                    "exclusions": list(self._state.defender_exclusions),
                    "guided_fallback": False,
                    "guided_uri": DEFENDER_THREAT_URI,
                    "detail": (
                        "Opened Windows Security Threat Settings "
                        f"({DEFENDER_THREAT_URI})."
                    ),
                }

            if action == "add_exclusions":
                if not self._state.is_admin:
                    return {
                        "ok": False,
                        "action": "add_exclusions",
                        "added": [],
                        "exclusions": list(self._state.defender_exclusions),
                        "rtp_enabled": self._state.defender_rtp_enabled,
                        "tamper_protected": self._state.defender_tamper_protected,
                        "guided_fallback": False,
                        "guided_uri": DEFENDER_EXCLUSIONS_URI,
                        "detail": "Administrator privileges required to modify Defender exclusions.",
                    }
                newly_added = self._add_exclusions_locked(exclusion_paths or [])
                return {
                    "ok": True,
                    "action": "add_exclusions",
                    "added": newly_added,
                    "exclusions": list(self._state.defender_exclusions),
                    "rtp_enabled": self._state.defender_rtp_enabled,
                    "tamper_protected": self._state.defender_tamper_protected,
                    "guided_fallback": False,
                    "guided_uri": DEFENDER_EXCLUSIONS_URI,
                    "detail": f"Added {len(newly_added)} Defender exclusion path(s).",
                }

            if action == "disable":
                if not self._state.is_admin:
                    return {
                        "ok": False,
                        "action": "disable",
                        "rtp_enabled": self._state.defender_rtp_enabled,
                        "tamper_protected": self._state.defender_tamper_protected,
                        "guided_fallback": True,
                        "guided_uri": DEFENDER_THREAT_URI,
                        "added_exclusions": [],
                        "exclusions": list(self._state.defender_exclusions),
                        "detail": "Administrator privileges required to disable Windows Defender.",
                    }

                newly_added = self._add_exclusions_locked(exclusion_paths or [])
                if self._state.defender_tamper_protected:
                    return {
                        "ok": False,
                        "action": "disable",
                        "rtp_enabled": self._state.defender_rtp_enabled,
                        "tamper_protected": True,
                        "guided_fallback": True,
                        "guided_uri": DEFENDER_THREAT_URI,
                        "added_exclusions": newly_added,
                        "exclusions": list(self._state.defender_exclusions),
                        "detail": (
                            "Tamper Protection is enabled; Real-Time Protection cannot be disabled "
                            "programmatically. Open windowsdefender://threatsettings/ to disable manually."
                        ),
                    }

                self._state.defender_rtp_enabled = False
                return {
                    "ok": True,
                    "action": "disable",
                    "rtp_enabled": False,
                    "tamper_protected": False,
                    "guided_fallback": False,
                    "guided_uri": DEFENDER_THREAT_URI,
                    "added_exclusions": newly_added,
                    "exclusions": list(self._state.defender_exclusions),
                    "detail": "Real-Time Protection disabled (verified).",
                }

            if action == "restore":
                if not self._state.is_admin:
                    return {
                        "ok": False,
                        "action": "restore",
                        "removed_exclusions": [],
                        "exclusions": list(self._state.defender_exclusions),
                        "rtp_enabled": self._state.defender_rtp_enabled,
                        "tamper_protected": self._state.defender_tamper_protected,
                        "guided_fallback": False,
                        "guided_uri": DEFENDER_THREAT_URI,
                        "detail": "Administrator privileges required to restore Defender settings.",
                    }

                if exclusion_paths is not None:
                    targets = {p.strip() for p in exclusion_paths if isinstance(p, str) and p.strip()}
                else:
                    targets = set(self._state.added_exclusions_ledger)

                removed: list[str] = []
                remaining: list[str] = []
                for path in self._state.defender_exclusions:
                    if path in targets:
                        removed.append(path)
                    else:
                        remaining.append(path)
                self._state.defender_exclusions = remaining
                if exclusion_paths is None:
                    self._state.added_exclusions_ledger.clear()
                else:
                    self._state.added_exclusions_ledger = [
                        p for p in self._state.added_exclusions_ledger if p not in targets
                    ]
                self._state.defender_rtp_enabled = True
                return {
                    "ok": True,
                    "action": "restore",
                    "removed_exclusions": removed,
                    "exclusions": list(self._state.defender_exclusions),
                    "rtp_enabled": True,
                    "tamper_protected": self._state.defender_tamper_protected,
                    "guided_fallback": False,
                    "guided_uri": DEFENDER_THREAT_URI,
                    "detail": (
                        f"Restored Real-Time Protection and removed {len(removed)} "
                        "agent-added exclusion(s)."
                    ),
                }

            return {
                "ok": False,
                "action": action,
                "rtp_enabled": self._state.defender_rtp_enabled,
                "tamper_protected": self._state.defender_tamper_protected,
                "exclusions": list(self._state.defender_exclusions),
                "guided_fallback": False,
                "guided_uri": DEFENDER_THREAT_URI,
                "detail": f"Unsupported Defender action: {action}",
            }

    def capture_screenshot(self, dest_path: Path) -> Path | None:
        return _write_png(Path(dest_path))


class SimulatedInstallerDriver:
    """Simulated Autodesk 3ds Max ISO silent installer mirroring Install-3dsMax.ps1."""

    def __init__(self, state: _SimulatedState | None = None) -> None:
        self._state = state if state is not None else _SimulatedState()

    @property
    def fail_install(self) -> bool:
        with self._state.lock:
            return self._state.fail_install

    @fail_install.setter
    def fail_install(self, value: bool) -> None:
        with self._state.lock:
            self._state.fail_install = bool(value)

    def _sim_max_exe(self, version: str) -> Path:
        assert self._state.work_dir is not None
        return _sim_max_exe_path(self._state.work_dir, version)

    def _sim_status_file(self, version: str) -> Path:
        assert self._state.work_dir is not None
        return self._state.work_dir / "logs" / f"3dsMax-{version}-status.txt"

    def _write_status_line(self, version: str, percent: int, message: str) -> None:
        status_file = self._sim_status_file(version)
        status_file.parent.mkdir(parents=True, exist_ok=True)
        status_file.write_text(f"{percent}%|{message}\n", encoding="utf-8")

    def is_max_installed(self, version: str) -> bool:
        with self._state.lock:
            return str(version) in self._state.installed_max_versions

    def is_installer_running(self) -> bool:
        with self._state.lock:
            return self._state.installer_running

    def install_3dsmax(
        self,
        version: str,
        iso_path: Path,
        *,
        progress_cb: Callable[[int, str], None],
        log_cb: Callable[[str, str], None],
        cancel_event: threading.Event,
        timeout_sec: int = 2400,
    ) -> dict[str, Any]:
        ver = str(version)
        iso = Path(iso_path)

        if cancel_event.is_set():
            log_cb("WARN", f"Installation of 3ds Max {ver} cancelled before start.")
            return {
                "ok": False,
                "status": "aborted",
                "version": ver,
                "iso_path": str(iso),
                "exit_code": -1,
                "already_installed": False,
                "detail": "Installation cancelled before start.",
            }

        if not iso.exists() or not iso.is_file():
            log_cb("ERROR", f"ISO file not found: {iso}")
            return {
                "ok": False,
                "status": "failed",
                "version": ver,
                "iso_path": str(iso),
                "exit_code": 2,
                "already_installed": False,
                "detail": f"ISO file not found: {iso}",
            }

        if self.is_max_installed(ver) and not self.fail_install:
            max_exe = self._sim_max_exe(ver)
            max_exe.parent.mkdir(parents=True, exist_ok=True)
            if not max_exe.exists():
                max_exe.write_text(f"Simulated 3ds Max {ver}\n", encoding="utf-8")
            msg = f"Autodesk 3ds Max {ver} is already installed."
            self._write_status_line(ver, 100, msg)
            log_cb("INFO", msg)
            progress_cb(100, msg)
            return {
                "ok": True,
                "status": "completed",
                "version": ver,
                "iso_path": str(iso),
                "exit_code": 0,
                "already_installed": True,
                "max_exe": str(max_exe),
                "detail": msg,
            }

        milestones = [
            (5, f"Mounting ISO image {iso.name}..."),
            (15, f"Launching Autodesk 3ds Max {ver} Setup.exe -q..."),
            (35, f"Installing Autodesk 3ds Max {ver}... (6500 files)"),
            (60, f"Installing Autodesk 3ds Max {ver}... (11400 files)"),
            (85, f"Installing Autodesk 3ds Max {ver}... (16200 files)"),
            (95, f"Finalizing Autodesk 3ds Max {ver} installation..."),
        ]

        deadline = time.monotonic() + max(0.0, float(timeout_sec))
        with self._state.lock:
            self._state.installer_running = True
            delay = self._state.step_delay_sec

        try:
            for pct, msg in milestones:
                if cancel_event.is_set():
                    log_cb("WARN", f"Installation of 3ds Max {ver} cancelled.")
                    return {
                        "ok": False,
                        "status": "aborted",
                        "version": ver,
                        "iso_path": str(iso),
                        "exit_code": -1,
                        "already_installed": False,
                        "detail": "Installation cancelled by operator.",
                    }
                if time.monotonic() > deadline:
                    log_cb(
                        "ERROR",
                        f"Installation of 3ds Max {ver} timed out after {timeout_sec}s.",
                    )
                    return {
                        "ok": False,
                        "status": "timeout",
                        "version": ver,
                        "iso_path": str(iso),
                        "exit_code": 1,
                        "already_installed": False,
                        "detail": f"Installation timed out after {timeout_sec}s.",
                    }

                self._write_status_line(ver, pct, msg)
                progress_cb(pct, msg)
                log_cb("INFO", f"[{pct}%] {msg}")

                if self.fail_install and pct >= 60:
                    log_cb(
                        "ERROR",
                        f"Simulated MSI error 1603 during 3ds Max {ver} installation.",
                    )
                    self._write_status_line(ver, pct, "ERROR: MSI 1603")
                    return {
                        "ok": False,
                        "status": "failed",
                        "version": ver,
                        "iso_path": str(iso),
                        "exit_code": 1,
                        "already_installed": False,
                        "detail": (
                            f"Simulated installation failure (MSI 1603) for 3ds Max {ver}."
                        ),
                    }

                if _interruptible_wait(cancel_event, delay):
                    log_cb("WARN", f"Installation of 3ds Max {ver} cancelled.")
                    return {
                        "ok": False,
                        "status": "aborted",
                        "version": ver,
                        "iso_path": str(iso),
                        "exit_code": -1,
                        "already_installed": False,
                        "detail": "Installation cancelled by operator.",
                    }

            max_exe = self._sim_max_exe(ver)
            max_exe.parent.mkdir(parents=True, exist_ok=True)
            max_exe.write_text(f"Simulated 3ds Max {ver}\n", encoding="utf-8")

            with self._state.lock:
                if ver not in self._state.installed_max_versions:
                    self._state.installed_max_versions.append(ver)
                    self._state.installed_max_versions.sort()

            done_msg = f"Autodesk 3ds Max {ver} installed successfully."
            self._write_status_line(ver, 100, done_msg)
            progress_cb(100, done_msg)
            log_cb("INFO", done_msg)
            return {
                "ok": True,
                "status": "completed",
                "version": ver,
                "iso_path": str(iso),
                "exit_code": 0,
                "already_installed": False,
                "max_exe": str(max_exe),
                "detail": done_msg,
            }
        finally:
            with self._state.lock:
                self._state.installer_running = False


class SimulatedFirstRunDriver:
    """Simulated 3ds Max first-launch watcher mirroring `max-auto --profile client`."""

    def __init__(self, state: _SimulatedState | None = None) -> None:
        self._state = state if state is not None else _SimulatedState()

    @property
    def first_run_outcome(self) -> str:
        with self._state.lock:
            return self._state.first_run_outcome

    @first_run_outcome.setter
    def first_run_outcome(self, value: str) -> None:
        if value not in VALID_FIRST_RUN_OUTCOMES:
            raise ValueError(
                f"Invalid first_run_outcome: {value!r}; "
                f"expected one of {sorted(VALID_FIRST_RUN_OUTCOMES)}"
            )
        with self._state.lock:
            self._state.first_run_outcome = value

    def run_first_run(
        self,
        version: str,
        *,
        log_cb: Callable[[str, str], None],
        cancel_event: threading.Event,
        timeout_sec: int = 300,
        dump_dir: Path | None = None,
    ) -> dict[str, Any]:
        ver = str(version)
        with self._state.lock:
            outcome = self._state.first_run_outcome
            delay = self._state.step_delay_sec

        privacy_rule = (
            "Privacy Settings (3ds Max 2026+ WebView)"
            if _is_2026_or_newer(ver)
            else "Privacy / Data Collection Consent"
        )
        ready_rules = [
            privacy_rule,
            "Color Management / Theme Prompt (Don't show again + Continue)",
            "Welcome / What's New Screen",
            "3ds Max Security / Safe Scene Messages (Close QMessageWidget)",
        ]

        log_cb(
            "INFO",
            f"Launching 3ds Max {ver} (de-elevated) and starting first-run watcher...",
        )

        if cancel_event.is_set():
            log_cb("WARN", f"First-run for 3ds Max {ver} cancelled before launch.")
            return {
                "status": "aborted",
                "detail": "First-run cancelled before launch.",
                "matched_rules": [],
                "screenshots": [],
            }

        effective_outcome = "timeout" if timeout_sec <= 0 else outcome

        if _interruptible_wait(cancel_event, delay):
            log_cb("WARN", f"First-run for 3ds Max {ver} cancelled during splash.")
            return {
                "status": "aborted",
                "detail": "First-run cancelled during startup.",
                "matched_rules": [],
                "screenshots": [],
            }

        if effective_outcome == "ready":
            matched_rules: list[str] = []
            for rule in ready_rules:
                log_cb("INFO", f"[RULE '{rule}'] matched and dismissed.")
                matched_rules.append(rule)
                if _interruptible_wait(cancel_event, delay):
                    log_cb("WARN", f"First-run for 3ds Max {ver} cancelled.")
                    return {
                        "status": "aborted",
                        "detail": "First-run cancelled while handling popups.",
                        "matched_rules": matched_rules,
                        "screenshots": [],
                    }
            log_cb("INFO", f"Main workspace 'Untitled - Autodesk 3ds Max {ver}' is ready.")
            return {
                "status": "ready",
                "detail": f"Untitled - Autodesk 3ds Max {ver} workspace ready.",
                "matched_rules": matched_rules,
                "screenshots": [],
            }

        if effective_outcome == "licensing_required":
            matched = ["Autodesk Licensing - Let's Get Started (UIA Select)"]
            log_cb(
                "WARN",
                f"Licensing dialog detected for 3ds Max {ver}; pausing for manual licensing.",
            )
            screenshots = (
                [
                    str(
                        _write_png(
                            Path(dump_dir) / f"first_run_licensing_{ver}.png",
                            rgb=(180, 83, 9),
                        )
                    )
                ]
                if dump_dir is not None
                else []
            )
            return {
                "status": "licensing_required",
                "detail": (
                    f"Autodesk Licensing prompt detected for 3ds Max {ver}; "
                    "manual licensing required."
                ),
                "matched_rules": matched,
                "screenshots": screenshots,
            }

        if effective_outcome == "timeout":
            matched = [privacy_rule]
            log_cb(
                "ERROR",
                f"First-run watcher timed out after {timeout_sec}s waiting for 3ds Max {ver} workspace.",
            )
            screenshots = (
                [
                    str(
                        _write_png(
                            Path(dump_dir) / f"first_run_timeout_{ver}.png",
                            rgb=(153, 27, 27),
                        )
                    )
                ]
                if dump_dir is not None
                else []
            )
            return {
                "status": "timeout",
                "detail": (
                    f"Timed out after {timeout_sec}s waiting for Autodesk 3ds Max {ver} "
                    "main workspace."
                ),
                "matched_rules": matched,
                "screenshots": screenshots,
            }

        # effective_outcome == "failed"
        log_cb("ERROR", f"3ds Max {ver} exited unexpectedly during first-run startup.")
        screenshots = (
            [
                str(
                    _write_png(
                        Path(dump_dir) / f"first_run_failed_{ver}.png",
                        rgb=(127, 29, 29),
                    )
                )
            ]
            if dump_dir is not None
            else []
        )
        return {
            "status": "failed",
            "detail": (
                f"Simulated first-run failure: 3ds Max {ver} exited before main "
                "workspace became ready."
            ),
            "matched_rules": [],
            "screenshots": screenshots,
        }


class SimulatedPostConfigDriver:
    """Simulated 1-click post-configuration driver creating real artifacts in work_dir."""

    def __init__(self, state: _SimulatedState | None = None) -> None:
        self._state = state if state is not None else _SimulatedState()

    def _enu_dir(self, version: str) -> Path:
        assert self._state.work_dir is not None
        return self._state.work_dir / "Autodesk" / "3dsMax" / f"{version} - 64bit" / "ENU"

    def _backup_dir(self, version: str) -> Path:
        assert self._state.work_dir is not None
        return (
            self._state.work_dir
            / "Autodesk"
            / "3dsMax"
            / f"{version} - 64bit"
            / "ENU_backup"
        )

    def _plugins_dir(self, version: str) -> Path:
        assert self._state.work_dir is not None
        return (
            self._state.work_dir
            / "ProgramFiles"
            / "Autodesk"
            / f"3ds Max {version}"
            / "plugins"
        )

    def _sim_max_exe(self, version: str) -> Path:
        assert self._state.work_dir is not None
        return _sim_max_exe_path(self._state.work_dir, version)

    def _verify_json_path(self) -> Path:
        assert self._state.work_dir is not None
        return self._state.work_dir / "logs" / "max_verify.json"

    def _ensure_enu_and_backup(self, version: str) -> tuple[Path, Path]:
        enu_dir = self._enu_dir(version)
        backup_dir = self._backup_dir(version)
        (enu_dir / "en-US" / "UI" / "UserSettings").mkdir(parents=True, exist_ok=True)
        (enu_dir / "scripts" / "startup").mkdir(parents=True, exist_ok=True)
        (enu_dir / "usermacros").mkdir(parents=True, exist_ok=True)
        ini_path = enu_dir / "3dsmax.ini"
        if not ini_path.exists():
            ini_path.write_text("[Directories]\n[UI]\n", encoding="utf-8")
        if not backup_dir.exists():
            shutil.copytree(enu_dir, backup_dir)
        return enu_dir, backup_dir

    @staticmethod
    def _parse_steps_yaml_stdlib(text: str) -> list[dict[str, Any]]:
        """Parse simple YAML `steps:` entries using Python stdlib only."""
        steps: list[dict[str, Any]] = []
        in_steps = False
        current: dict[str, Any] | None = None
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if indent == 0 and stripped.endswith(":"):
                in_steps = stripped == "steps:"
                current = None
                continue
            if not in_steps:
                continue
            if stripped.startswith("- "):
                current = {}
                steps.append(current)
                stripped = stripped[2:].strip()
                if not stripped:
                    continue
            if current is None or ":" not in stripped:
                continue
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if not val:
                continue
            if val.startswith("[") and val.endswith("]"):
                items = [
                    item.strip().strip("'\"")
                    for item in val[1:-1].split(",")
                    if item.strip()
                ]
                current[key] = items
            elif val.lower() in ("true", "false"):
                current[key] = val.lower() == "true"
            else:
                current[key] = val.strip("'\"")
        return steps

    def load_steps(
        self, config_path: Path | None = None, version: str | None = None
    ) -> list[PostConfigStepState]:
        if config_path is not None:
            cfg = Path(config_path)
            if cfg.is_file():
                raw_entries: list[dict[str, Any]] = []
                suffix = cfg.suffix.lower()
                text = cfg.read_text(encoding="utf-8")
                if suffix == ".json":
                    parsed = json.loads(text)
                    if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                        raw_entries = [
                            e for e in parsed["steps"] if isinstance(e, dict)
                        ]
                elif suffix in (".yaml", ".yml"):
                    raw_entries = self._parse_steps_yaml_stdlib(text)
                if raw_entries:
                    steps: list[PostConfigStepState] = []
                    for entry in raw_entries:
                        if entry.get("enabled") is False:
                            continue
                        allowed_versions = entry.get("versions")
                        if (
                            version is not None
                            and isinstance(allowed_versions, list)
                            and str(version) not in {str(v) for v in allowed_versions}
                        ):
                            continue
                        step_id = entry.get("step_id") or entry.get("id")
                        name = entry.get("name")
                        category = entry.get("category")
                        if step_id and name and category:
                            steps.append(
                                PostConfigStepState(
                                    step_id=str(step_id),
                                    name=str(name),
                                    category=str(category),
                                    status="pending",
                                    detail="",
                                )
                            )
                    if steps:
                        return steps

        return [
            PostConfigStepState(
                step_id=step_id,
                name=name,
                category=category,
                status="pending",
                detail="",
            )
            for step_id, name, category in DEFAULT_POST_CONFIG_STEPS
        ]

    def run_step(
        self,
        step_id: str,
        version: str,
        *,
        assets_dir: Path | None,
        log_cb: Callable[[str, str], None],
        cancel_event: threading.Event,
        config_path: Path | None = None,
    ) -> dict[str, Any]:
        ver = str(version)
        if cancel_event.is_set():
            log_cb("WARN", f"Post-config step '{step_id}' cancelled before start.")
            return {
                "ok": False,
                "status": "aborted",
                "step_id": step_id,
                "version": ver,
                "files": [],
                "detail": f"Step '{step_id}' cancelled.",
            }

        steps = self.load_steps(config_path=config_path, version=ver)
        step = next((s for s in steps if s.step_id == step_id), None)
        if step is None:
            log_cb("ERROR", f"Unknown post-config step: {step_id}")
            return {
                "ok": False,
                "status": "failed",
                "step_id": step_id,
                "version": ver,
                "files": [],
                "detail": f"Unknown post-config step: {step_id}",
            }

        log_cb(
            "INFO",
            f"Running post-config step '{step_id}' ({step.name}) for 3ds Max {ver}...",
        )

        with self._state.lock:
            delay = self._state.step_delay_sec
            should_fail = step_id in self._state.fail_post_config_steps

        if _interruptible_wait(cancel_event, delay):
            log_cb("WARN", f"Post-config step '{step_id}' cancelled.")
            return {
                "ok": False,
                "status": "aborted",
                "step_id": step_id,
                "category": step.category,
                "version": ver,
                "files": [],
                "detail": f"Step '{step_id}' cancelled.",
            }

        if should_fail:
            log_cb("ERROR", f"Simulated failure in post-config step '{step_id}'.")
            return {
                "ok": False,
                "status": "failed",
                "step_id": step_id,
                "category": step.category,
                "version": ver,
                "files": [],
                "detail": f"Simulated failure in post-config step '{step_id}'.",
            }

        enu_dir, backup_dir = self._ensure_enu_and_backup(ver)
        assets = Path(assets_dir) if assets_dir is not None else None
        files: list[Path] = []
        detail = f"Completed {step.name}"

        if step.category == "renderer":
            plugins_dir = self._plugins_dir(ver)
            plugins_dir.mkdir(parents=True, exist_ok=True)
            lower_id = step_id.lower()
            if "corona" in lower_id:
                plugin_file = plugins_dir / "CoronaMaxUtils.dlu"
                renderer_name = "Chaos Corona"
            elif "vray" in lower_id:
                plugin_file = plugins_dir / "vrender.dlr"
                renderer_name = "V-Ray"
            else:
                plugin_file = plugins_dir / f"{step_id}.dlr"
                renderer_name = step.name
            plugin_file.write_text(
                f"# Simulated {renderer_name} plugin for 3ds Max {ver}\n"
                f"installed_at={utc_now_iso()}\n",
                encoding="utf-8",
            )
            files.append(plugin_file)
            detail = f"Installed {renderer_name} plugin ({plugin_file.name})."

        elif step.category == "theme":
            ui_dir = enu_dir / "en-US" / "UI"
            clrx_name = "custom_theme.clrx"
            if assets is not None and assets.is_dir():
                for src in sorted(assets.rglob("*")):
                    if src.is_file() and src.suffix.lower() in (".clrx", ".cuix"):
                        dst = ui_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
                        if src.suffix.lower() == ".clrx":
                            clrx_name = src.name
            if not files:
                clrx_path = ui_dir / "custom_theme.clrx"
                cuix_path = ui_dir / "custom_toolbar.cuix"
                clrx_path.write_text(
                    f"<ColorScheme version='{ver}' name='CustomDark'/>\n",
                    encoding="utf-8",
                )
                cuix_path.write_text(
                    f"<CUI version='{ver}' name='CustomToolbar'/>\n",
                    encoding="utf-8",
                )
                files.extend([clrx_path, cuix_path])

            ini_path = enu_dir / "3dsmax.ini"
            ini_text = ini_path.read_text(encoding="utf-8")
            lines = [
                line
                for line in ini_text.splitlines()
                if not line.startswith("ColorScheme=")
            ]
            lines.append(f"ColorScheme={clrx_name}")
            ini_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            detail = f"Applied UI theme ({clrx_name})."

        elif step.category == "quad_menu":
            if _is_legacy_menu_version(ver):
                target_dir = enu_dir / "en-US" / "UI"
                expected_ext = ".mnux"
                default_name = "custom_quads.mnux"
            else:
                target_dir = enu_dir / "en-US" / "UI" / "UserSettings"
                expected_ext = ".mnx"
                default_name = "custom_quads.mnx"
            target_dir.mkdir(parents=True, exist_ok=True)
            if assets is not None and assets.is_dir():
                for src in sorted(assets.rglob("*")):
                    if src.is_file() and src.suffix.lower() == expected_ext:
                        dst = target_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
            if not files:
                quad_file = target_dir / default_name
                quad_file.write_text(
                    f"<QuadMenu maxVersion='{ver}' format='{expected_ext}'/>\n",
                    encoding="utf-8",
                )
                files.append(quad_file)
            detail = f"Installed {expected_ext} quad menu(s) for 3ds Max {ver}."

        elif step.category == "hotkey":
            target_dir = enu_dir / "en-US" / "UI" / "UserSettings"
            target_dir.mkdir(parents=True, exist_ok=True)
            if assets is not None and assets.is_dir():
                for src in sorted(assets.rglob("*")):
                    if src.is_file() and src.suffix.lower() == ".hsx":
                        dst = target_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
            if not files:
                hsx_file = target_dir / "custom_hotkeys.hsx"
                hsx_file.write_text(
                    f"<Hotkeys maxVersion='{ver}'/>\n",
                    encoding="utf-8",
                )
                files.append(hsx_file)
            detail = f"Applied hotkey configuration for 3ds Max {ver}."

        elif step.category == "scripts":
            startup_dir = enu_dir / "scripts" / "startup"
            macros_dir = enu_dir / "usermacros"
            startup_dir.mkdir(parents=True, exist_ok=True)
            macros_dir.mkdir(parents=True, exist_ok=True)

            if assets is not None and assets.is_dir():
                for src in sorted(assets.rglob("*")):
                    if not src.is_file():
                        continue
                    ext = src.suffix.lower()
                    if ext in (".ms", ".mse"):
                        dst = startup_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
                    elif ext == ".mcr":
                        dst = macros_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
                    elif ext == ".mzp":
                        try:
                            extracted = _safe_extract_zip(src, startup_dir / src.stem)
                        except (ValueError, zipfile.BadZipFile) as exc:
                            log_cb("ERROR", f"Rejected unsafe archive {src.name}: {exc}")
                            return {
                                "ok": False,
                                "status": "failed",
                                "step_id": step_id,
                                "category": step.category,
                                "version": ver,
                                "files": [str(p) for p in files],
                                "detail": f"Failed to extract {src.name}: {exc}",
                            }
                        files.extend(extracted)

            verify_ms = startup_dir / "verify_install.ms"
            if not verify_ms.exists():
                verify_ms.write_text(
                    f"-- Auto-generated verification script for 3ds Max {ver}\n"
                    f"-- Target output: max_verify.json\n"
                    f'format "3ds Max {ver} verification ready\\n"\n',
                    encoding="utf-8",
                )
            if verify_ms not in files:
                files.append(verify_ms)

            custom_mcr = macros_dir / "custom_tools.mcr"
            if not custom_mcr.exists():
                custom_mcr.write_text(
                    f'macroScript CustomTools category:"MaxAuto" '
                    f'tooltip:"3ds Max {ver} Tools" ()\n',
                    encoding="utf-8",
                )
            if custom_mcr not in files:
                files.append(custom_mcr)
            detail = f"Deployed {len(files)} script/macro artifact(s)."

        elif step.category == "verify":
            verification = self.verify_installation(ver)
            verify_detail = (
                f"Verification {verification['status']}: "
                f"{len(verification['renderers'])} renderer(s), "
                f"{len(verification['scripts'])} script(s)."
            )
            log_cb("INFO", verify_detail)
            return {
                "ok": bool(verification["ok"]),
                "status": "done" if verification["ok"] else "failed",
                "step_id": step_id,
                "category": "verify",
                "version": ver,
                "files": [verification["verify_file"]],
                "backup_dir": str(backup_dir),
                "verification": verification,
                "detail": verify_detail,
            }

        log_cb("INFO", f"Completed post-config step '{step_id}' ({len(files)} file(s)).")
        return {
            "ok": True,
            "status": "done",
            "step_id": step_id,
            "category": step.category,
            "version": ver,
            "files": [str(p) for p in files],
            "backup_dir": str(backup_dir),
            "detail": detail,
        }

    def verify_installation(self, version: str) -> dict[str, Any]:
        ver = str(version)
        max_exe = self._sim_max_exe(ver)
        with self._state.lock:
            max_installed = (
                ver in self._state.installed_max_versions
            ) or max_exe.exists()

        enu_dir = self._enu_dir(ver)
        backup_dir = self._backup_dir(ver)
        plugins_dir = self._plugins_dir(ver)
        verify_path = self._verify_json_path()

        enu_exists = enu_dir.is_dir()
        backup_exists = backup_dir.is_dir()

        plugins: list[str] = []
        if plugins_dir.is_dir():
            plugins = sorted(
                p.name
                for p in plugins_dir.iterdir()
                if p.is_file() and p.suffix.lower() in (".dlu", ".dlr", ".gup")
            )

        renderers: list[str] = []
        for plugin in plugins:
            lower = plugin.lower()
            if lower == "coronamaxutils.dlu":
                if "Chaos Corona" not in renderers:
                    renderers.append("Chaos Corona")
            elif lower == "vrender.dlr":
                if "V-Ray" not in renderers:
                    renderers.append("V-Ray")
            elif lower.endswith(".dlr"):
                stem = Path(plugin).stem
                if stem not in renderers:
                    renderers.append(stem)

        scripts: list[str] = []
        scripts_root = enu_dir / "scripts"
        if scripts_root.is_dir():
            scripts = sorted(
                str(p.relative_to(scripts_root).as_posix())
                for p in scripts_root.rglob("*")
                if p.is_file() and p.suffix.lower() in (".ms", ".mse")
            )

        macros: list[str] = []
        macros_root = enu_dir / "usermacros"
        if macros_root.is_dir():
            macros = sorted(
                p.name
                for p in macros_root.iterdir()
                if p.is_file() and p.suffix.lower() == ".mcr"
            )

        ui_files: list[str] = []
        ui_root = enu_dir / "en-US" / "UI"
        if ui_root.is_dir():
            ui_files = sorted(
                str(p.relative_to(ui_root).as_posix())
                for p in ui_root.rglob("*")
                if p.is_file()
                and p.suffix.lower() in (".clrx", ".cuix", ".mnx", ".mnux", ".hsx")
            )

        checks = {
            "max_installed": max_installed,
            "enu_exists": enu_exists,
            "backup_exists": backup_exists,
            "renderers_present": len(renderers) > 0,
            "scripts_present": len(scripts) > 0,
            "ui_configured": len(ui_files) > 0,
        }

        ok = bool(max_installed or (enu_exists and (scripts or ui_files or renderers)))
        status = "ok" if ok else "failed"

        report: dict[str, Any] = {
            "ok": ok,
            "status": status,
            "version": ver,
            "max_version": ver,
            "max_installed": max_installed,
            "max_exe": str(max_exe),
            "enu_dir": str(enu_dir),
            "backup_dir": str(backup_dir),
            "verify_file": str(verify_path),
            "renderers": renderers,
            "plugins": plugins,
            "scripts": scripts,
            "macros": macros,
            "ui_files": ui_files,
            "checks": checks,
            "timestamp": utc_now_iso(),
        }
        _atomic_write_json(verify_path, report)
        return report


def create_simulated_bundle(
    *,
    work_dir: Path | str | None = None,
    initial_admin: bool = True,
    smart_app_control_state: str = "evaluation",
    defender_tamper_protected: bool = False,
    defender_rtp_enabled: bool = True,
    fail_install: bool = False,
    first_run_outcome: str = "ready",
    fail_post_config_steps: set[str] | list[str] | None = None,
    step_delay_sec: float = 0.01,
    installed_max_versions: list[str] | None = None,
    hostname: str = "SIM-CLIENT-PC",
    os_version: str = "Windows 11 Pro 24H2 (Simulated)",
    session_id: int = 1,
    free_disk_bytes: int = 120 * 1024**3,
    ahk_available: bool = True,
    third_party_av: list[str] | None = None,
    initial_exclusions: list[str] | None = None,
) -> DriverBundle:
    """Create a DriverBundle whose four simulated drivers share live synchronized state."""
    state = _SimulatedState(
        work_dir=Path(work_dir) if work_dir is not None else None,
        is_admin=bool(initial_admin),
        session_id=session_id,
        hostname=hostname,
        os_version=os_version,
        smart_app_control_state=smart_app_control_state,
        defender_rtp_enabled=bool(defender_rtp_enabled),
        defender_tamper_protected=bool(defender_tamper_protected),
        defender_exclusions=list(initial_exclusions or []),
        third_party_av=list(third_party_av or []),
        free_disk_bytes=free_disk_bytes,
        ahk_available=bool(ahk_available),
        installed_max_versions=list(installed_max_versions or []),
        fail_install=bool(fail_install),
        first_run_outcome=first_run_outcome,
        fail_post_config_steps=set(fail_post_config_steps or ()),
        step_delay_sec=step_delay_sec,
    )
    return DriverBundle(
        system=SimulatedSystemDriver(state),
        installer=SimulatedInstallerDriver(state),
        first_run=SimulatedFirstRunDriver(state),
        post_config=SimulatedPostConfigDriver(state),
    )


__all__ = [
    "SAC_STATE_TO_REGISTRY",
    "VALID_FIRST_RUN_OUTCOMES",
    "SAC_GUIDED_URI",
    "DEFENDER_THREAT_URI",
    "DEFENDER_EXCLUSIONS_URI",
    "DEFAULT_POST_CONFIG_STEPS",
    "SimulatedSystemDriver",
    "SimulatedInstallerDriver",
    "SimulatedFirstRunDriver",
    "SimulatedPostConfigDriver",
    "create_simulated_bundle",
]
