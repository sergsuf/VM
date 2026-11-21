"""Direct HTTP/1.1, Relay, and Operator-Bridge transports (F-07, F-08)."""

from __future__ import annotations

import http.client
import json
import os
import threading
import time
import urllib.parse
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from remote.auth import PairingConfig
from remote.iso_catalog import (
    RangeNotSatisfiableError,
    STREAM_BUFFER_SIZE,
    _if_range_matches,
    parse_range_header,
)
from remote.protocol import (
    Command,
    CommandAck,
    LogEntry,
    SessionState,
    SystemStatus,
    UpdateManifest,
)


class TransportError(RuntimeError):
    """Base error for HTTP transport failures."""

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class TransportAuthError(TransportError):
    """Raised on HTTP 401 or 403 authentication/authorization failures."""


class TransportNotFoundError(TransportError):
    """Raised on HTTP 404 Not Found responses."""


@dataclass
class FileStreamResponse:
    """Streaming HTTP file response supporting O(1)-RAM chunk iteration."""

    status: int
    headers: dict[str, str]
    _resp: http.client.HTTPResponse
    _conn: http.client.HTTPConnection

    @property
    def content_length(self) -> int | None:
        raw = self.get_header("Content-Length")
        if raw is None:
            return None
        try:
            return int(raw)
        except ValueError:
            return None

    @property
    def content_range(self) -> str | None:
        return self.get_header("Content-Range")

    def get_header(self, name: str) -> str | None:
        target = name.lower()
        for k, v in self.headers.items():
            if k.lower() == target:
                return v
        return None

    def iter_chunks(self, chunk_size: int = STREAM_BUFFER_SIZE) -> Iterator[bytes]:
        while True:
            chunk = self._resp.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def read_all(self) -> bytes:
        return b"".join(self.iter_chunks())

    def close(self) -> None:
        try:
            self._resp.close()
        finally:
            self._conn.close()

    def __enter__(self) -> FileStreamResponse:
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.close()


def _to_wire_dict(obj: Any) -> dict[str, Any]:
    if hasattr(obj, "to_dict"):
        return obj.to_dict()
    if isinstance(obj, dict):
        return dict(obj)
    raise TypeError(f"Expected dict or model with .to_dict(), got {type(obj).__name__}")


def _open_http_connection(
    base_url: str, *, timeout: float
) -> tuple[http.client.HTTPConnection, str]:
    parsed = urllib.parse.urlsplit(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise TransportError(f"Unsupported URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise TransportError(f"Invalid URL without host: {base_url!r}")
    port = parsed.port
    if parsed.scheme == "https":
        conn: http.client.HTTPConnection = http.client.HTTPSConnection(
            host, port=port or 443, timeout=timeout
        )
    else:
        conn = http.client.HTTPConnection(
            host, port=port or 80, timeout=timeout
        )
    base_path = parsed.path.rstrip("/")
    return conn, base_path


class DirectTransport:
    """Outbound HTTP/1.1 client transport targeting Operator Server (/api/v1/...)."""

    def __init__(
        self,
        server_url: str | None = None,
        session_id: str | None = None,
        token: str | None = None,
        client_id: str = "client-1",
        *,
        pairing: PairingConfig | None = None,
        base_url: str | None = None,
        relay_url: str | None = None,
        timeout: float = 15.0,
    ) -> None:
        resolved_url = server_url or base_url or relay_url or (
            pairing.server_url if pairing is not None else None
        )
        resolved_sid = session_id or (pairing.session_id if pairing is not None else None)
        resolved_tok = token or (pairing.token if pairing is not None else None)

        if not resolved_url or not resolved_sid or not resolved_tok:
            raise ValueError(
                "DirectTransport requires server_url, session_id, and token "
                "(or a PairingConfig)"
            )

        self.server_url = resolved_url.rstrip("/")
        self.session_id = resolved_sid
        self.token = resolved_tok
        self.client_id = client_id
        self.timeout = float(timeout)

    def _api_prefix(self) -> str:
        return "/api/v1"

    def _auth_headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "X-Session-Id": self.session_id,
            "X-Client-Id": self.client_id,
        }
        if extra:
            headers.update(extra)
        return headers

    def _request_json(
        self,
        method: str,
        rel_path: str,
        *,
        payload: Any | None = None,
        timeout: float | None = None,
    ) -> tuple[int, Any]:
        req_timeout = self.timeout if timeout is None else float(timeout)
        conn, base_path = _open_http_connection(self.server_url, timeout=req_timeout)
        full_path = f"{base_path}{self._api_prefix()}{rel_path}"
        headers = self._auth_headers()
        body_bytes: bytes | None = None
        if payload is not None:
            body_bytes = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"
            headers["Content-Length"] = str(len(body_bytes))

        try:
            conn.request(method, full_path, body=body_bytes, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = int(resp.status)
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError(f"HTTP {method} {full_path} failed: {exc}") from exc
        finally:
            conn.close()

        if status in (401, 403):
            raise TransportAuthError(
                f"HTTP {status} on {method} {full_path}", status_code=status
            )
        if status == 404:
            raise TransportNotFoundError(
                f"HTTP 404 on {method} {full_path}", status_code=404
            )
        if status >= 400:
            detail = raw.decode("utf-8", errors="replace")[:300]
            raise TransportError(
                f"HTTP {status} on {method} {full_path}: {detail}",
                status_code=status,
            )
        if status == 204 or not raw:
            return status, None
        try:
            return status, json.loads(raw.decode("utf-8"))
        except json.JSONDecodeError as exc:
            raise TransportError(
                f"Invalid JSON response from {method} {full_path}: {exc}",
                status_code=status,
            ) from exc

    def register(self, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = dict(payload or {})
        body.setdefault("client_id", self.client_id)
        _, data = self._request_json("POST", "/clients/register", payload=body)
        return dict(data or {"ok": True})

    def heartbeat(self, status: SystemStatus | dict[str, Any]) -> dict[str, Any]:
        cid = urllib.parse.quote(self.client_id, safe="")
        _, data = self._request_json(
            "POST", f"/clients/{cid}/heartbeat", payload=_to_wire_dict(status)
        )
        return dict(data or {"ok": True})

    def post_state(self, state: SessionState | dict[str, Any]) -> dict[str, Any]:
        cid = urllib.parse.quote(self.client_id, safe="")
        _, data = self._request_json(
            "POST", f"/clients/{cid}/state", payload=_to_wire_dict(state)
        )
        return dict(data or {"ok": True})

    send_state = post_state

    def post_logs(self, entries: Sequence[LogEntry | dict[str, Any]]) -> dict[str, Any]:
        cid = urllib.parse.quote(self.client_id, safe="")
        wire_entries = [_to_wire_dict(item) for item in entries]
        _, data = self._request_json(
            "POST", f"/clients/{cid}/logs", payload={"entries": wire_entries}
        )
        return dict(data or {"ok": True, "accepted": len(wire_entries)})

    send_logs = post_logs

    def post_ack(self, ack: CommandAck | dict[str, Any]) -> dict[str, Any]:
        cid = urllib.parse.quote(self.client_id, safe="")
        _, data = self._request_json(
            "POST", f"/clients/{cid}/ack", payload=_to_wire_dict(ack)
        )
        return dict(data or {"ok": True})

    send_ack = post_ack

    def poll_commands(self, after_seq: int = 0, wait: int = 25) -> list[Command]:
        cid = urllib.parse.quote(self.client_id, safe="")
        wait_val = max(0, int(wait))
        status, data = self._request_json(
            "GET",
            f"/clients/{cid}/commands/poll?after_seq={int(after_seq)}&wait={wait_val}",
            timeout=max(self.timeout, wait_val + 10.0),
        )
        if status == 204 or not data:
            return []
        raw_cmds = data.get("commands", [])
        return [Command.from_dict(c) for c in raw_cmds]

    def get_file_meta(self, file_id: str) -> dict[str, Any]:
        fid = urllib.parse.quote(file_id, safe="")
        _, data = self._request_json("GET", f"/files/{fid}/meta")
        if not isinstance(data, dict):
            raise TransportError("File meta response must be a JSON object")
        return data

    def open_file_stream(
        self,
        file_id: str,
        *,
        start: int | None = None,
        end: int | None = None,
        download_url: str | None = None,
        timeout: float | None = None,
    ) -> FileStreamResponse:
        req_timeout = self.timeout if timeout is None else float(timeout)
        headers: dict[str, str] = {}

        if download_url:
            parsed = urllib.parse.urlsplit(download_url)
            conn, _ = _open_http_connection(download_url, timeout=req_timeout)
            req_path = parsed.path or "/"
            if parsed.query:
                req_path = f"{req_path}?{parsed.query}"
        else:
            conn, base_path = _open_http_connection(self.server_url, timeout=req_timeout)
            fid = urllib.parse.quote(file_id, safe="")
            req_path = f"{base_path}{self._api_prefix()}/files/{fid}"
            headers.update(self._auth_headers())

        if start is not None:
            if end is not None:
                headers["Range"] = f"bytes={int(start)}-{int(end)}"
            else:
                headers["Range"] = f"bytes={int(start)}-"

        try:
            conn.request("GET", req_path, headers=headers)
            resp = conn.getresponse()
        except Exception as exc:
            conn.close()
            raise TransportError(f"GET {req_path} failed: {exc}") from exc

        status = int(resp.status)
        if status in (401, 403):
            resp.close()
            conn.close()
            raise TransportAuthError(f"HTTP {status} on GET {req_path}", status_code=status)
        if status == 404:
            resp.close()
            conn.close()
            raise TransportNotFoundError(f"HTTP 404 on GET {req_path}", status_code=404)

        resp_headers = {k: v for k, v in resp.getheaders()}
        return FileStreamResponse(
            status=status,
            headers=resp_headers,
            _resp=resp,
            _conn=conn,
        )

    def get_latest_update(self) -> UpdateManifest | None:
        status, data = self._request_json("GET", "/updates/latest")
        if status == 204 or not data:
            return None
        return UpdateManifest.from_dict(data)


HttpTransport = DirectTransport


class RelayTransport(DirectTransport):
    """Outbound HTTP/1.1 client transport routed via Relay Server (/relay/v1/sessions/{sid}/...)."""

    def _api_prefix(self) -> str:
        sid = urllib.parse.quote(self.session_id, safe="")
        return f"/relay/v1/sessions/{sid}"


def create_transport(
    pairing: PairingConfig,
    client_id: str = "client-1",
    *,
    timeout: float = 15.0,
) -> DirectTransport:
    """Instantiate DirectTransport or RelayTransport from PairingConfig."""
    if pairing.mode == "relay":
        return RelayTransport(pairing=pairing, client_id=client_id, timeout=timeout)
    return DirectTransport(pairing=pairing, client_id=client_id, timeout=timeout)


class OperatorRelayBridge:
    """Bridges an Operator instance behind NAT to a public RelayServer."""

    def __init__(
        self,
        relay_url: str,
        session_id: str,
        token: str,
        *,
        on_register: Callable[[str, dict[str, Any]], None] | None = None,
        on_heartbeat: Callable[[str, dict[str, Any]], None] | None = None,
        on_state: Callable[[str, dict[str, Any]], None] | None = None,
        on_logs: Callable[[str, dict[str, Any]], None] | None = None,
        on_ack: Callable[[str, dict[str, Any]], None] | None = None,
        get_file_meta: Callable[[str], dict[str, Any] | None] | None = None,
        resolve_file_path: Callable[[str], Path | None] | None = None,
        get_latest_update: Callable[[], dict[str, Any] | UpdateManifest | None] | None = None,
        poll_wait: int = 15,
        timeout: float = 15.0,
    ) -> None:
        self.relay_url = relay_url.rstrip("/")
        self.session_id = session_id
        self.token = token
        self._on_register = on_register
        self._on_heartbeat = on_heartbeat
        self._on_state = on_state
        self._on_logs = on_logs
        self._on_ack = on_ack
        self._get_file_meta = get_file_meta
        self._resolve_file_path = resolve_file_path
        self._get_latest_update = get_latest_update
        self._poll_wait = max(1, int(poll_wait))
        self._timeout = float(timeout)

        self._stop_event = threading.Event()
        self._threads: list[threading.Thread] = []
        self._last_event_seq = 0

    def _prefix(self) -> str:
        sid = urllib.parse.quote(self.session_id, safe="")
        return f"/relay/v1/sessions/{sid}/operator"

    def _headers(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        hdrs = {
            "Authorization": f"Bearer {self.token}",
            "X-Session-Id": self.session_id,
        }
        if extra:
            hdrs.update(extra)
        return hdrs

    def _post_json(self, rel_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        conn, base_path = _open_http_connection(self.relay_url, timeout=self._timeout)
        full_path = f"{base_path}{self._prefix()}{rel_path}"
        body = json.dumps(payload).encode("utf-8")
        headers = self._headers(
            {
                "Content-Type": "application/json; charset=utf-8",
                "Content-Length": str(len(body)),
            }
        )
        try:
            conn.request("POST", full_path, body=body, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = int(resp.status)
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError(f"OperatorRelayBridge POST {full_path} failed: {exc}") from exc
        finally:
            conn.close()

        if status in (401, 403):
            raise TransportAuthError(f"HTTP {status} on {full_path}", status_code=status)
        if status >= 400:
            raise TransportError(
                f"HTTP {status} on {full_path}: {raw.decode('utf-8', errors='replace')}",
                status_code=status,
            )
        return json.loads(raw.decode("utf-8")) if raw else {"ok": True}

    def _get_json(self, rel_path: str, *, timeout: float) -> tuple[int, Any]:
        conn, base_path = _open_http_connection(self.relay_url, timeout=timeout)
        full_path = f"{base_path}{self._prefix()}{rel_path}"
        try:
            conn.request("GET", full_path, headers=self._headers())
            resp = conn.getresponse()
            raw = resp.read()
            status = int(resp.status)
        except (OSError, http.client.HTTPException) as exc:
            raise TransportError(f"OperatorRelayBridge GET {full_path} failed: {exc}") from exc
        finally:
            conn.close()

        if status == 204:
            return 204, None
        if status in (401, 403):
            raise TransportAuthError(f"HTTP {status} on {full_path}", status_code=status)
        if status >= 400:
            raise TransportError(f"HTTP {status} on {full_path}", status_code=status)
        return status, json.loads(raw.decode("utf-8")) if raw else None

    def register_session(self) -> dict[str, Any]:
        return self._post_json("/register", {"session_id": self.session_id})

    def enqueue_command(
        self, client_id: str, cmd: Command | dict[str, Any]
    ) -> dict[str, Any]:
        cid = urllib.parse.quote(client_id, safe="")
        return self._post_json(f"/clients/{cid}/commands", _to_wire_dict(cmd))

    def start(self) -> None:
        self.register_session()
        if self._threads:
            return
        self._stop_event.clear()
        t_events = threading.Thread(
            target=self._event_loop,
            name=f"relay-bridge-events-{self.session_id}",
            daemon=True,
        )
        t_reqs = threading.Thread(
            target=self._request_loop,
            name=f"relay-bridge-reqs-{self.session_id}",
            daemon=True,
        )
        self._threads = [t_events, t_reqs]
        t_events.start()
        t_reqs.start()

    def stop(self) -> None:
        self._stop_event.set()
        for t in self._threads:
            t.join(timeout=2.0)
        self._threads.clear()

    def _event_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                status, data = self._get_json(
                    f"/events/poll?after_seq={self._last_event_seq}&wait={self._poll_wait}",
                    timeout=max(self._timeout, self._poll_wait + 10.0),
                )
                if status == 200 and data:
                    for ev in data.get("events", []):
                        self._last_event_seq = max(
                            self._last_event_seq, int(ev.get("seq", 0))
                        )
                        self._dispatch_event(ev)
            except Exception:
                if not self._stop_event.is_set():
                    time.sleep(0.5)

    def _dispatch_event(self, ev: dict[str, Any]) -> None:
        ev_type = ev.get("type")
        cid = str(ev.get("client_id", ""))
        payload = ev.get("payload") or {}
        if ev_type == "register" and self._on_register:
            self._on_register(cid, payload)
        elif ev_type == "heartbeat" and self._on_heartbeat:
            self._on_heartbeat(cid, payload)
        elif ev_type == "state" and self._on_state:
            self._on_state(cid, payload)
        elif ev_type == "logs" and self._on_logs:
            self._on_logs(cid, payload)
        elif ev_type == "ack" and self._on_ack:
            self._on_ack(cid, payload)

    def _request_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                status, data = self._get_json(
                    f"/requests/poll?wait={self._poll_wait}",
                    timeout=max(self._timeout, self._poll_wait + 10.0),
                )
                if status == 200 and data:
                    for req in data.get("requests", []):
                        threading.Thread(
                            target=self._handle_request,
                            args=(req,),
                            daemon=True,
                        ).start()
            except Exception:
                if not self._stop_event.is_set():
                    time.sleep(0.5)

    def _send_relay_response_bytes(
        self,
        req_id: str,
        status: int,
        body: bytes,
        *,
        content_type: str = "application/json; charset=utf-8",
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        conn, base_path = _open_http_connection(self.relay_url, timeout=self._timeout)
        full_path = f"{base_path}{self._prefix()}/responses/{req_id}"
        hdrs = self._headers(
            {
                "X-Relay-Status": str(status),
                "X-Relay-Content-Type": content_type,
                "Content-Length": str(len(body)),
            }
        )
        if extra_headers:
            hdrs.update(extra_headers)
        try:
            conn.request("POST", full_path, body=body, headers=hdrs)
            resp = conn.getresponse()
            resp.read()
        finally:
            conn.close()

    def _handle_request(self, req: dict[str, Any]) -> None:
        req_id = str(req.get("req_id", ""))
        kind = req.get("kind")
        if not req_id:
            return

        try:
            if kind == "meta":
                file_id = str(req.get("file_id", ""))
                meta = self._get_file_meta(file_id) if self._get_file_meta else None
                if meta is None:
                    self._send_relay_response_bytes(
                        req_id, 404, json.dumps({"error": "File not found"}).encode("utf-8")
                    )
                else:
                    self._send_relay_response_bytes(
                        req_id, 200, json.dumps(meta).encode("utf-8")
                    )
                return

            if kind == "update_latest":
                manifest = self._get_latest_update() if self._get_latest_update else None
                if manifest is None:
                    self._send_relay_response_bytes(req_id, 204, b"")
                else:
                    wire = _to_wire_dict(manifest)
                    self._send_relay_response_bytes(
                        req_id, 200, json.dumps(wire).encode("utf-8")
                    )
                return

            if kind == "file":
                self._stream_file_response(req_id, req)
                return
        except Exception:
            try:
                self._send_relay_response_bytes(
                    req_id, 500, json.dumps({"error": "Operator bridge error"}).encode("utf-8")
                )
            except Exception:
                pass

    def _stream_file_response(self, req_id: str, req: dict[str, Any]) -> None:
        file_id = str(req.get("file_id", ""))
        path = self._resolve_file_path(file_id) if self._resolve_file_path else None
        if path is None or not path.is_file():
            self._send_relay_response_bytes(
                req_id, 404, json.dumps({"error": "File not found"}).encode("utf-8")
            )
            return

        st = path.stat()
        total_size = int(st.st_size)
        etag = f'"{st.st_size:x}-{st.st_mtime_ns:x}"'
        range_hdr = req.get("range")
        if_range_hdr = req.get("if_range")
        head_only = str(req.get("method", "GET")).upper() == "HEAD"

        use_range = bool(range_hdr) and _if_range_matches(
            if_range_hdr, etag=etag, mtime_sec=st.st_mtime
        )

        if use_range and isinstance(range_hdr, str):
            try:
                start, end = parse_range_header(range_hdr, total_size)
            except RangeNotSatisfiableError:
                self._send_relay_response_bytes(
                    req_id,
                    416,
                    b"",
                    content_type="application/octet-stream",
                    extra_headers={
                        "X-Relay-Content-Range": f"bytes */{total_size}",
                        "X-Relay-Accept-Ranges": "bytes",
                    },
                )
                return
            status = 206
            length = end - start + 1
            extra = {
                "X-Relay-Content-Range": f"bytes {start}-{end}/{total_size}",
                "X-Relay-Accept-Ranges": "bytes",
                "X-Relay-ETag": etag,
            }
        else:
            status = 200
            start = 0
            length = total_size
            extra = {
                "X-Relay-Accept-Ranges": "bytes",
                "X-Relay-ETag": etag,
            }

        send_len = 0 if head_only else length
        conn, base_path = _open_http_connection(self.relay_url, timeout=60.0)
        full_path = f"{base_path}{self._prefix()}/responses/{req_id}"
        hdrs = self._headers(
            {
                "X-Relay-Status": str(status),
                "X-Relay-Content-Type": "application/octet-stream",
                "X-Relay-Content-Length": str(length),
                "Content-Length": str(send_len),
                **extra,
            }
        )
        try:
            conn.putrequest("POST", full_path)
            for k, v in hdrs.items():
                conn.putheader(k, v)
            conn.endheaders()

            if not head_only and length > 0:
                remaining = length
                with path.open("rb") as fh:
                    if start > 0:
                        fh.seek(start)
                    while remaining > 0:
                        chunk = fh.read(min(STREAM_BUFFER_SIZE, remaining))
                        if not chunk:
                            break
                        conn.send(chunk)
                        remaining -= len(chunk)

            resp = conn.getresponse()
            resp.read()
        finally:
            conn.close()


__all__ = [
    "TransportError",
    "TransportAuthError",
    "TransportNotFoundError",
    "FileStreamResponse",
    "DirectTransport",
    "HttpTransport",
    "RelayTransport",
    "OperatorRelayBridge",
    "create_transport",
]
