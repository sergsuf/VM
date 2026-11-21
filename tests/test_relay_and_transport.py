"""Unit & integration tests for remote.transport and remote.relay_server."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from remote.auth import PairingConfig, extract_bearer_token, verify_token
from remote.iso_catalog import IsoCatalog
from remote.iso_transfer import IsoTransferClient
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
    UpdateManifest,
    utc_now_iso,
)
from remote.relay_server import RelayServer, build_arg_parser
from remote.transport import (
    DirectTransport,
    HttpTransport,
    OperatorRelayBridge,
    RelayTransport,
    TransportAuthError,
    TransportNotFoundError,
    create_transport,
)


class _MockDirectServer:
    def __init__(self, session_id: str, token: str, catalog: IsoCatalog) -> None:
        self.session_id = session_id
        self.token = token
        self.catalog = catalog
        self.events: list[tuple[str, str, dict]] = []
        self.commands: list[dict] = []
        self.update_manifest: UpdateManifest | None = None
        self.file_requests_received = 0

        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def _check_auth(self) -> bool:
                tok = extract_bearer_token(self.headers.get("Authorization"))
                sid = self.headers.get("X-Session-Id")
                if not tok or not verify_token(parent.token, tok) or sid != parent.session_id:
                    self.send_response(401)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return False
                return True

            def _send_json(self, status: int, data: object) -> None:
                body = json.dumps(data).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:
                if not self._check_auth():
                    return
                if "/commands/poll" in self.path:
                    if not parent.commands:
                        self.send_response(204)
                        self.end_headers()
                        return
                    self._send_json(200, {"commands": list(parent.commands)})
                    return
                if self.path.endswith("/updates/latest"):
                    if parent.update_manifest is None:
                        self.send_response(204)
                        self.end_headers()
                        return
                    self._send_json(200, parent.update_manifest.to_dict())
                    return
                if self.path.startswith("/api/v1/files/") and self.path.endswith("/meta"):
                    fid = self.path.split("/")[4]
                    meta = parent.catalog.get_meta(fid)
                    if meta is None:
                        self.send_error(404)
                        return
                    self._send_json(200, meta)
                    return
                if self.path.startswith("/api/v1/files/"):
                    parent.file_requests_received += 1
                    fid = self.path.split("/")[4]
                    parent.catalog.serve_http_file(self, fid)
                    return
                self.send_error(404)

            def do_POST(self) -> None:
                if not self._check_auth():
                    return
                length = int(self.headers.get("Content-Length", "0") or "0")
                raw = self.rfile.read(length) if length else b"{}"
                body = json.loads(raw.decode("utf-8"))
                cid = self.headers.get("X-Client-Id", "")
                parent.events.append((self.path, cid, body))
                if self.path.endswith("/heartbeat"):
                    self._send_json(200, {"ok": True, "server_time": utc_now_iso()})
                elif self.path.endswith("/logs"):
                    self._send_json(200, {"ok": True, "accepted": len(body.get("entries", []))})
                else:
                    self._send_json(200, {"ok": True})

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def base_url(self) -> str:
        port = int(self._httpd.server_address[1])
        return f"http://127.0.0.1:{port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3.0)


class TestDirectTransport(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.iso_dir = Path(self._tmp.name) / "isos"
        self.iso_dir.mkdir()
        self.iso_data = os.urandom(128 * 1024)
        (self.iso_dir / "Autodesk_3ds_Max_2025_Win.iso").write_bytes(self.iso_data)
        self.catalog = IsoCatalog(self.iso_dir)
        self.spec = self.catalog.scan()[0]
        self.srv = _MockDirectServer("S-DIR1", "tok-direct-1234567890", self.catalog)

    def tearDown(self) -> None:
        self.srv.close()
        self._tmp.cleanup()

    def test_factory_and_alias(self) -> None:
        self.assertIs(HttpTransport, DirectTransport)
        cfg_d = PairingConfig("direct", self.srv.base_url, "S-DIR1", "tok-direct-1234567890")
        cfg_r = PairingConfig("relay", self.srv.base_url, "S-REL1", "tok-relay-1234567890")
        self.assertIsInstance(create_transport(cfg_d, "c1"), DirectTransport)
        self.assertIsInstance(create_transport(cfg_r, "c1"), RelayTransport)

    def test_direct_control_plane_and_range_stream(self) -> None:
        tr = DirectTransport(
            self.srv.base_url,
            "S-DIR1",
            "tok-direct-1234567890",
            client_id="client-abc",
        )
        self.assertTrue(tr.register({"hostname": "vm-01"})["ok"])
        hb = tr.heartbeat(
            SystemStatus(hostname="vm-01", os_version="Win11", is_admin=True, session_id=1)
        )
        self.assertTrue(hb["ok"])
        tr.post_state(
            SessionState(
                session_id="S-DIR1",
                client_id="client-abc",
                stage=Stage.PREP,
                status=StageStatus.IDLE,
            )
        )
        ack_logs = tr.post_logs(
            [LogEntry(ts=utc_now_iso(), level="INFO", stage="PREP", msg="hello")]
        )
        self.assertEqual(ack_logs["accepted"], 1)
        tr.post_ack(CommandAck(cmd_id="cmd-1", status="completed", detail="ok"))

        self.assertEqual(tr.poll_commands(after_seq=0, wait=0), [])
        cmd = Command(
            cmd_id="cmd-1",
            seq=1,
            cmd_type=CommandType.REFRESH_STATUS,
            payload={},
            created_at=utc_now_iso(),
        )
        self.srv.commands.append(cmd.to_dict())
        polled = tr.poll_commands(after_seq=0, wait=0)
        self.assertEqual(len(polled), 1)
        self.assertEqual(polled[0].cmd_type, CommandType.REFRESH_STATUS)

        meta = tr.get_file_meta(self.spec.file_id)
        self.assertEqual(meta["size_bytes"], len(self.iso_data))

        with tr.open_file_stream(self.spec.file_id, start=100, end=1099) as resp:
            self.assertEqual(resp.status, 206)
            self.assertEqual(resp.read_all(), self.iso_data[100:1100])

        self.assertIsNone(tr.get_latest_update())
        self.srv.update_manifest = UpdateManifest(
            version="1.2.3",
            min_runtime_version="1.0.0",
            file_id="upd-1",
            filename="app-1.2.3.zip",
            size_bytes=1234,
            sha256="a" * 64,
        )
        manifest = tr.get_latest_update()
        assert manifest is not None
        self.assertEqual(manifest.version, "1.2.3")

    def test_auth_rejection_raises_transport_auth_error(self) -> None:
        bad = DirectTransport(self.srv.base_url, "S-DIR1", "wrong-token", client_id="c1")
        with self.assertRaises(TransportAuthError):
            bad.register()


class TestRelayServerAndBridge(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.iso_dir = self.root / "isos"
        self.dest_dir = self.root / "dest"
        self.iso_dir.mkdir()
        self.dest_dir.mkdir()

        self.iso_data = os.urandom(192 * 1024)
        (self.iso_dir / "Autodesk_3ds_Max_2025_Win.iso").write_bytes(self.iso_data)
        self.catalog = IsoCatalog(self.iso_dir)
        self.spec = self.catalog.scan()[0]

        self.relay = RelayServer(host="127.0.0.1", port=0, admin_token="adm-secret")
        self.relay.start()

        self.received_events: list[tuple[str, str, dict]] = []
        self.event_lock = threading.Lock()
        self.update_manifest: UpdateManifest | None = None

        def _rec(kind: str):
            def _cb(cid: str, payload: dict) -> None:
                with self.event_lock:
                    self.received_events.append((kind, cid, payload))
            return _cb

        def _resolve_path(fid: str) -> Path | None:
            entry = self.catalog.get_entry(fid)
            return entry.path if entry is not None else None

        self.bridge = OperatorRelayBridge(
            self.relay.base_url,
            "S-REL1",
            "tok-session-1",
            on_register=_rec("register"),
            on_heartbeat=_rec("heartbeat"),
            on_state=_rec("state"),
            on_logs=_rec("logs"),
            on_ack=_rec("ack"),
            get_file_meta=self.catalog.get_meta,
            resolve_file_path=_resolve_path,
            get_latest_update=lambda: self.update_manifest,
            poll_wait=1,
        )
        self.bridge.start()

    def tearDown(self) -> None:
        self.bridge.stop()
        self.relay.shutdown()
        self._tmp.cleanup()

    def test_session_isolation_and_auth_enforcement(self) -> None:
        # Unknown session -> 404
        unknown = RelayTransport(self.relay.base_url, "S-NOPE", "tok-session-1", client_id="c1")
        with self.assertRaises(TransportNotFoundError):
            unknown.register()

        # Wrong token -> 403
        wrong_tok = RelayTransport(self.relay.base_url, "S-REL1", "wrong-tok", client_id="c1")
        with self.assertRaises(TransportAuthError):
            wrong_tok.register()

        # Register session 2 and verify session 1 token cannot access session 2
        self.assertTrue(self.relay.register_session("S-REL2", "tok-session-2"))
        cross = RelayTransport(self.relay.base_url, "S-REL2", "tok-session-1", client_id="c1")
        with self.assertRaises(TransportAuthError):
            cross.register()

        # Re-registering existing session with different token fails
        self.assertFalse(self.relay.register_session("S-REL1", "hijack-token"))

    def test_control_plane_and_long_poll_wake(self) -> None:
        rt = RelayTransport(self.relay.base_url, "S-REL1", "tok-session-1", client_id="c-win1")
        rt.register({"hostname": "client-pc"})
        rt.heartbeat(SystemStatus(hostname="client-pc", os_version="Win11", is_admin=True, session_id=1))
        rt.post_state(
            SessionState(
                session_id="S-REL1",
                client_id="c-win1",
                stage=Stage.PREP,
                status=StageStatus.IDLE,
            )
        )
        rt.post_logs([LogEntry(ts=utc_now_iso(), level="INFO", stage="PREP", msg="ready")])
        rt.post_ack(CommandAck(cmd_id="c1", status="completed", detail="ok"))

        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            with self.event_lock:
                if len(self.received_events) >= 5:
                    break
            time.sleep(0.05)

        with self.event_lock:
            kinds = [e[0] for e in self.received_events]
        self.assertEqual(kinds, ["register", "heartbeat", "state", "logs", "ack"])

        # Long-poll wakes immediately when operator enqueues command
        polled: list[Command] = []

        def _poll() -> None:
            polled.extend(rt.poll_commands(after_seq=0, wait=5))

        t = threading.Thread(target=_poll)
        t.start()
        time.sleep(0.05)
        t0 = time.monotonic()
        self.bridge.enqueue_command(
            "c-win1",
            Command(
                cmd_id="cmd-99",
                seq=0,
                cmd_type=CommandType.START_INSTALL,
                payload={"version": "2025"},
                created_at=utc_now_iso(),
            ),
        )
        t.join(timeout=2.0)
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 1.5)
        self.assertEqual(len(polled), 1)
        self.assertEqual(polled[0].cmd_id, "cmd-99")
        self.assertEqual(polled[0].seq, 1)

        # Idle poll returns empty
        self.assertEqual(rt.poll_commands(after_seq=1, wait=0), [])

    def test_o1_ram_range_file_relay_and_transfer(self) -> None:
        rt = RelayTransport(self.relay.base_url, "S-REL1", "tok-session-1", client_id="c-win1")
        meta = rt.get_file_meta(self.spec.file_id)
        self.assertEqual(meta["filename"], self.spec.filename)
        self.assertEqual(meta["size_bytes"], len(self.iso_data))

        with rt.open_file_stream(self.spec.file_id, start=0, end=65535) as resp:
            self.assertEqual(resp.status, 206)
            self.assertEqual(resp.read_all(), self.iso_data[:65536])

        with rt.open_file_stream(self.spec.file_id, start=9999999, end=10000010) as resp:
            self.assertEqual(resp.status, 416)

        # HEAD through Relay preserves actual Content-Length with empty body
        import http.client
        import urllib.parse

        parsed_relay = urllib.parse.urlsplit(self.relay.base_url)
        conn = http.client.HTTPConnection(parsed_relay.hostname, parsed_relay.port, timeout=5.0)
        try:
            conn.request(
                "HEAD",
                f"/relay/v1/sessions/S-REL1/files/{self.spec.file_id}",
                headers={"Authorization": "Bearer tok-session-1", "X-Session-Id": "S-REL1"},
            )
            head_resp = conn.getresponse()
            head_body = head_resp.read()
            self.assertEqual(head_resp.status, 200)
            self.assertEqual(int(head_resp.getheader("Content-Length") or "0"), len(self.iso_data))
            self.assertEqual(head_body, b"")

            # Malformed query param returns 400 cleanly
            conn.request(
                "GET",
                "/relay/v1/sessions/S-REL1/clients/c-win1/commands/poll?after_seq=bad&wait=0",
                headers={"Authorization": "Bearer tok-session-1", "X-Session-Id": "S-REL1"},
            )
            bad_resp = conn.getresponse()
            bad_resp.read()
            self.assertEqual(bad_resp.status, 400)
        finally:
            conn.close()

        # Full chunked download through Relay + OperatorRelayBridge
        dl = IsoTransferClient(transport=rt, chunk_size=64 * 1024)
        res = dl.download(self.spec, self.dest_dir)
        self.assertFalse(res.skipped)
        self.assertEqual(res.local_path.read_bytes(), self.iso_data)

        # Update manifest check (204 -> None, 200 -> UpdateManifest)
        self.assertIsNone(rt.get_latest_update())
        self.update_manifest = UpdateManifest(
            version="2.0.0",
            min_runtime_version="1.0.0",
            file_id="upd-2",
            filename="app-2.0.0.zip",
            size_bytes=555,
            sha256="b" * 64,
        )
        upd = rt.get_latest_update()
        assert upd is not None
        self.assertEqual(upd.version, "2.0.0")

    def test_download_url_bypasses_relay_file_proxy(self) -> None:
        catalog = self.catalog

        class _PublicHandler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:
                direct_hits.append(self.path)
                fid = self.path.rsplit("/", 1)[-1]
                catalog.serve_http_file(self, fid)

        direct_hits: list[str] = []
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), _PublicHandler)
        httpd.daemon_threads = True
        t = threading.Thread(target=httpd.serve_forever, daemon=True)
        t.start()
        try:
            port = int(httpd.server_address[1])
            rt = RelayTransport(self.relay.base_url, "S-REL1", "tok-session-1", client_id="c-win1")
            bypass_spec = IsoSpec(
                file_id=self.spec.file_id,
                filename=self.spec.filename,
                version=self.spec.version,
                size_bytes=self.spec.size_bytes,
                sha256=self.spec.sha256,
                download_url=f"http://127.0.0.1:{port}/files/{self.spec.file_id}",
            )

            # Ensure bridge never receives a file request when download_url is set
            with unittest.mock.patch.object(
                self.bridge, "_stream_file_response"
            ) as m_relay_stream:
                dl = IsoTransferClient(transport=rt, chunk_size=64 * 1024)
                res = dl.download(bypass_spec, self.dest_dir)
                m_relay_stream.assert_not_called()
                self.assertEqual(res.local_path.read_bytes(), self.iso_data)
                self.assertGreater(len(direct_hits), 0)
        finally:
            httpd.shutdown()
            httpd.server_close()
            t.join(timeout=3.0)

    def test_cli_arg_parser(self) -> None:
        args = build_arg_parser().parse_args(
            ["--host", "127.0.0.1", "--port", "9000", "--admin-token", "adm"]
        )
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 9000)
        self.assertEqual(args.admin_token, "adm")


if __name__ == "__main__":
    unittest.main()
