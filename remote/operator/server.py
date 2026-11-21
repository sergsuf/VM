"""Pure-Python Operator Control Panel Server (Direct + Relay + SSE + Range)."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
from pathlib import Path
import queue
import threading
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse
import uuid

from remote.auth import (
    PairingConfig,
    extract_bearer_token,
    generate_session_id,
    generate_token,
    verify_token,
)
from remote.drivers.simulated import SimulatedPostConfigDriver
from remote.iso_catalog import IsoCatalog
from remote.protocol import (
    Command,
    CommandType,
    PostConfigStepState,
    UpdateManifest,
    utc_now_iso,
)
from remote.transport import OperatorRelayBridge

_STATIC_DIR = Path(__file__).resolve().parent / "static"
_MAX_JSON_BODY_BYTES = 16 * 1024 * 1024
_DEFAULT_POST_CONFIG_YAML = (
    Path(__file__).resolve().parents[2] / "config" / "post_config.yaml"
)

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
}


@dataclass
class _ClientRecord:
    client_id: str
    state: dict[str, Any] = field(default_factory=dict)
    system_status: dict[str, Any] = field(default_factory=dict)
    logs: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=500))
    last_ack: dict[str, Any] | None = None
    last_seen: float = field(default_factory=time.time)
    last_seen_iso: str = field(default_factory=utc_now_iso)
    next_seq: int = 1
    commands: list[dict[str, Any]] = field(default_factory=list)
    cmd_cond: threading.Condition = field(default_factory=threading.Condition)

    def to_snapshot(self) -> dict[str, Any]:
        return {
            "client_id": self.client_id,
            "state": dict(self.state),
            "system_status": dict(self.system_status),
            "logs": list(self.logs),
            "last_ack": dict(self.last_ack) if self.last_ack else None,
            "last_seen": self.last_seen_iso,
            "connected": (time.time() - self.last_seen) < 30.0,
        }


class OperatorServer:
    """HTTP control plane for Operator Web UI, Direct clients, and Relay bridge."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8765,
        *,
        iso_dir: Path | str | None = None,
        assets_dir: Path | str | None = None,
        updates_dir: Path | str | None = None,
        mode: str = "direct",
        relay_url: str | None = None,
        session_id: str | None = None,
        token: str | None = None,
        public_url: str | None = None,
        post_config_path: Path | str | None = None,
    ) -> None:
        self.host = host
        self._requested_port = int(port)
        self.iso_dir = Path(iso_dir) if iso_dir is not None else None
        self.assets_dir = Path(assets_dir) if assets_dir is not None else None
        self.updates_dir = Path(updates_dir) if updates_dir is not None else None
        self.mode = mode
        if self.mode not in ("direct", "relay"):
            raise ValueError(f"Unsupported mode: {mode!r}")
        self.relay_url = relay_url.rstrip("/") if relay_url else None
        self.session_id = session_id or generate_session_id()
        self.token = token or generate_token()
        self.public_url = public_url.rstrip("/") if public_url else None
        self.post_config_path = (
            Path(post_config_path)
            if post_config_path is not None
            else (_DEFAULT_POST_CONFIG_YAML if _DEFAULT_POST_CONFIG_YAML.is_file() else None)
        )

        self._lock = threading.RLock()
        self._clients: dict[str, _ClientRecord] = {}
        self._sse_subscribers: list[queue.Queue[tuple[str, dict[str, Any]] | None]] = []

        self.catalog = IsoCatalog(self.iso_dir)
        if self.iso_dir is not None and self.iso_dir.exists():
            self.catalog.scan()

        self._update_manifest: UpdateManifest | None = None
        self._load_latest_update()
        self._post_config_steps: list[PostConfigStepState] = self._load_post_config_steps()

        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._relay_bridge: OperatorRelayBridge | None = None

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self._requested_port

    @property
    def base_url(self) -> str:
        connect_host = "127.0.0.1" if self.host in ("0.0.0.0", "::", "") else self.host
        return f"http://{connect_host}:{self.port}"

    @property
    def pairing(self) -> PairingConfig:
        server_url = (
            self.relay_url
            if self.mode == "relay" and self.relay_url
            else (self.public_url or self.base_url)
        )
        return PairingConfig(
            mode=self.mode,
            server_url=server_url,
            session_id=self.session_id,
            token=self.token,
        )

    def _load_post_config_steps(self) -> list[PostConfigStepState]:
        try:
            return SimulatedPostConfigDriver().load_steps(config_path=self.post_config_path)
        except Exception:
            return []

    def _load_latest_update(self) -> None:
        if self.updates_dir is None or not self.updates_dir.is_dir():
            self._update_manifest = None
            return
        manifest_file = self.updates_dir / "manifest.json"
        if not manifest_file.is_file():
            self._update_manifest = None
            return
        try:
            raw = json.loads(manifest_file.read_text(encoding="utf-8"))
            manifest = UpdateManifest.from_dict(raw)
            archive = self.updates_dir / manifest.filename
            if archive.is_file():
                self.catalog.register_file(
                    manifest.file_id,
                    archive,
                    version=manifest.version,
                    sha256=manifest.sha256,
                    filename=manifest.filename,
                )
            self._update_manifest = manifest
        except Exception:
            self._update_manifest = None

    def rescan(self) -> None:
        if self.iso_dir is not None and self.iso_dir.exists():
            self.catalog.scan()
        self._load_latest_update()
        self._post_config_steps = self._load_post_config_steps()
        self._broadcast("snapshot", self.get_snapshot())

    def _get_or_create_client(self, client_id: str) -> _ClientRecord:
        with self._lock:
            rec = self._clients.get(client_id)
            if rec is None:
                rec = _ClientRecord(client_id=client_id)
                self._clients[client_id] = rec
            rec.last_seen = time.time()
            rec.last_seen_iso = utc_now_iso()
            return rec

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        with self._lock:
            subs = list(self._sse_subscribers)
        for q in subs:
            try:
                q.put_nowait((event, data))
            except queue.Full:
                pass

    # ------------------------------------------------------------------
    # Client / Relay event callbacks
    # ------------------------------------------------------------------
    def _on_register(self, cid: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rec = self._get_or_create_client(cid)
            if isinstance(payload.get("system_status"), dict):
                rec.system_status = dict(payload["system_status"])
            if isinstance(payload.get("state"), dict):
                rec.state = dict(payload["state"])
            snap = rec.to_snapshot()
        self._broadcast("client_registered", snap)
        return {"ok": True}

    def _on_heartbeat(self, cid: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rec = self._get_or_create_client(cid)
            if isinstance(payload, dict) and payload:
                rec.system_status = dict(payload)
            hb_payload = {
                "client_id": cid,
                "system_status": dict(rec.system_status),
                "last_seen": rec.last_seen_iso,
            }
        self._broadcast("heartbeat", hb_payload)
        return {"ok": True, "server_time": utc_now_iso()}

    def _on_state(self, cid: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rec = self._get_or_create_client(cid)
            if isinstance(payload, dict):
                rec.state = dict(payload)
            state_copy = dict(rec.state)
        self._broadcast("state", {"client_id": cid, "state": state_copy})
        return {"ok": True}

    def _on_logs(self, cid: str, payload: dict[str, Any]) -> dict[str, Any]:
        entries = payload.get("entries") if isinstance(payload, dict) else []
        if not isinstance(entries, list):
            entries = []
        valid_entries = [dict(item) for item in entries if isinstance(item, dict)]
        with self._lock:
            rec = self._get_or_create_client(cid)
            for item in valid_entries:
                rec.logs.append(item)
        if valid_entries:
            self._broadcast("logs", {"client_id": cid, "entries": valid_entries})
        return {"ok": True, "accepted": len(entries)}

    def _on_ack(self, cid: str, payload: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            rec = self._get_or_create_client(cid)
            ack_copy: dict[str, Any] | None = None
            if isinstance(payload, dict):
                rec.last_ack = dict(payload)
                ack_copy = dict(rec.last_ack)
        if isinstance(payload, dict):
            ack_cmd_id = str(payload.get("cmd_id") or "")
            ack_status = str(payload.get("status") or "")
            if ack_cmd_id and ack_status in ("completed", "rejected", "failed"):
                with rec.cmd_cond:
                    rec.commands = [
                        c for c in rec.commands if c.get("cmd_id") != ack_cmd_id
                    ]
        self._broadcast("ack", {"client_id": cid, "ack": ack_copy})
        return {"ok": True}

    def _resolve_file_path(self, file_id: str) -> Path | None:
        entry = self.catalog.get_entry(file_id)
        return entry.path if entry is not None else None

    def _get_latest_update(self) -> UpdateManifest | None:
        self._load_latest_update()
        return self._update_manifest

    def enqueue_command(
        self,
        client_id: str,
        cmd_type: str,
        payload: dict[str, Any] | None = None,
        *,
        require_registered: bool = False,
    ) -> dict[str, Any]:
        ctype = CommandType(cmd_type)
        with self._lock:
            if require_registered and client_id not in self._clients:
                raise KeyError(f"Unknown client: {client_id}")
        rec = self._get_or_create_client(client_id)
        cmd_id = f"cmd-{uuid.uuid4().hex[:10]}"
        if self.mode == "relay" and self._relay_bridge is not None:
            cmd = Command(
                cmd_id=cmd_id,
                seq=0,
                cmd_type=ctype,
                payload=dict(payload or {}),
            )
            res = self._relay_bridge.enqueue_command(client_id, cmd)
            seq = int(res.get("seq", 0))
            self._broadcast(
                "command",
                {
                    "client_id": client_id,
                    "cmd_id": res.get("cmd_id", cmd_id),
                    "seq": seq,
                    "cmd_type": ctype.value,
                },
            )
            return {"ok": True, "cmd_id": res.get("cmd_id", cmd_id), "seq": seq}

        with rec.cmd_cond:
            seq = rec.next_seq
            rec.next_seq += 1
            cmd = Command(
                cmd_id=cmd_id,
                seq=seq,
                cmd_type=ctype,
                payload=dict(payload or {}),
            )
            rec.commands.append(cmd.to_dict())
            rec.cmd_cond.notify_all()
        self._broadcast(
            "command",
            {
                "client_id": client_id,
                "cmd_id": cmd_id,
                "seq": seq,
                "cmd_type": ctype.value,
            },
        )
        return {"ok": True, "cmd_id": cmd_id, "seq": seq}

    def get_snapshot(self) -> dict[str, Any]:
        self._load_latest_update()
        with self._lock:
            clients = {cid: rec.to_snapshot() for cid, rec in self._clients.items()}
        pairing = self.pairing
        return {
            "pairing": pairing.to_dict(),
            "pairing_uri": pairing.to_pairing_uri(),
            "clients": clients,
            "iso_catalog": [s.to_dict() for s in self.catalog.list_specs()],
            "post_config_steps": [s.to_dict() for s in self._post_config_steps],
            "update_manifest": (
                self._update_manifest.to_dict() if self._update_manifest else None
            ),
        }

    # ------------------------------------------------------------------
    # HTTP Server lifecycle
    # ------------------------------------------------------------------
    def start(self) -> None:
        if self._httpd is not None:
            return
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send_json(self, code: int, data: Any) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def _check_ui_post_security(self) -> bool:
                origin = self.headers.get("Origin")
                if origin:
                    origin_netloc = urlparse(origin).netloc.lower()
                    host_header = (self.headers.get("Host") or "").lower()
                    if not origin_netloc or (host_header and origin_netloc != host_header):
                        self.close_connection = True
                        self._send_json(
                            HTTPStatus.FORBIDDEN,
                            {"error": "Cross-origin request forbidden"},
                        )
                        return False

                raw_len = self.headers.get("Content-Length") or "0"
                try:
                    length = int(raw_len)
                except ValueError:
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"}
                    )
                    return False
                if length < 0:
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "Negative Content-Length"}
                    )
                    return False
                if length > _MAX_JSON_BODY_BYTES:
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"error": "Payload too large"},
                    )
                    return False

                ctype_hdr = self.headers.get("Content-Type")
                ctype_main = (ctype_hdr or "").split(";", 1)[0].strip().lower()
                if (ctype_hdr is not None or length > 0) and ctype_main != "application/json":
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                        {"error": "Content-Type must be application/json"},
                    )
                    return False
                return True

            def _read_json(self) -> dict[str, Any] | None:
                raw_len = self.headers.get("Content-Length") or "0"
                try:
                    length = int(raw_len)
                except ValueError:
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"}
                    )
                    return None
                if length < 0:
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "Negative Content-Length"}
                    )
                    return None
                if length > _MAX_JSON_BODY_BYTES:
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"error": "Payload too large"},
                    )
                    return None
                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                    return data if isinstance(data, dict) else {}
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
                    return None

            def _check_auth(self) -> bool:
                token = extract_bearer_token(self.headers.get("Authorization"))
                if token is None or not verify_token(parent.token, token):
                    self._send_json(
                        HTTPStatus.UNAUTHORIZED, {"error": "Invalid or missing bearer token"}
                    )
                    return False
                sid = self.headers.get("X-Session-Id")
                if sid != parent.session_id:
                    self._send_json(
                        HTTPStatus.FORBIDDEN, {"error": "Invalid session ID"}
                    )
                    return False
                return True

            def _serve_static(self, rel_name: str) -> None:
                safe_name = rel_name.lstrip("/")
                static_root = _STATIC_DIR.resolve()
                target = (static_root / safe_name).resolve()
                if not target.is_relative_to(static_root) or not target.is_file():
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Static file not found"})
                    return
                ctype = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
                data = target.read_bytes()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_HEAD(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                if len(parts) == 4 and parts[:3] == ["api", "v1", "files"]:
                    if not self._check_auth():
                        return
                    parent.catalog.serve_http_file(self, parts[3], head_only=True)
                    return
                self.send_response(HTTPStatus.NOT_FOUND)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_GET(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                parts = [p for p in path.split("/") if p]

                if path == "/":
                    self._serve_static("index.html")
                    return
                if len(parts) >= 2 and parts[0] == "static":
                    self._serve_static("/".join(parts[1:]))
                    return
                if path == "/api/v1/ui/snapshot":
                    self._send_json(HTTPStatus.OK, parent.get_snapshot())
                    return
                if path == "/api/v1/ui/pairing/connect.json":
                    body = parent.pairing.to_connect_json().encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header(
                        "Content-Disposition", 'attachment; filename="connect.json"'
                    )
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if path == "/api/v1/ui/events":
                    self._handle_sse()
                    return

                # Authenticated client / file / update endpoints
                if len(parts) == 5 and parts[:3] == ["api", "v1", "files"] and parts[4] == "meta":
                    if not self._check_auth():
                        return
                    meta = parent.catalog.get_meta(parts[3])
                    if meta is None:
                        self._send_json(HTTPStatus.NOT_FOUND, {"error": "File not found"})
                        return
                    self._send_json(HTTPStatus.OK, meta)
                    return

                if len(parts) == 4 and parts[:3] == ["api", "v1", "files"]:
                    if not self._check_auth():
                        return
                    parent.catalog.serve_http_file(self, parts[3], head_only=False)
                    return

                if path == "/api/v1/updates/latest":
                    if not self._check_auth():
                        return
                    manifest = parent._get_latest_update()
                    if manifest is None:
                        self.send_response(HTTPStatus.NO_CONTENT)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self._send_json(HTTPStatus.OK, manifest.to_dict())
                    return

                if (
                    len(parts) == 6
                    and parts[:3] == ["api", "v1", "clients"]
                    and parts[4] == "commands"
                    and parts[5] == "poll"
                ):
                    if not self._check_auth():
                        return
                    cid = unquote(parts[3])
                    qs = parse_qs(parsed.query)
                    try:
                        after_seq = int(qs.get("after_seq", ["0"])[0])
                        wait_val = float(qs.get("wait", ["25"])[0])
                        if math.isnan(wait_val) or math.isinf(wait_val):
                            raise ValueError("Invalid wait")
                    except (ValueError, TypeError):
                        self._send_json(
                            HTTPStatus.BAD_REQUEST,
                            {"error": "Invalid after_seq or wait parameter"},
                        )
                        return
                    wait_sec = max(0.0, min(60.0, wait_val))
                    rec = parent._get_or_create_client(cid)
                    deadline = time.monotonic() + wait_sec
                    with rec.cmd_cond:
                        effective_after = 0 if after_seq >= rec.next_seq else after_seq
                        cmds = [c for c in rec.commands if c["seq"] > effective_after]
                        while not cmds:
                            remaining = deadline - time.monotonic()
                            if remaining <= 0:
                                break
                            rec.cmd_cond.wait(timeout=remaining)
                            cmds = [c for c in rec.commands if c["seq"] > effective_after]
                    if not cmds:
                        self.send_response(HTTPStatus.NO_CONTENT)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    self._send_json(HTTPStatus.OK, {"commands": cmds})
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_POST(self) -> None:  # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                parts = [p for p in path.split("/") if p]

                if path == "/api/v1/ui/rescan":
                    if not self._check_ui_post_security():
                        return
                    parent.rescan()
                    self._send_json(HTTPStatus.OK, {"ok": True})
                    return

                if (
                    len(parts) == 6
                    and parts[:4] == ["api", "v1", "ui", "clients"]
                    and parts[5] == "command"
                ):
                    if not self._check_ui_post_security():
                        return
                    body = self._read_json()
                    if body is None:
                        return
                    cid = unquote(parts[4])
                    cmd_type = str(body.get("cmd_type") or "")
                    payload = body.get("payload") or {}
                    try:
                        res = parent.enqueue_command(
                            cid, cmd_type, payload, require_registered=True
                        )
                        self._send_json(HTTPStatus.OK, res)
                    except KeyError as exc:
                        self._send_json(
                            HTTPStatus.NOT_FOUND, {"ok": False, "error": str(exc)}
                        )
                    except ValueError as exc:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
                        )
                    return

                # Authenticated client endpoints
                if path == "/api/v1/clients/register":
                    if not self._check_auth():
                        return
                    body = self._read_json()
                    if body is None:
                        return
                    cid = str(
                        self.headers.get("X-Client-Id")
                        or body.get("client_id")
                        or "client-1"
                    )
                    self._send_json(HTTPStatus.OK, parent._on_register(cid, body))
                    return

                if len(parts) == 5 and parts[:3] == ["api", "v1", "clients"]:
                    if not self._check_auth():
                        return
                    body = self._read_json()
                    if body is None:
                        return
                    cid, action = unquote(parts[3]), parts[4]
                    if action == "heartbeat":
                        self._send_json(HTTPStatus.OK, parent._on_heartbeat(cid, body))
                        return
                    if action == "state":
                        self._send_json(HTTPStatus.OK, parent._on_state(cid, body))
                        return
                    if action == "logs":
                        self._send_json(HTTPStatus.OK, parent._on_logs(cid, body))
                        return
                    if action == "ack":
                        self._send_json(HTTPStatus.OK, parent._on_ack(cid, body))
                        return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def _handle_sse(self) -> None:
                q: queue.Queue[tuple[str, dict[str, Any]] | None] = queue.Queue(
                    maxsize=256
                )
                with parent._lock:
                    parent._sse_subscribers.append(q)

                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "keep-alive")
                self.end_headers()

                try:
                    init_payload = json.dumps(
                        parent.get_snapshot(), ensure_ascii=False
                    )
                    self.wfile.write(
                        f"event: snapshot\ndata: {init_payload}\n\n".encode("utf-8")
                    )
                    self.wfile.flush()

                    while True:
                        try:
                            item = q.get(timeout=15.0)
                        except queue.Empty:
                            self.wfile.write(b": keepalive\n\n")
                            self.wfile.flush()
                            continue
                        if item is None:
                            break
                        ev_name, ev_data = item
                        encoded = json.dumps(ev_data, ensure_ascii=False)
                        frame = f"event: {ev_name}\ndata: {encoded}\n\n".encode("utf-8")
                        self.wfile.write(frame)
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass
                finally:
                    with parent._lock:
                        if q in parent._sse_subscribers:
                            parent._sse_subscribers.remove(q)

        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._httpd = _Server((self.host, self._requested_port), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="operator-server", daemon=True
        )
        self._thread.start()

        if self.mode == "relay":
            if not self.relay_url:
                raise ValueError("relay_url is required when mode == 'relay'")
            self._relay_bridge = OperatorRelayBridge(
                self.relay_url,
                self.session_id,
                self.token,
                on_register=self._on_register,
                on_heartbeat=self._on_heartbeat,
                on_state=self._on_state,
                on_logs=self._on_logs,
                on_ack=self._on_ack,
                get_file_meta=self.catalog.get_meta,
                resolve_file_path=self._resolve_file_path,
                get_latest_update=self._get_latest_update,
            )
            self._relay_bridge.start()

    def serve_forever(self) -> None:
        self.start()
        if self._thread is not None:
            self._thread.join()

    def shutdown(self) -> None:
        if self._relay_bridge is not None:
            self._relay_bridge.stop()
            self._relay_bridge = None
        with self._lock:
            subs = list(self._sse_subscribers)
        for q in subs:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> OperatorServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()
