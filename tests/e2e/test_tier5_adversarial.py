"""Tier 5 — White-Box Adversarial Hardening Tests (C1-01..C1-06, F-C2-01..F-C2-13).

Pure Python standard-library ``unittest.TestCase`` suite covering all 19
verified adversarial findings from Challenger 1 and Challenger 2.
All imports are strictly Python stdlib + ``tests.e2e.*``; production modules
are resolved dynamically via ``require_module`` / ``require_attr``.
"""

from __future__ import annotations

import hashlib
import http.client
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.parse
import zipfile

from tests.e2e.helpers import feature, require_module


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class _MockFileStream:
    """Minimal FileStreamLike for testing IsoTransferClient edge cases."""

    def __init__(self, status: int, chunks: list[bytes]) -> None:
        self.status = status
        self._chunks = chunks
        self.closed = False

    def iter_chunks(self, chunk_size: int = 65536):  # noqa: ARG002
        for ch in self._chunks:
            yield ch

    def close(self) -> None:
        self.closed = True

    def __enter__(self) -> _MockFileStream:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class _FakeTransport:
    """Minimal transport stub returning preconfigured streams."""

    def __init__(self, handler) -> None:
        self._handler = handler
        self.calls = 0

    def open_file_stream(
        self,
        file_id: str,  # noqa: ARG002
        *,
        start: int = 0,
        end: int | None = None,
        download_url: str | None = None,  # noqa: ARG002
        **kwargs,  # noqa: ARG002
    ):
        self.calls += 1
        return self._handler(self.calls, start, end)


class TestTier5Adversarial(unittest.TestCase):
    """Tier 5 white-box adversarial tests for all 19 verified findings."""

    # ------------------------------------------------------------------
    # C1-01: Updater SemVer & Path Traversal
    # ------------------------------------------------------------------
    @feature("F-18", "F-24")
    def test_c1_01_updater_path_traversal_and_semver(self) -> None:
        """C1-01: parse_semver & AppUpdater reject traversal in version and filename."""
        proto = require_module("remote.protocol")
        updater_mod = require_module("remote.updater")

        for bad_ver in ("2.0.0-../../victim", "2.0.0/sub", "..\\bad", "2.0.0foo"):
            with self.assertRaises(ValueError):
                updater_mod.parse_semver(bad_ver)

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            app_dir = root / "app"
            updater = updater_mod.AppUpdater(app_dir=app_dir, current_version="1.0.0")
            victim = root / "victim.txt"
            victim.write_text("keep", encoding="utf-8")

            bad_manifests = [
                proto.UpdateManifest(
                    version="2.0.0",
                    min_runtime_version="1.0.0",
                    file_id="upd",
                    filename="../escaped.zip",
                    size_bytes=10,
                    sha256="0" * 64,
                    download_url="http://127.0.0.1:9/upd.zip",
                ),
                proto.UpdateManifest(
                    version="2.0.0",
                    min_runtime_version="1.0.0",
                    file_id="upd",
                    filename="/tmp/abs.zip",
                    size_bytes=10,
                    sha256="0" * 64,
                    download_url="http://127.0.0.1:9/upd.zip",
                ),
                proto.UpdateManifest(
                    version="2.0.0",
                    min_runtime_version="1.0.0",
                    file_id="upd",
                    filename="..\\win.zip",
                    size_bytes=10,
                    sha256="0" * 64,
                    download_url="http://127.0.0.1:9/upd.zip",
                ),
                proto.UpdateManifest(
                    version="2.0.0-../../victim",
                    min_runtime_version="1.0.0",
                    file_id="upd",
                    filename="ok.zip",
                    size_bytes=10,
                    sha256="0" * 64,
                    download_url="http://127.0.0.1:9/upd.zip",
                ),
            ]
            for m in bad_manifests:
                with self.assertRaises((updater_mod.UpdateVerificationError, ValueError)):
                    updater.check_manifest(m)
                with self.assertRaises((updater_mod.UpdateVerificationError, ValueError)):
                    updater.apply_update(m)

            self.assertFalse((root / "escaped.zip").exists())
            self.assertTrue(victim.is_file())

    # ------------------------------------------------------------------
    # C1-02: IsoTransferClient Path Traversal
    # ------------------------------------------------------------------
    @feature("F-11", "F-24")
    def test_c1_02_iso_transfer_filename_traversal(self) -> None:
        """C1-02: IsoTransferClient.download rejects traversal or path separators in filename."""
        proto = require_module("remote.protocol")
        iso_mod = require_module("remote.iso_transfer")

        with tempfile.TemporaryDirectory() as td:
            dest = Path(td) / "isos"
            dest.mkdir()
            tr = _FakeTransport(lambda *_: _MockFileStream(200, [b"abc"]))
            client = iso_mod.IsoTransferClient(transport=tr)

            for bad_name in ("../escaped.iso", "/tmp/abs.iso", "sub/dir.iso", "..\\win.iso"):
                spec = proto.IsoSpec(
                    file_id="iso1",
                    filename=bad_name,
                    version="2025",
                    size_bytes=3,
                    sha256=_sha256_bytes(b"abc"),
                )
                with self.assertRaises(iso_mod.TransferError):
                    client.download(spec, dest, skip_local_check=True)
            self.assertFalse((Path(td) / "escaped.iso").exists())
            self.assertEqual(tr.calls, 0)

    # ------------------------------------------------------------------
    # C1-03: Truncated 200 OK Infinite Loop Prevention
    # ------------------------------------------------------------------
    @feature("F-11", "F-24")
    def test_c1_03_iso_transfer_truncated_200_no_infinite_loop(self) -> None:
        """C1-03: Truncated 200 OK when offset > 0 retries up to max_retries and raises."""
        proto = require_module("remote.protocol")
        iso_mod = require_module("remote.iso_transfer")

        payload = b"A" * 200
        with tempfile.TemporaryDirectory() as td:
            dest = Path(td)
            part = dest / "max.iso.part"
            meta = dest / "max.iso.part.meta.json"
            part.write_bytes(payload[:100])
            tail_sha = _sha256_bytes(payload[:100])
            meta.write_text(
                json.dumps(
                    {
                        "file_id": "iso1",
                        "filename": "max.iso",
                        "size_bytes": 200,
                        "sha256": _sha256_bytes(payload),
                        "bytes_written": 100,
                        "tail_sha256": tail_sha,
                        "tail_size": 100,
                    }
                ),
                encoding="utf-8",
            )

            tr = _FakeTransport(lambda *_: _MockFileStream(200, [b"X" * 50]))
            client = iso_mod.IsoTransferClient(
                transport=tr, max_retries=2, backoff_base=0.01, max_backoff=0.02
            )
            spec = proto.IsoSpec(
                file_id="iso1",
                filename="max.iso",
                version="2025",
                size_bytes=200,
                sha256=_sha256_bytes(payload),
            )
            with self.assertRaises(iso_mod.TransferError):
                client.download(spec, dest, skip_local_check=True)
            self.assertEqual(tr.calls, 3)

    # ------------------------------------------------------------------
    # C1-04: Corrupt .part.meta.json Tail Handling
    # ------------------------------------------------------------------
    @feature("F-11", "F-24")
    def test_c1_04_iso_transfer_corrupt_meta_tail(self) -> None:
        """C1-04: Corrupt tail_size or empty tail_sha256 resets safely instead of crashing."""
        proto = require_module("remote.protocol")
        iso_mod = require_module("remote.iso_transfer")

        payload = b"0123456789" * 20
        sha = _sha256_bytes(payload)
        spec = proto.IsoSpec(
            file_id="iso1",
            filename="max.iso",
            version="2025",
            size_bytes=len(payload),
            sha256=sha,
        )

        for bad_meta in (
            {"tail_size": "bad", "tail_sha256": sha, "bytes_written": 50},
            {"tail_size": -5, "tail_sha256": sha, "bytes_written": 50},
            {"tail_size": 10, "tail_sha256": "", "bytes_written": 50},
        ):
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td)
                (dest / "max.iso.part").write_bytes(b"CORRUPT" * 10)
                meta_obj = {
                    "file_id": "iso1",
                    "filename": "max.iso",
                    "size_bytes": len(payload),
                    "sha256": sha,
                    **bad_meta,
                }
                (dest / "max.iso.part.meta.json").write_text(
                    json.dumps(meta_obj), encoding="utf-8"
                )
                tr = _FakeTransport(
                    lambda _c, start, end: _MockFileStream(
                        206, [payload[start : (end + 1 if end is not None else len(payload))]]
                    )
                )
                client = iso_mod.IsoTransferClient(transport=tr, backoff_base=0.01)
                res = client.download(spec, dest, skip_local_check=True)
                self.assertEqual(res.local_path.read_bytes(), payload)

    # ------------------------------------------------------------------
    # C1-05: Corrupt active.json Quarantine & Bootstrap Recovery
    # ------------------------------------------------------------------
    @feature("F-18", "F-19", "F-24")
    def test_c1_05_corrupt_active_json_quarantine_and_recovery(self) -> None:
        """C1-05: Corrupt active.json is quarantined and bootstrap recovers cleanly."""
        updater_mod = require_module("remote.updater")
        bootstrap_mod = require_module("remote.client.bootstrap")

        for raw_content in ("{truncated", "[]", '{"version": "1.0.0", "attempts": null}'):
            with tempfile.TemporaryDirectory() as td:
                root = Path(td)
                rel_dir = root / "app" / "releases" / "1.0.0"
                (rel_dir / "remote" / "client").mkdir(parents=True)
                (rel_dir / "remote" / "__init__.py").write_text("", encoding="utf-8")
                (rel_dir / "remote" / "client" / "__init__.py").write_text("", encoding="utf-8")
                (rel_dir / "remote" / "client" / "main.py").write_text(
                    "import sys; sys.exit(0)\n", encoding="utf-8"
                )
                active_path = root / "app" / "active.json"
                active_path.write_text(raw_content, encoding="utf-8")

                st = updater_mod.read_active_state(active_path)
                self.assertEqual(st["version"], "1.0.0")
                self.assertIsInstance(st["attempts"], int)

                active_path.write_text(raw_content, encoding="utf-8")
                rc = bootstrap_mod.main(["--root", str(root), "--no-elevate"])
                self.assertEqual(rc, 0)
                if raw_content in ("{truncated", "[]"):
                    backups = list((root / "app").glob("active.json.corrupt.*"))
                    self.assertTrue(backups)

    # ------------------------------------------------------------------
    # C1-06: Relay Whitespace Session/Token & Invalid Response Header
    # ------------------------------------------------------------------
    @feature("F-08", "F-24")
    def test_c1_06_relay_whitespace_and_invalid_response_headers(self) -> None:
        """C1-06: Relay rejects whitespace session/token and invalid X-Relay-Status cleanly."""
        relay_mod = require_module("remote.relay_server")

        relay = relay_mod.RelayServer(host="127.0.0.1", port=0)
        relay.start()
        try:
            conn = http.client.HTTPConnection("127.0.0.1", relay.port, timeout=5.0)
            try:
                body = json.dumps({"session_id": "   ", "token": "   "}).encode("utf-8")
                conn.request(
                    "POST",
                    "/relay/v1/sessions/register",
                    body=body,
                    headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
                )
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 400)

                valid_body = json.dumps({"session_id": "S-1", "token": "tok-1"}).encode("utf-8")
                conn.request(
                    "POST",
                    "/relay/v1/sessions/register",
                    body=valid_body,
                    headers={
                        "Content-Type": "application/json",
                        "Content-Length": str(len(valid_body)),
                    },
                )
                resp2 = conn.getresponse()
                resp2.read()
                self.assertEqual(resp2.status, 200)
            finally:
                conn.close()

            sess = relay.get_session("S-1")
            assert sess is not None
            pipe = relay_mod._RelayStreamPipe(req_id="req-bad", kind="http")
            with sess.lock:
                sess.inflight_responses["req-bad"] = pipe

            conn2 = http.client.HTTPConnection("127.0.0.1", relay.port, timeout=5.0)
            try:
                conn2.request(
                    "POST",
                    "/relay/v1/sessions/S-1/operator/responses/req-bad",
                    body=b"hello",
                    headers={
                        "Authorization": "Bearer tok-1",
                        "X-Session-Id": "S-1",
                        "X-Relay-Status": "not-an-int",
                        "Content-Length": "5",
                    },
                )
                r_bad = conn2.getresponse()
                r_bad.read()
                self.assertEqual(r_bad.status, 400)
            finally:
                conn2.close()
        finally:
            relay.shutdown()

    # ------------------------------------------------------------------
    # F-C2-01: XSS Escaping in Operator UI & Client Status UI
    # ------------------------------------------------------------------
    @feature("F-22", "F-23", "F-24")
    def test_fc2_01_xss_escaping_in_operator_and_status_ui(self) -> None:
        """F-C2-01: Untrusted client_id and logs are HTML-escaped in Status UI and app.js."""
        agent_mod = require_module("remote.client.agent")
        status_mod = require_module("remote.client.status_ui")
        sim_mod = require_module("remote.drivers.simulated")
        op_mod = require_module("remote.operator.server")

        with tempfile.TemporaryDirectory() as td:
            xss_cid = '<img src=x onerror=alert(1)>'
            agent = agent_mod.ClientAgent(
                client_id=xss_cid,
                drivers=sim_mod.create_simulated_bundle(),
                data_dir=Path(td) / "data",
                iso_dir=Path(td) / "isos",
            )
            with status_mod.ClientStatusServer(agent, port=0) as status_srv:
                conn = http.client.HTTPConnection("127.0.0.1", status_srv.port, timeout=5.0)
                try:
                    conn.request("GET", "/")
                    resp = conn.getresponse()
                    html_body = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    self.assertNotIn("<img src=x", html_body)
                    self.assertIn("&lt;img src=x", html_body)
                    self.assertIn("function escapeHtml", html_body)
                finally:
                    conn.close()

            with op_mod.OperatorServer(port=0) as op_srv:
                conn = http.client.HTTPConnection("127.0.0.1", op_srv.port, timeout=5.0)
                try:
                    conn.request("GET", "/static/app.js")
                    resp = conn.getresponse()
                    js_body = resp.read().decode("utf-8")
                    self.assertEqual(resp.status, 200)
                    self.assertIn("function escapeHtml", js_body)
                    self.assertIn("escapeHtml(c.client_id)", js_body)
                    self.assertIn("escapeHtml(l.msg)", js_body)
                    self.assertIn("escapeHtml(i.filename)", js_body)
                finally:
                    conn.close()

    # ------------------------------------------------------------------
    # F-C2-02: CSRF Origin & Content-Type Enforcement
    # ------------------------------------------------------------------
    @feature("F-22", "F-23", "F-24")
    def test_fc2_02_csrf_origin_and_content_type_enforcement(self) -> None:
        """F-C2-02: UI POST endpoints reject text/plain (415) and cross-origin Origin (403)."""
        agent_mod = require_module("remote.client.agent")
        status_mod = require_module("remote.client.status_ui")
        sim_mod = require_module("remote.drivers.simulated")
        op_mod = require_module("remote.operator.server")

        with tempfile.TemporaryDirectory() as td:
            agent = agent_mod.ClientAgent(
                client_id="c-csrf",
                drivers=sim_mod.create_simulated_bundle(),
                data_dir=Path(td) / "data",
                iso_dir=Path(td) / "isos",
            )
            with status_mod.ClientStatusServer(agent, port=0) as status_srv, op_mod.OperatorServer(
                port=0
            ) as op_srv:
                op_srv._on_register("c-csrf", {})

                c1 = http.client.HTTPConnection("127.0.0.1", status_srv.port, timeout=5.0)
                payload = b'{"action":"defender_control","payload":{"action":"disable"}}'
                c1.request(
                    "POST",
                    "/api/action",
                    body=payload,
                    headers={"Content-Type": "text/plain", "Content-Length": str(len(payload))},
                )
                r1 = c1.getresponse()
                r1.read()
                self.assertEqual(r1.status, 415)
                c1.close()

                c2 = http.client.HTTPConnection("127.0.0.1", status_srv.port, timeout=5.0)
                c2.request(
                    "POST",
                    "/api/action",
                    body=payload,
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://evil.example",
                        "Content-Length": str(len(payload)),
                    },
                )
                r2 = c2.getresponse()
                r2.read()
                self.assertEqual(r2.status, 403)
                c2.close()

                cmd_body = b'{"cmd_type":"abort","payload":{}}'
                c3 = http.client.HTTPConnection("127.0.0.1", op_srv.port, timeout=5.0)
                c3.request(
                    "POST",
                    "/api/v1/ui/clients/c-csrf/command",
                    body=cmd_body,
                    headers={"Content-Type": "text/plain", "Content-Length": str(len(cmd_body))},
                )
                r3 = c3.getresponse()
                r3.read()
                self.assertEqual(r3.status, 415)
                c3.close()

                c4 = http.client.HTTPConnection("127.0.0.1", op_srv.port, timeout=5.0)
                c4.request(
                    "POST",
                    "/api/v1/ui/rescan",
                    body=b"{}",
                    headers={
                        "Content-Type": "application/json",
                        "Origin": "https://evil.example",
                        "Content-Length": "2",
                    },
                )
                r4 = c4.getresponse()
                r4.read()
                self.assertEqual(r4.status, 403)
                c4.close()

                self.assertEqual(op_srv._clients["c-csrf"].commands, [])

    # ------------------------------------------------------------------
    # F-C2-03: Command Replay & Sequence Desync Across Restarts
    # ------------------------------------------------------------------
    @feature("F-07", "F-21", "F-22", "F-24")
    def test_fc2_03_command_replay_and_seq_desync_across_restarts(self) -> None:
        """F-C2-03: Client restart does not replay acked commands; Operator restart does not drop new ones."""
        agent_mod = require_module("remote.client.agent")
        sim_mod = require_module("remote.drivers.simulated")
        op_mod = require_module("remote.operator.server")

        with tempfile.TemporaryDirectory() as td:
            data_dir = Path(td) / "data"
            iso_dir = Path(td) / "isos"
            with op_mod.OperatorServer(port=0, session_id="S-SEQ", token="tok-seq") as op1:
                pairing = op1.pairing
                a1 = agent_mod.ClientAgent(
                    client_id="c-seq",
                    pairing=pairing,
                    drivers=sim_mod.create_simulated_bundle(),
                    data_dir=data_dir,
                    iso_dir=iso_dir,
                    poll_wait=0.2,
                )
                a1.start()
                try:
                    time.sleep(0.15)
                    op1.enqueue_command("c-seq", "refresh_status", {})
                    op1.enqueue_command("c-seq", "skip_stage", {"target_stage": "WAIT_LICENSE"})
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        if op1._clients["c-seq"].commands == []:
                            break
                        time.sleep(0.05)
                    self.assertEqual(op1._clients["c-seq"].commands, [])
                finally:
                    a1.stop()

                a2 = agent_mod.ClientAgent(
                    client_id="c-seq",
                    pairing=pairing,
                    drivers=sim_mod.create_simulated_bundle(),
                    data_dir=data_dir,
                    iso_dir=iso_dir,
                    poll_wait=0.2,
                )
                self.assertGreaterEqual(a2._last_seq, 2)
                a2.start()
                try:
                    time.sleep(0.25)
                    op1.enqueue_command("c-seq", "reset", {"target_stage": "PREP"})
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        if a2.state.stage.value == "PREP":
                            break
                        time.sleep(0.05)
                    self.assertEqual(a2.state.stage.value, "PREP")
                finally:
                    a2.stop()

            op2 = op_mod.OperatorServer(port=0, session_id="S-SEQ", token="tok-seq")
            op2.start()
            try:
                a3 = agent_mod.ClientAgent(
                    client_id="c-seq",
                    pairing=op2.pairing,
                    drivers=sim_mod.create_simulated_bundle(),
                    data_dir=data_dir,
                    iso_dir=iso_dir,
                    poll_wait=0.2,
                )
                a3._last_seq = 5
                a3.start()
                try:
                    time.sleep(0.15)
                    op2.enqueue_command("c-seq", "skip_stage", {"target_stage": "WAIT_LICENSE"})
                    deadline = time.monotonic() + 3.0
                    while time.monotonic() < deadline:
                        if a3.state.stage.value == "WAIT_LICENSE":
                            break
                        time.sleep(0.05)
                    self.assertEqual(a3.state.stage.value, "WAIT_LICENSE")
                finally:
                    a3.stop()
            finally:
                op2.shutdown()

    # ------------------------------------------------------------------
    # F-C2-04: WindowsPostConfigDriver Renderer Drain & Timeout
    # ------------------------------------------------------------------
    @feature("F-17", "F-24")
    def test_fc2_04_windows_post_config_renderer_drain_and_timeout(self) -> None:
        """F-C2-04: Renderer step drains >128KB stdout without deadlock and enforces timeout."""
        win_mod = require_module("remote.drivers.windows")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            assets = root / "assets"
            assets.mkdir()
            (assets / "corona_setup.exe").write_bytes(b"exe")
            cfg = root / "post.yaml"
            cfg.write_text(
                "steps:\n"
                "  - id: renderer_corona\n"
                "    name: Chaos Corona\n"
                "    category: renderer\n"
                "    installer_glob: '*corona*.exe'\n"
                "    timeout_sec: 0.25\n",
                encoding="utf-8",
            )
            drv = win_mod.WindowsPostConfigDriver(
                local_app_data=root / "lad",
                program_files=root / "pf",
                popen_fn=lambda cmd, **kw: subprocess.Popen(  # noqa: ARG005
                    [sys.executable, "-c", "print('X' * 150000)"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                ),
            )
            logs: list[tuple[str, str]] = []
            res = drv.run_step(
                "renderer_corona",
                "2025",
                assets_dir=assets,
                log_cb=lambda lvl, msg: logs.append((lvl, msg)),
                cancel_event=threading.Event(),
                config_path=cfg,
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "done")

            drv_hang = win_mod.WindowsPostConfigDriver(
                local_app_data=root / "lad",
                program_files=root / "pf",
                popen_fn=lambda cmd, **kw: subprocess.Popen(  # noqa: ARG005
                    [sys.executable, "-c", "import time; time.sleep(10)"],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True,
                ),
            )
            res_to = drv_hang.run_step(
                "renderer_corona",
                "2025",
                assets_dir=assets,
                log_cb=lambda lvl, msg: logs.append((lvl, msg)),
                cancel_event=threading.Event(),
                config_path=cfg,
            )
            self.assertFalse(res_to["ok"])
            self.assertEqual(res_to["status"], "timeout")

    # ------------------------------------------------------------------
    # F-C2-05: Install-3dsMax.ps1 Array -match Under StrictMode
    # ------------------------------------------------------------------
    @feature("F-15", "F-24")
    def test_fc2_05_install_3dsmax_ps1_strictmode_match(self) -> None:
        """F-C2-05: Install-3dsMax.ps1 joins Get-Content array before -match under StrictMode."""
        ps1 = Path(__file__).resolve().parents[2] / "vm" / "Install-3dsMax.ps1"
        text = ps1.read_text(encoding="utf-8")
        self.assertIn('-join "`n"', text)
        self.assertIn("Installation success or error status: (1602|1603)", text)

    # ------------------------------------------------------------------
    # F-C2-06: Static File Serving Prefix Traversal
    # ------------------------------------------------------------------
    @feature("F-22", "F-24")
    def test_fc2_06_static_serving_prefix_traversal_blocked(self) -> None:
        """F-C2-06: _serve_static uses is_relative_to and blocks sibling prefix traversal."""
        op_mod = require_module("remote.operator.server")
        server_py = Path(__file__).resolve().parents[2] / "remote" / "operator" / "server.py"
        self.assertIn("is_relative_to", server_py.read_text(encoding="utf-8"))
        with op_mod.OperatorServer(port=0) as op_srv:
            conn = http.client.HTTPConnection("127.0.0.1", op_srv.port, timeout=5.0)
            try:
                conn.request("GET", "/static/../server.py")
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 404)
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # F-C2-07: Unsanitized version & manifest.filename Rejected
    # ------------------------------------------------------------------
    @feature("F-17", "F-18", "F-21", "F-24")
    def test_fc2_07_unsanitized_version_and_filename_rejected(self) -> None:
        """F-C2-07: Traversal in version or manifest.filename is rejected in Agent and Drivers."""
        agent_mod = require_module("remote.client.agent")
        sim_mod = require_module("remote.drivers.simulated")
        win_mod = require_module("remote.drivers.windows")

        with tempfile.TemporaryDirectory() as td:
            agent = agent_mod.ClientAgent(
                client_id="c-path",
                drivers=sim_mod.create_simulated_bundle(),
                data_dir=Path(td) / "data",
                iso_dir=Path(td) / "isos",
            )
            res = agent.execute_local_action("start_install", {"version": "../../evil"})
            self.assertFalse(res.get("ok", True))

            res_upd = agent.execute_local_action(
                "push_update",
                {
                    "manifest": {
                        "version": "2.0.0",
                        "min_runtime_version": "1.0.0",
                        "file_id": "u1",
                        "filename": "../../escaped.zip",
                        "size_bytes": 10,
                        "sha256": "0" * 64,
                    }
                },
            )
            self.assertFalse(res_upd.get("ok", True))
            self.assertFalse((Path(td) / "escaped.zip").exists())

            drv_pc = win_mod.WindowsPostConfigDriver(local_app_data=Path(td) / "lad")
            with self.assertRaises(ValueError):
                drv_pc._enu_dir("../../evil")
            drv_fr = win_mod.WindowsFirstRunDriver()
            with self.assertRaises(ValueError):
                drv_fr.run_first_run(
                    "../../evil",
                    log_cb=lambda *_: None,
                    cancel_event=threading.Event(),
                )

    # ------------------------------------------------------------------
    # F-C2-08: URL-Encoded client_id Consistency
    # ------------------------------------------------------------------
    @feature("F-07", "F-22", "F-24")
    def test_fc2_08_url_encoded_client_id_consistency(self) -> None:
        """F-C2-08: OperatorServer unquotes URL-encoded client_id path segments."""
        op_mod = require_module("remote.operator.server")
        tr_mod = require_module("remote.transport")
        sim_mod = require_module("remote.drivers.simulated")

        cid = "client vm@1"
        with op_mod.OperatorServer(port=0, session_id="S-URL", token="tok-url") as op_srv:
            tr = tr_mod.DirectTransport(
                base_url=op_srv.base_url,
                session_id=op_srv.session_id,
                token=op_srv.token,
                client_id=cid,
            )
            tr.register({"client_id": cid})
            tr.heartbeat(sim_mod.SimulatedSystemDriver().collect_status("1.0.0", "1.0.0"))
            snap = op_srv.get_snapshot()
            self.assertEqual(list(snap["clients"].keys()), [cid])

            encoded = urllib.parse.quote(cid, safe="")
            conn = http.client.HTTPConnection("127.0.0.1", op_srv.port, timeout=5.0)
            try:
                body = json.dumps({"cmd_type": "refresh_status", "payload": {}}).encode("utf-8")
                conn.request(
                    "POST",
                    f"/api/v1/ui/clients/{encoded}/command",
                    body=body,
                    headers={"Content-Type": "application/json", "Content-Length": str(len(body))},
                )
                resp = conn.getresponse()
                resp.read()
                self.assertEqual(resp.status, 200)
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # F-C2-09: Content-Length & Query Parameter Validation
    # ------------------------------------------------------------------
    @feature("F-07", "F-22", "F-23", "F-24")
    def test_fc2_09_content_length_and_query_param_validation(self) -> None:
        """F-C2-09: Invalid/oversized Content-Length and invalid poll params return 400/413."""
        agent_mod = require_module("remote.client.agent")
        status_mod = require_module("remote.client.status_ui")
        sim_mod = require_module("remote.drivers.simulated")
        op_mod = require_module("remote.operator.server")

        with tempfile.TemporaryDirectory() as td:
            agent = agent_mod.ClientAgent(
                client_id="c-len",
                drivers=sim_mod.create_simulated_bundle(),
                data_dir=Path(td) / "data",
                iso_dir=Path(td) / "isos",
            )
            with status_mod.ClientStatusServer(agent, port=0) as status_srv, op_mod.OperatorServer(
                port=0, session_id="S-LEN", token="tok-len"
            ) as op_srv:
                for port, path in ((status_srv.port, "/api/action"), (op_srv.port, "/api/v1/ui/rescan")):
                    for bad_len, expected in (("abc", 400), ("-5", 400), ("999999999", 413)):
                        c = http.client.HTTPConnection("127.0.0.1", port, timeout=5.0)
                        c.putrequest("POST", path)
                        c.putheader("Content-Type", "application/json")
                        c.putheader("Content-Length", bad_len)
                        c.endheaders()
                        r = c.getresponse()
                        r.read()
                        self.assertEqual(r.status, expected)
                        c.close()

                c_poll = http.client.HTTPConnection("127.0.0.1", op_srv.port, timeout=5.0)
                c_poll.request(
                    "GET",
                    "/api/v1/clients/c1/commands/poll?after_seq=abc&wait=nan",
                    headers={"Authorization": "Bearer tok-len", "X-Session-Id": "S-LEN"},
                )
                r_poll = c_poll.getresponse()
                r_poll.read()
                self.assertEqual(r_poll.status, 400)
                c_poll.close()

    # ------------------------------------------------------------------
    # F-C2-10: _ClientRecord Thread-Safety Race
    # ------------------------------------------------------------------
    @feature("F-22", "F-24")
    def test_fc2_10_operator_client_record_thread_safety(self) -> None:
        """F-C2-10: Concurrent _on_logs and get_snapshot never raise RuntimeError."""
        op_mod = require_module("remote.operator.server")

        with op_mod.OperatorServer(port=0) as op_srv:
            errors: list[Exception] = []
            stop = threading.Event()

            def _writer() -> None:
                try:
                    for i in range(150):
                        op_srv._on_logs(
                            "c-race",
                            {"entries": [{"ts": "t", "level": "INFO", "stage": "PREP", "msg": str(i)}]},
                        )
                except Exception as exc:
                    errors.append(exc)

            def _reader() -> None:
                try:
                    while not stop.is_set():
                        op_srv.get_snapshot()
                except Exception as exc:
                    errors.append(exc)

            readers = [threading.Thread(target=_reader) for _ in range(2)]
            writers = [threading.Thread(target=_writer) for _ in range(4)]
            for r in readers:
                r.start()
            for w in writers:
                w.start()
            for w in writers:
                w.join()
            stop.set()
            for r in readers:
                r.join()
            self.assertEqual(errors, [])

    # ------------------------------------------------------------------
    # F-C2-11: No Duplicate Concurrent Stage Workers
    # ------------------------------------------------------------------
    @feature("F-21", "F-24")
    def test_fc2_11_agent_no_duplicate_concurrent_workers(self) -> None:
        """F-C2-11: _launch_worker refuses to clear _cancel_event or spawn while old worker is alive."""
        agent_mod = require_module("remote.client.agent")
        base_mod = require_module("remote.drivers.base")
        sim_mod = require_module("remote.drivers.simulated")

        started = threading.Event()
        release = threading.Event()

        class _SlowInstaller(sim_mod.SimulatedInstallerDriver):
            def install_3dsmax(self, version, iso_path, *, progress_cb, log_cb, cancel_event, timeout_sec=2400):  # noqa: ARG002
                started.set()
                release.wait(timeout=5.0)
                return {"ok": False, "detail": "cancelled"}

        with tempfile.TemporaryDirectory() as td:
            bundle = base_mod.DriverBundle(
                system=sim_mod.SimulatedSystemDriver(),
                installer=_SlowInstaller(),
                first_run=sim_mod.SimulatedFirstRunDriver(),
                post_config=sim_mod.SimulatedPostConfigDriver(),
            )
            agent = agent_mod.ClientAgent(
                client_id="c-dup",
                drivers=bundle,
                data_dir=Path(td) / "data",
                iso_dir=Path(td) / "isos",
            )
            try:
                agent.execute_local_action("start_install", {"version": "2025"})
                self.assertTrue(started.wait(timeout=2.0))
                launched = agent._launch_worker(lambda: None)
                self.assertFalse(launched)
                self.assertTrue(agent._cancel_event.is_set())
            finally:
                release.set()
                agent._wait_worker(timeout=2.0)

    # ------------------------------------------------------------------
    # F-C2-12: configure_defender("restore") Verifies Outcome
    # ------------------------------------------------------------------
    @feature("F-14", "F-24")
    def test_fc2_12_configure_defender_restore_verifies_outcome(self) -> None:
        """F-C2-12: configure_defender('restore') returns ok=False if Set-MpPreference fails."""
        win_mod = require_module("remote.drivers.windows")

        with tempfile.TemporaryDirectory() as td:
            def _fake_run_cmd(cmd: list[str], timeout: float = 20.0):  # noqa: ARG001
                joined = " ".join(cmd)
                if "Get-MpComputerStatus" in joined:
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps(
                            {
                                "RealTimeProtectionEnabled": False,
                                "IsTamperProtected": True,
                                "ExclusionPath": [],
                            }
                        ),
                        stderr="",
                    )
                if "Set-MpPreference" in joined:
                    return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Tamper protected")
                return subprocess.CompletedProcess(cmd, 0, stdout="[]", stderr="")

            drv = win_mod.WindowsSystemDriver(
                data_dir=Path(td),
                run_cmd=_fake_run_cmd,
            )
            drv._win32.is_user_an_admin = lambda: True
            res = drv.configure_defender("restore")
            self.assertFalse(res["ok"])
            self.assertIn("Failed to restore", res["detail"])

    # ------------------------------------------------------------------
    # F-C2-13: CLI --json-result Atomic/Error, SKIP_STAGE Missing Key, MZP Dir Copy
    # ------------------------------------------------------------------
    @feature("F-16", "F-17", "F-21", "F-24")
    def test_fc2_13_cli_json_result_skip_stage_and_mzp_dir_copy(self) -> None:
        """F-C2-13: CLI writes error JSON on watch_loop exception; SKIP_STAGE rejects missing key; MZP copies dirs."""
        cli_mod = require_module("cli.main")
        agent_mod = require_module("remote.client.agent")
        sim_mod = require_module("remote.drivers.simulated")
        op_mod = require_module("remote.operator.server")
        win_mod = require_module("remote.drivers.windows")

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            json_out = root / "out" / "result.json"
            with patch("cli.main.watch_loop", side_effect=RuntimeError("boom")):
                with self.assertRaises(Exception):
                    cli_mod.cli.main(
                        ["run", "--no-launch", "--json-result", str(json_out)],
                        standalone_mode=False,
                    )
            self.assertTrue(json_out.is_file())
            parsed = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(parsed["status"], "failed")
            self.assertIn("boom", parsed["detail"])

            with op_mod.OperatorServer(port=0, session_id="S-SKIP", token="tok-skip") as op_srv:
                agent = agent_mod.ClientAgent(
                    client_id="c-skip",
                    pairing=op_srv.pairing,
                    drivers=sim_mod.create_simulated_bundle(),
                    data_dir=root / "data",
                    iso_dir=root / "isos",
                    poll_wait=0.2,
                )
                agent.start()
                try:
                    op_srv.enqueue_command("c-skip", "skip_stage", {})
                    deadline = time.monotonic() + 3.0
                    ack = None
                    while time.monotonic() < deadline:
                        ack = op_srv._clients.get("c-skip") and op_srv._clients["c-skip"].last_ack
                        if ack and ack.get("status") == "rejected":
                            break
                        time.sleep(0.05)
                    self.assertIsNotNone(ack)
                    self.assertEqual(ack["status"], "rejected")
                finally:
                    agent.stop()

            extract_dir = root / "mzp_ext"
            extract_dir.mkdir()
            (extract_dir / "mzp.run").write_text('copy "scripts" to "$userScripts"\n', encoding="utf-8")
            (extract_dir / "scripts" / "sub").mkdir(parents=True)
            (extract_dir / "scripts" / "sub" / "tool.ms").write_text("-- hello\n", encoding="utf-8")
            enu_dir = root / "ENU"
            drv_pc = win_mod.WindowsPostConfigDriver(local_app_data=root)
            copied = drv_pc._apply_mzp_run(
                extract_dir,
                enu_dir=enu_dir,
                startup_dir=enu_dir / "scripts" / "Startup",
                macros_dir=enu_dir / "usermacros",
                icons_dir=enu_dir / "usericons",
            )
            self.assertTrue((enu_dir / "scripts" / "Startup" / "sub" / "tool.ms").is_file())
            self.assertEqual(len(copied), 1)
