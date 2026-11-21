"""Built-in pure-Python HTTP/1.1 relay server for NAT traversal (F-08)."""

from __future__ import annotations

import argparse
import json
import queue
import signal
import sys
import threading
import urllib.parse
import uuid
from collections import deque
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from remote.auth import extract_bearer_token, verify_token
from remote.protocol import utc_now_iso

STREAM_CHUNK_SIZE: int = 64 * 1024
DEFAULT_MAX_POLL_WAIT: int = 60
MAX_EVENTS_PER_SESSION: int = 2000
MAX_COMMANDS_PER_CLIENT: int = 500


@dataclass
class _RelayClientQueue:
    client_id: str
    cond: threading.Condition = field(default_factory=threading.Condition)
    commands: list[dict[str, Any]] = field(default_factory=list)
    next_seq: int = 1


@dataclass
class _RelayStreamPipe:
    req_id: str
    kind: str
    headers_ready: threading.Event = field(default_factory=threading.Event)
    closed: threading.Event = field(default_factory=threading.Event)
    status: int = 200
    headers: dict[str, str] = field(default_factory=dict)
    chunks: queue.Queue[bytes | None] = field(default_factory=lambda: queue.Queue(maxsize=8))


class _RelaySession:
    def __init__(self, session_id: str, token: str) -> None:
        self.session_id = session_id
        self.token = token
        self.lock = threading.RLock()
        self.clients: dict[str, _RelayClientQueue] = {}
        self.event_cond = threading.Condition(self.lock)
        self.events: list[dict[str, Any]] = []
        self.next_event_seq: int = 1
        self.req_cond = threading.Condition(self.lock)
        self.pending_requests: deque[dict[str, Any]] = deque()
        self.inflight_responses: dict[str, _RelayStreamPipe] = {}

    def get_or_create_client(self, client_id: str) -> _RelayClientQueue:
        with self.lock:
            cq = self.clients.get(client_id)
            if cq is None:
                cq = _RelayClientQueue(client_id=client_id)
                self.clients[client_id] = cq
            return cq

    def append_event(self, event_type: str, client_id: str, payload: Any) -> int:
        with self.event_cond:
            seq = self.next_event_seq
            self.next_event_seq += 1
            self.events.append(
                {
                    "seq": seq,
                    "type": event_type,
                    "client_id": client_id,
                    "payload": payload,
                    "ts": utc_now_iso(),
                }
            )
            if len(self.events) > MAX_EVENTS_PER_SESSION:
                del self.events[: len(self.events) - MAX_EVENTS_PER_SESSION]
            self.event_cond.notify_all()
            return seq

    def poll_events(self, after_seq: int, wait_sec: float) -> list[dict[str, Any]]:
        deadline = threading.Event()
        with self.event_cond:
            matched = [ev for ev in self.events if ev["seq"] > after_seq]
            if matched or wait_sec <= 0:
                return matched
            self.event_cond.wait(timeout=wait_sec)
            return [ev for ev in self.events if ev["seq"] > after_seq]
        _ = deadline  # unused

    def enqueue_command(self, client_id: str, cmd: dict[str, Any]) -> dict[str, Any]:
        cq = self.get_or_create_client(client_id)
        with cq.cond:
            stored = dict(cmd)
            seq = cq.next_seq
            cq.next_seq += 1
            stored["seq"] = seq
            if not stored.get("cmd_id"):
                stored["cmd_id"] = f"cmd-{uuid.uuid4().hex[:12]}"
            if not stored.get("created_at"):
                stored["created_at"] = utc_now_iso()
            cq.commands.append(stored)
            if len(cq.commands) > MAX_COMMANDS_PER_CLIENT:
                del cq.commands[: len(cq.commands) - MAX_COMMANDS_PER_CLIENT]
            cq.cond.notify_all()
            return stored

    def poll_commands(self, client_id: str, after_seq: int, wait_sec: float) -> list[dict[str, Any]]:
        cq = self.get_or_create_client(client_id)
        with cq.cond:
            matched = [c for c in cq.commands if int(c.get("seq", 0)) > after_seq]
            if matched or wait_sec <= 0:
                return matched
            cq.cond.wait(timeout=wait_sec)
            return [c for c in cq.commands if int(c.get("seq", 0)) > after_seq]

    def create_request(self, req_payload: dict[str, Any]) -> _RelayStreamPipe:
        req_id = uuid.uuid4().hex
        pipe = _RelayStreamPipe(req_id=req_id, kind=str(req_payload.get("kind", "file")))
        item = dict(req_payload)
        item["req_id"] = req_id
        with self.req_cond:
            self.inflight_responses[req_id] = pipe
            self.pending_requests.append(item)
            self.req_cond.notify_all()
        return pipe

    def poll_requests(self, wait_sec: float, max_items: int = 8) -> list[dict[str, Any]]:
        with self.req_cond:
            if not self.pending_requests and wait_sec > 0:
                self.req_cond.wait(timeout=wait_sec)
            items: list[dict[str, Any]] = []
            while self.pending_requests and len(items) < max_items:
                items.append(self.pending_requests.popleft())
            return items

    def pop_pipe(self, req_id: str) -> None:
        with self.lock:
            self.inflight_responses.pop(req_id, None)


class _ThreadingRelayHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, server_address: tuple[str, int], relay: RelayServer) -> None:
        self.relay = relay
        super().__init__(server_address, _RelayRequestHandler)


class _RelayRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        # Suppress noisy default stderr HTTP access logs during tests/runtime.
        return

    @property
    def relay(self) -> RelayServer:
        return self.server.relay  # type: ignore[attr-defined]

    def _send_json(self, status: int, payload: Any) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_empty(self, status: int) -> None:
        self.send_response(status)
        if status != 204:
            self.send_header("Content-Length", "0")
        self.end_headers()

    def _read_json_body(self) -> dict[str, Any]:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return {}
        try:
            length = int(raw_len)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        raw = self.rfile.read(length)
        if not raw:
            return {}
        parsed = json.loads(raw.decode("utf-8"))
        if not isinstance(parsed, dict):
            raise ValueError("JSON body must be an object")
        return parsed

    def _parse_query(self) -> tuple[str, dict[str, list[str]]]:
        parsed = urllib.parse.urlsplit(self.path)
        return parsed.path, urllib.parse.parse_qs(parsed.query)

    def _authenticate_session(self, session_id: str) -> _RelaySession | None:
        token = extract_bearer_token(self.headers.get("Authorization"))
        if not token:
            self._send_json(401, {"error": "Missing or invalid Bearer token"})
            return None

        hdr_sid = self.headers.get("X-Session-Id")
        if hdr_sid is not None and hdr_sid != session_id:
            self._send_json(403, {"error": "X-Session-Id mismatch"})
            return None

        session = self.relay.get_session(session_id)
        if session is None:
            self._send_json(404, {"error": f"Unknown session {session_id!r}"})
            return None

        if not verify_token(session.token, token):
            self._send_json(403, {"error": "Invalid session token"})
            return None
        return session

    def _authenticate_admin(self) -> bool:
        expected = self.relay.admin_token
        if not expected:
            return True
        token = extract_bearer_token(self.headers.get("Authorization"))
        if not token:
            self._send_json(401, {"error": "Missing admin token"})
            return False
        if not verify_token(expected, token):
            self._send_json(403, {"error": "Invalid admin token"})
            return False
        return True

    def _handle_proxied_request(
        self,
        session: _RelaySession,
        req_payload: dict[str, Any],
        *,
        head_only: bool = False,
    ) -> None:
        pipe = session.create_request(req_payload)
        try:
            if not pipe.headers_ready.wait(timeout=30.0):
                self._send_json(504, {"error": "Operator bridge timed out"})
                return

            self.send_response(pipe.status)
            if pipe.status == 204:
                self.end_headers()
                return

            for k, v in pipe.headers.items():
                self.send_header(k, v)
            self.end_headers()

            if head_only:
                pipe.closed.set()
                return

            while True:
                try:
                    chunk = pipe.chunks.get(timeout=30.0)
                except queue.Empty:
                    break
                if chunk is None:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    pipe.closed.set()
                    break
        finally:
            pipe.closed.set()
            session.pop_pipe(pipe.req_id)

    def do_HEAD(self) -> None:
        path, _ = self._parse_query()
        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/") if p]
        if len(parts) == 6 and parts[:3] == ["relay", "v1", "sessions"] and parts[4] == "files":
            session_id, file_id = parts[3], parts[5]
            session = self._authenticate_session(session_id)
            if session is None:
                return
            self._handle_proxied_request(
                session,
                {
                    "kind": "file",
                    "file_id": file_id,
                    "method": "HEAD",
                    "range": self.headers.get("Range"),
                    "if_range": self.headers.get("If-Range"),
                },
                head_only=True,
            )
            return
        self._send_empty(404)

    def do_GET(self) -> None:
        path, query = self._parse_query()
        if path in ("/healthz", "/relay/v1/health"):
            self._send_json(200, {"ok": True})
            return

        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/") if p]

        if parts == ["relay", "v1", "admin", "sessions"]:
            if not self._authenticate_admin():
                return
            self._send_json(200, {"sessions": self.relay.list_session_ids()})
            return

        if len(parts) < 5 or parts[:3] != ["relay", "v1", "sessions"]:
            self._send_json(404, {"error": "Not found"})
            return

        session_id = parts[3]
        session = self._authenticate_session(session_id)
        if session is None:
            return

        rest = parts[4:]

        # GET /relay/v1/sessions/{sid}/clients/{cid}/commands/poll
        if len(rest) == 4 and rest[0] == "clients" and rest[2:] == ["commands", "poll"]:
            client_id = rest[1]
            try:
                after_seq = int(query.get("after_seq", ["0"])[0])
                wait_sec = min(
                    float(DEFAULT_MAX_POLL_WAIT),
                    max(0.0, float(query.get("wait", ["25"])[0])),
                )
            except (ValueError, TypeError):
                self._send_json(400, {"error": "Invalid after_seq or wait parameter"})
                return
            cmds = session.poll_commands(client_id, after_seq, wait_sec)
            if not cmds:
                self._send_empty(204)
                return
            self._send_json(200, {"commands": cmds})
            return

        # GET /relay/v1/sessions/{sid}/files/{file_id}/meta
        if len(rest) == 3 and rest[0] == "files" and rest[2] == "meta":
            file_id = rest[1]
            self._handle_proxied_request(
                session, {"kind": "meta", "file_id": file_id}
            )
            return

        # GET /relay/v1/sessions/{sid}/files/{file_id}
        if len(rest) == 2 and rest[0] == "files":
            file_id = rest[1]
            self._handle_proxied_request(
                session,
                {
                    "kind": "file",
                    "file_id": file_id,
                    "method": "GET",
                    "range": self.headers.get("Range"),
                    "if_range": self.headers.get("If-Range"),
                },
            )
            return

        # GET /relay/v1/sessions/{sid}/updates/latest
        if rest == ["updates", "latest"]:
            self._handle_proxied_request(session, {"kind": "update_latest"})
            return

        # GET /relay/v1/sessions/{sid}/operator/events/poll
        if rest == ["operator", "events", "poll"]:
            try:
                after_seq = int(query.get("after_seq", ["0"])[0])
                wait_sec = min(
                    float(DEFAULT_MAX_POLL_WAIT),
                    max(0.0, float(query.get("wait", ["20"])[0])),
                )
            except (ValueError, TypeError):
                self._send_json(400, {"error": "Invalid after_seq or wait parameter"})
                return
            events = session.poll_events(after_seq, wait_sec)
            if not events:
                self._send_empty(204)
                return
            self._send_json(200, {"events": events})
            return

        # GET /relay/v1/sessions/{sid}/operator/requests/poll
        if rest == ["operator", "requests", "poll"]:
            try:
                wait_sec = min(
                    float(DEFAULT_MAX_POLL_WAIT),
                    max(0.0, float(query.get("wait", ["20"])[0])),
                )
            except (ValueError, TypeError):
                self._send_json(400, {"error": "Invalid wait parameter"})
                return
            reqs = session.poll_requests(wait_sec)
            if not reqs:
                self._send_empty(204)
                return
            self._send_json(200, {"requests": reqs})
            return

        self._send_json(404, {"error": "Unknown relay endpoint"})

    def do_POST(self) -> None:
        path, _ = self._parse_query()
        parts = [urllib.parse.unquote(p) for p in path.strip("/").split("/") if p]

        # Optional: POST /relay/v1/sessions/register
        if parts == ["relay", "v1", "sessions", "register"]:
            try:
                body = self._read_json_body()
            except (json.JSONDecodeError, ValueError) as exc:
                self._send_json(400, {"error": str(exc)})
                return
            sid = str(body.get("session_id") or self.headers.get("X-Session-Id") or "").strip()
            tok = str(
                body.get("token")
                or extract_bearer_token(self.headers.get("Authorization"))
                or ""
            ).strip()
            if not sid or not tok:
                self._send_json(400, {"error": "session_id and token required"})
                return
            ok = self.relay.register_session(sid, tok)
            if not ok:
                self._send_json(403, {"error": "Session token mismatch"})
                return
            self._send_json(200, {"ok": True, "session_id": sid})
            return

        if len(parts) < 5 or parts[:3] != ["relay", "v1", "sessions"]:
            self._send_json(404, {"error": "Not found"})
            return

        session_id = parts[3]
        rest = parts[4:]

        # POST /relay/v1/sessions/{sid}/operator/register
        if rest == ["operator", "register"]:
            token = extract_bearer_token(self.headers.get("Authorization"))
            if not token:
                self._send_json(401, {"error": "Missing or invalid Bearer token"})
                return
            hdr_sid = self.headers.get("X-Session-Id")
            if hdr_sid is not None and hdr_sid != session_id:
                self._send_json(403, {"error": "X-Session-Id mismatch"})
                return
            if not self.relay.register_session(session_id, token):
                self._send_json(403, {"error": "Session already exists with different token"})
                return
            self._send_json(200, {"ok": True, "session_id": session_id})
            return

        session = self._authenticate_session(session_id)
        if session is None:
            return

        # POST /relay/v1/sessions/{sid}/operator/responses/{req_id} (streamed body!)
        if len(rest) == 3 and rest[0] == "operator" and rest[1] == "responses":
            req_id = rest[2]
            with session.lock:
                pipe = session.inflight_responses.get(req_id)
            if pipe is None:
                self._drain_request_body()
                self._send_json(404, {"error": f"Unknown req_id {req_id!r}"})
                return

            try:
                relay_status = int(self.headers.get("X-Relay-Status", "200"))
                content_len = int(self.headers.get("Content-Length", "0") or "0")
                if content_len < 0:
                    raise ValueError("negative Content-Length")
            except (ValueError, TypeError):
                self.close_connection = True
                self._send_json(400, {"error": "Invalid X-Relay-Status or Content-Length header"})
                return

            headers: dict[str, str] = {}
            if self.headers.get("X-Relay-Content-Type"):
                headers["Content-Type"] = self.headers["X-Relay-Content-Type"]
            if self.headers.get("X-Relay-Content-Range"):
                headers["Content-Range"] = self.headers["X-Relay-Content-Range"]
            if self.headers.get("X-Relay-Accept-Ranges"):
                headers["Accept-Ranges"] = self.headers["X-Relay-Accept-Ranges"]
            if self.headers.get("X-Relay-ETag"):
                headers["ETag"] = self.headers["X-Relay-ETag"]
            if relay_status != 204:
                advertised_len = self.headers.get("X-Relay-Content-Length")
                headers["Content-Length"] = advertised_len if advertised_len is not None else str(content_len)

            pipe.status = relay_status
            pipe.headers = headers
            pipe.headers_ready.set()

            remaining = content_len
            try:
                while remaining > 0:
                    to_read = min(STREAM_CHUNK_SIZE, remaining)
                    chunk = self.rfile.read(to_read)
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    while not pipe.closed.is_set():
                        try:
                            pipe.chunks.put(chunk, timeout=0.5)
                            break
                        except queue.Full:
                            continue
                    if pipe.closed.is_set():
                        self.close_connection = True
                        break
            finally:
                try:
                    pipe.chunks.put(None, timeout=2.0)
                except queue.Full:
                    pass

            self._send_json(200, {"ok": True})
            return

        # JSON-body endpoints
        try:
            body = self._read_json_body()
        except (json.JSONDecodeError, ValueError) as exc:
            self._send_json(400, {"error": str(exc)})
            return

        # POST /relay/v1/sessions/{sid}/clients/register
        if rest == ["clients", "register"]:
            cid = str(body.get("client_id") or self.headers.get("X-Client-Id") or "client-1")
            session.get_or_create_client(cid)
            session.append_event("register", cid, body)
            self._send_json(200, {"ok": True})
            return

        # POST /relay/v1/sessions/{sid}/clients/{cid}/(heartbeat|state|logs|ack)
        if len(rest) == 3 and rest[0] == "clients":
            cid, action = rest[1], rest[2]
            if action == "heartbeat":
                session.append_event("heartbeat", cid, body)
                self._send_json(200, {"ok": True, "server_time": utc_now_iso()})
                return
            if action == "state":
                session.append_event("state", cid, body)
                self._send_json(200, {"ok": True})
                return
            if action == "logs":
                entries = body.get("entries")
                if not isinstance(entries, list):
                    entries = body.get("logs")
                count = len(entries) if isinstance(entries, list) else 0
                session.append_event("logs", cid, body)
                self._send_json(200, {"ok": True, "accepted": count})
                return
            if action == "ack":
                session.append_event("ack", cid, body)
                self._send_json(200, {"ok": True})
                return

        # POST /relay/v1/sessions/{sid}/operator/clients/{cid}/commands
        if len(rest) == 4 and rest[0] == "operator" and rest[1] == "clients" and rest[3] == "commands":
            cid = rest[2]
            stored = session.enqueue_command(cid, body)
            self._send_json(
                200,
                {"ok": True, "cmd_id": stored["cmd_id"], "seq": stored["seq"]},
            )
            return

        self._send_json(404, {"error": "Unknown relay endpoint"})

    def _drain_request_body(self) -> None:
        raw_len = self.headers.get("Content-Length")
        if not raw_len:
            return
        try:
            remaining = int(raw_len)
        except ValueError:
            return
        while remaining > 0:
            chunk = self.rfile.read(min(STREAM_CHUNK_SIZE, remaining))
            if not chunk:
                break
            remaining -= len(chunk)


class RelayServer:
    """Thread-safe pure-Python HTTP/1.1 Relay Server."""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8766,
        *,
        admin_token: str = "",
    ) -> None:
        self._host = host
        self._port = int(port)
        self._admin_token = admin_token
        self._lock = threading.RLock()
        self._sessions: dict[str, _RelaySession] = {}
        self._httpd: _ThreadingRelayHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def admin_token(self) -> str:
        return self._admin_token

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self._port

    @property
    def base_url(self) -> str:
        host = "127.0.0.1" if self._host in ("0.0.0.0", "") else self._host
        return f"http://{host}:{self.port}"

    def register_session(self, session_id: str, token: str) -> bool:
        with self._lock:
            existing = self._sessions.get(session_id)
            if existing is None:
                self._sessions[session_id] = _RelaySession(session_id, token)
                return True
            return verify_token(existing.token, token)

    def get_session(self, session_id: str) -> _RelaySession | None:
        with self._lock:
            return self._sessions.get(session_id)

    def list_session_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._sessions.keys())

    def start(self) -> None:
        if self._httpd is not None:
            return
        self._httpd = _ThreadingRelayHTTPServer((self._host, self._port), self)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever,
            name="max-relay-server",
            daemon=True,
        )
        self._thread.start()

    def serve_forever(self) -> None:
        if self._httpd is None:
            self._httpd = _ThreadingRelayHTTPServer((self._host, self._port), self)
        self._httpd.serve_forever()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="max-relay",
        description="Built-in pure-Python NAT-traversal relay server for 3ds Max deployment.",
    )
    parser.add_argument("--host", default="0.0.0.0", help="Bind host (default: 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8766, help="Bind port (default: 8766)")
    parser.add_argument("--admin-token", default="", help="Optional admin bearer token")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    server = RelayServer(host=args.host, port=args.port, admin_token=args.admin_token)
    server.start()
    print(f"max-relay listening on http://{args.host}:{server.port}", flush=True)

    stop_event = threading.Event()

    def _handle_sig(_signum: int, _frame: Any) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    try:
        while not stop_event.is_set():
            stop_event.wait(timeout=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())


__all__ = [
    "RelayServer",
    "build_arg_parser",
    "main",
]
