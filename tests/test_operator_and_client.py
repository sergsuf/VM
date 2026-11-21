"""Comprehensive tests for OperatorServer, ClientAgent, ClientStatusServer, and CLIs."""

from __future__ import annotations

import hashlib
from http import HTTPStatus
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
import zipfile

from remote.auth import PairingConfig
from remote.client.agent import ClientAgent
from remote.client.main import build_arg_parser as build_client_parser
from remote.client.status_ui import ClientStatusServer
from remote.drivers.simulated import create_simulated_bundle
from remote.operator.main import build_arg_parser as build_operator_parser
from remote.operator.server import OperatorServer
from remote.protocol import CommandType, Stage, StageStatus, UpdateManifest
from remote.relay_server import RelayServer
from remote.updater import UPDATE_RESTART_EXIT_CODE


def _http_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: dict | None = None,
) -> tuple[int, dict]:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = dict(headers or {})
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8")
        return exc.code, (json.loads(raw) if raw else {})


def _wait_until(pred, *, timeout: float = 10.0, interval: float = 0.05) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(interval)
    raise AssertionError("Timed out waiting for condition")


def _make_fake_iso(iso_dir: Path, filename: str, content: bytes) -> Path:
    iso_dir.mkdir(parents=True, exist_ok=True)
    p = iso_dir / filename
    p.write_bytes(content)
    return p


def _make_update_bundle(updates_dir: Path, version: str) -> UpdateManifest:
    updates_dir.mkdir(parents=True, exist_ok=True)
    filename = f"max-agent-app-{version}.zip"
    zip_path = updates_dir / filename
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("remote/__init__.py", f'__version__ = "{version}"\n')
        zf.writestr("VERSION.txt", f"{version}\n")
    data = zip_path.read_bytes()
    manifest = UpdateManifest(
        version=version,
        min_runtime_version="1.0.0",
        file_id=f"app-{version}",
        filename=filename,
        size_bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        notes="Test update",
    )
    (updates_dir / "manifest.json").write_text(
        json.dumps(manifest.to_dict(), indent=2), encoding="utf-8"
    )
    return manifest


class TestOperatorAndClient(unittest.TestCase):
    def test_cli_parsers(self) -> None:
        op_args = build_operator_parser().parse_args(
            [
                "--host",
                "0.0.0.0",
                "--port",
                "9000",
                "--mode",
                "relay",
                "--relay-url",
                "http://127.0.0.1:8766",
                "--no-browser",
            ]
        )
        self.assertEqual(op_args.port, 9000)
        self.assertEqual(op_args.mode, "relay")
        self.assertTrue(op_args.no_browser)

        cl_args = build_client_parser().parse_args(
            [
                "--mode",
                "direct",
                "--server-url",
                "http://127.0.0.1:8765",
                "--session-id",
                "S-1234",
                "--token",
                "tok",
                "--driver",
                "simulated",
                "--headless",
                "--status-port",
                "0",
            ]
        )
        self.assertEqual(cl_args.driver, "simulated")
        self.assertTrue(cl_args.headless)
        self.assertEqual(cl_args.status_port, 0)

    def test_operator_auth_static_range_and_sse(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso_dir = root / "isos"
            updates_dir = root / "updates"
            _make_fake_iso(iso_dir, "3ds_Max_2025.iso", b"ISO-CONTENT-" * 1024)
            manifest = _make_update_bundle(updates_dir, "9.9.0")

            with OperatorServer(
                host="127.0.0.1",
                port=0,
                iso_dir=iso_dir,
                updates_dir=updates_dir,
                session_id="S-AUTH",
                token="secret-tok",
            ) as op:
                # Static files & UI snapshot
                with urllib.request.urlopen(f"{op.base_url}/", timeout=5) as r:
                    self.assertEqual(r.status, 200)
                    html = r.read().decode("utf-8")
                    self.assertIn("/static/app.js", html)
                with urllib.request.urlopen(f"{op.base_url}/static/app.js", timeout=5) as r:
                    self.assertEqual(r.status, 200)
                    self.assertIn("EventSource", r.read().decode("utf-8"))
                with urllib.request.urlopen(f"{op.base_url}/static/styles.css", timeout=5) as r:
                    self.assertEqual(r.status, 200)

                code, snap = _http_json(f"{op.base_url}/api/v1/ui/snapshot")
                self.assertEqual(code, 200)
                self.assertEqual(len(snap["iso_catalog"]), 1)
                self.assertEqual(snap["update_manifest"]["version"], "9.9.0")
                self.assertGreater(len(snap["post_config_steps"]), 0)

                # connect.json endpoint
                with urllib.request.urlopen(
                    f"{op.base_url}/api/v1/ui/pairing/connect.json", timeout=5
                ) as r:
                    cfg = PairingConfig.from_connect_json(r.read().decode("utf-8"))
                    self.assertEqual(cfg.session_id, "S-AUTH")

                # Auth checks: 401 missing, 401 bad token, 403 bad session
                c1, _ = _http_json(f"{op.base_url}/api/v1/updates/latest")
                self.assertEqual(c1, HTTPStatus.UNAUTHORIZED)
                c2, _ = _http_json(
                    f"{op.base_url}/api/v1/updates/latest",
                    headers={"Authorization": "Bearer wrong", "X-Session-Id": "S-AUTH"},
                )
                self.assertEqual(c2, HTTPStatus.UNAUTHORIZED)
                c3, _ = _http_json(
                    f"{op.base_url}/api/v1/updates/latest",
                    headers={"Authorization": "Bearer secret-tok", "X-Session-Id": "S-BAD"},
                )
                self.assertEqual(c3, HTTPStatus.FORBIDDEN)

                c4, _ = _http_json(
                    f"{op.base_url}/api/v1/ui/clients/nonexistent/command",
                    method="POST",
                    body={"cmd_type": "refresh_status", "payload": {}},
                )
                self.assertEqual(c4, HTTPStatus.NOT_FOUND)

                # Range request on update archive
                req = urllib.request.Request(
                    f"{op.base_url}/api/v1/files/{manifest.file_id}",
                    headers={
                        "Authorization": "Bearer secret-tok",
                        "X-Session-Id": "S-AUTH",
                        "Range": "bytes=0-15",
                    },
                )
                with urllib.request.urlopen(req, timeout=5) as r:
                    self.assertEqual(r.status, HTTPStatus.PARTIAL_CONTENT)
                    self.assertEqual(len(r.read()), 16)

                # SSE stream receives initial snapshot and live command event
                sse_req = urllib.request.Request(f"{op.base_url}/api/v1/ui/events")
                with urllib.request.urlopen(sse_req, timeout=5) as sse_resp:
                    self.assertIn("text/event-stream", sse_resp.headers.get("Content-Type", ""))
                    lines = []
                    for _ in range(4):
                        line = sse_resp.readline().decode("utf-8")
                        lines.append(line)
                        if line == "\n":
                            break
                    self.assertTrue(any(l.startswith("event: snapshot") for l in lines))

    def test_full_7_stage_e2e_direct_and_status_ui(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            op_isos = root / "op_isos"
            cl_isos = root / "cl_isos"
            cl_data = root / "cl_data"
            _make_fake_iso(op_isos, "Autodesk_3ds_Max_2025.iso", b"MAX2025ISO" * 5000)

            with OperatorServer(
                host="127.0.0.1",
                port=0,
                iso_dir=op_isos,
            ) as op:
                drivers = create_simulated_bundle(
                    work_dir=cl_data / "sim",
                    initial_admin=False,
                    smart_app_control_state="evaluation",
                    defender_rtp_enabled=True,
                    step_delay_sec=0.01,
                )
                agent = ClientAgent(
                    pairing=op.pairing,
                    drivers=drivers,
                    data_dir=cl_data,
                    iso_dir=cl_isos,
                    client_id="win-vm-1",
                    heartbeat_interval=0.5,
                    poll_wait=2,
                )
                with ClientStatusServer(agent, host="127.0.0.1", port=0) as status_ui:
                    agent.start()
                    try:
                        _wait_until(
                            lambda: "win-vm-1" in op.get_snapshot()["clients"]
                            and agent.connected
                        )

                        # Status UI serves localhost HTML & JSON
                        with urllib.request.urlopen(f"{status_ui.url}/", timeout=5) as r:
                            self.assertEqual(r.status, 200)
                            body = r.read().decode("utf-8").lower()
                            self.assertIn("defender", body)
                            self.assertIn("smart app control", body)

                        # UAC + SAC + Defender via Operator command
                        op.enqueue_command("win-vm-1", CommandType.REQUEST_ELEVATION.value)
                        _wait_until(lambda: agent.system_status.is_admin)

                        op.enqueue_command(
                            "win-vm-1",
                            CommandType.SMART_APP_CONTROL.value,
                            {"mode": "guided"},
                        )
                        op.enqueue_command(
                            "win-vm-1",
                            CommandType.DEFENDER_CONTROL.value,
                            {"action": "disable"},
                        )
                        _wait_until(
                            lambda: agent.system_status.defender_rtp_enabled is False
                        )

                        # Start Install -> DOWNLOAD_ISO -> INSTALL_MAX -> WAIT_LICENSE
                        iso_spec = op.catalog.list_specs()[0].to_dict()
                        op.enqueue_command(
                            "win-vm-1",
                            CommandType.START_INSTALL.value,
                            {"version": "2025", "iso_spec": iso_spec},
                        )
                        _wait_until(
                            lambda: agent.state.stage == Stage.WAIT_LICENSE
                            and agent.state.status == StageStatus.WAITING_OPERATOR,
                            timeout=10.0,
                        )

                        # Status UI reflects WAIT_LICENSE
                        code, st_json = _http_json(f"{status_ui.url}/api/status")
                        self.assertEqual(code, 200)
                        self.assertEqual(st_json["stage"], "WAIT_LICENSE")

                        # Confirm License -> FIRST_RUN -> POST_CONFIG (waiting_operator)
                        op.enqueue_command("win-vm-1", CommandType.CONFIRM_LICENSE.value)
                        _wait_until(
                            lambda: agent.state.stage == Stage.POST_CONFIG
                            and agent.state.status == StageStatus.WAITING_OPERATOR,
                            timeout=10.0,
                        )

                        # 1-Click POST_CONFIG -> DONE (completed)
                        op.enqueue_command(
                            "win-vm-1", CommandType.TRIGGER_POST_CONFIG.value
                        )
                        _wait_until(
                            lambda: agent.state.stage == Stage.DONE
                            and agent.state.status == StageStatus.COMPLETED,
                            timeout=10.0,
                        )
                        self.assertIn("verify", agent.state.summary)
                        self.assertTrue(agent.state.summary["verify"]["ok"])
                    finally:
                        agent.stop()

    def test_relay_mode_and_push_update_exit_42(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            updates_dir = root / "updates"
            app_root = root / "portable_app"
            manifest = _make_update_bundle(updates_dir, "2.0.0")

            relay = RelayServer(host="127.0.0.1", port=0)
            relay.start()
            try:
                with OperatorServer(
                    host="127.0.0.1",
                    port=0,
                    updates_dir=updates_dir,
                    mode="relay",
                    relay_url=relay.base_url,
                ) as op:
                    agent = ClientAgent(
                        pairing=op.pairing,
                        drivers=create_simulated_bundle(work_dir=root / "sim"),
                        data_dir=root / "cl_data",
                        iso_dir=root / "cl_isos",
                        client_id="relay-client",
                        heartbeat_interval=0.5,
                        poll_wait=2,
                        app_root=app_root,
                    )
                    agent.start()
                    try:
                        _wait_until(
                            lambda: "relay-client" in op.get_snapshot()["clients"]
                            and agent.connected
                        )
                        op.enqueue_command("relay-client", CommandType.PUSH_UPDATE.value)
                        self.assertTrue(agent.wait(timeout=10.0))
                        self.assertEqual(agent.exit_code, UPDATE_RESTART_EXIT_CODE)
                        active_json = json.loads(
                            (app_root / "app" / "active.json").read_text(encoding="utf-8")
                        )
                        self.assertEqual(active_json["version"], manifest.version)
                        self.assertTrue(active_json["pending_verify"])
                    finally:
                        agent.stop()
            finally:
                relay.shutdown()

    def test_status_ui_post_action_rejects_non_object_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            pairing = PairingConfig(
                mode="direct",
                server_url="http://127.0.0.1:8765",
                session_id="S-TEST",
                token="tok",
            )
            agent = ClientAgent(
                pairing=pairing,
                drivers=create_simulated_bundle(work_dir=root / "sim"),
                data_dir=root / "cl_data",
                iso_dir=root / "cl_isos",
                client_id="status-ui-test",
            )
            with ClientStatusServer(agent, host="127.0.0.1", port=0) as status_ui:
                for raw_body in (b"[1, 2, 3]", b'"str"', b"123", b"true", b"null", b'{"action": "pair", "payload": [1, 2]}'):
                    req = urllib.request.Request(
                        f"{status_ui.url}/api/action",
                        data=raw_body,
                        headers={"Content-Type": "application/json"},
                        method="POST",
                    )
                    with self.assertRaises(urllib.error.HTTPError) as ctx:
                        urllib.request.urlopen(req, timeout=5)
                    self.assertEqual(ctx.exception.code, HTTPStatus.BAD_REQUEST)
                    err_body = json.loads(ctx.exception.read().decode("utf-8"))
                    self.assertIn("error", err_body)


if __name__ == "__main__":
    unittest.main()
