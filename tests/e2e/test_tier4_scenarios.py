"""Tier 4 — Full Real-World End-to-End Scenarios (Exact 8 Scenarios).

Each scenario spins up real live subprocesses (Operator, Relay, Client, Bootstrap
Supervisor, Packaging CLIs) on ephemeral loopback ports inside an isolated
TempWorkspace and verifies the complete user-visible workflow end-to-end.
"""

from __future__ import annotations

import json
import pathlib
import threading
import time
import unittest
import zipfile

from tests.e2e.helpers import (
    CustomDriverAgentHarness,
    FaultInjectingProxy,
    LiveClientHarness,
    LiveOperatorHarness,
    LiveRelayHarness,
    ManagedProcess,
    TempWorkspace,
    auth_headers,
    build_bootstrap_command_args,
    create_app_update_zip,
    create_synthetic_iso,
    create_synthetic_post_config_assets,
    extract_client_state,
    extract_client_system_status,
    feature,
    find_free_port,
    http_request,
    invoke_build_app_update,
    invoke_build_portable_client,
    prepare_bootstrap_sandbox,
    require_module,
    sha256_file,
    wait_until,
)


class TestTier4Scenarios(unittest.TestCase):
    """8 full real-world end-to-end deployment scenarios."""

    @feature(
        "F-01",
        "F-03",
        "F-04",
        "F-05",
        "F-06",
        "F-07",
        "F-09",
        "F-11",
        "F-12",
        "F-13",
        "F-14",
        "F-15",
        "F-16",
        "F-17",
        "F-21",
        "F-22",
        "F-23",
    )
    def test_scenario_01_fresh_direct_lan_vm_deployment(self) -> None:
        """S1: Fresh client full Direct LAN/VM deployment PREP -> ISO download -> INSTALL -> WAIT_LICENSE -> FIRST_RUN -> POST_CONFIG -> DONE."""
        proto = require_module("remote.protocol")
        with TempWorkspace("s01_direct_full_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=393216, seed=201)
            create_synthetic_post_config_assets(ws.operator_assets, version="2025")

            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
                mode="direct",
            ) as op:
                connect_path = ws.client_data / "connect.json"
                connect_path.write_text(op.get_connect_json(), encoding="utf-8")

                with op.open_sse() as sse:
                    with LiveClientHarness(
                        data_dir=ws.client_data,
                        iso_dir=ws.client_isos,
                        connect_file=connect_path,
                        driver="simulated",
                        headless=True,
                    ) as client:
                        cid = op.wait_for_client(timeout=15.0)
                        client.wait_for_status_ui(timeout=12.0)

                        # Prep controls: refresh, UAC, SAC guided, Defender exclusions
                        op.send_command(cid, proto.CommandType.REFRESH_STATUS, {})
                        op.send_command(cid, proto.CommandType.REQUEST_ELEVATION, {})
                        op.send_command(cid, proto.CommandType.SMART_APP_CONTROL, {"mode": "guided"})
                        op.send_command(
                            cid,
                            proto.CommandType.DEFENDER_CONTROL,
                            {"action": "add_exclusions", "exclusion_paths": [str(ws.client_isos)]},
                        )

                        # Start install with missing local ISO -> chunked download -> INSTALL_MAX -> WAIT_LICENSE
                        iso_meta = op.get_iso_catalog()[0]
                        op.send_command(
                            cid,
                            proto.CommandType.START_INSTALL,
                            {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                        )
                        rec_wl = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=30.0)
                        st_wl = extract_client_state(rec_wl)
                        self.assertFalse(st_wl.get("iso_skipped_download"))
                        self.assertEqual(sha256_file(pathlib.Path(st_wl["iso_local_path"])), iso.sha256)
                        self.assertEqual(list(ws.client_isos.glob("*.part*")), [])

                        # Manual licensing gate must hold until Operator confirms
                        time.sleep(0.3)
                        self.assertEqual(
                            extract_client_state(op.get_clients()[cid])["stage"], "WAIT_LICENSE"
                        )

                        # Confirm license -> FIRST_RUN -> POST_CONFIG (waiting_operator)
                        op.send_command(cid, proto.CommandType.CONFIRM_LICENSE, {})
                        op.wait_for_stage(cid, "POST_CONFIG", "waiting_operator", timeout=25.0)

                        # 1-Click POST_CONFIG -> DONE
                        op.send_command(cid, proto.CommandType.TRIGGER_POST_CONFIG, {})
                        rec_done = op.wait_for_stage(cid, "DONE", "completed", timeout=25.0)
                        st_done = extract_client_state(rec_done)
                        self.assertTrue(st_done.get("summary"), "Expected populated DONE verification summary")

                        # Verify atomic persistence & SSE delivery
                        persisted = json.loads(client.state_file_path.read_text(encoding="utf-8"))
                        self.assertEqual(persisted["stage"], "DONE")
                        self.assertEqual(persisted["status"], "completed")
                        self.assertGreater(len(sse.snapshot_events()), 0)

    @feature("F-03", "F-09", "F-10", "F-15", "F-16", "F-17", "F-21", "F-22")
    def test_scenario_02_existing_iso_fast_path_deployment(self) -> None:
        """S2: Pre-placed verified ISO skips network download completely and deploys to DONE."""
        proto = require_module("remote.protocol")
        with TempWorkspace("s02_fast_path_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2027", size_bytes=262144, seed=202)
            local_iso = ws.client_isos / iso.filename
            local_iso.write_bytes(iso.path.read_bytes())

            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
                mode="direct",
            ) as op:
                with FaultInjectingProxy(op.base_url) as proxy:
                    with LiveClientHarness(
                        data_dir=ws.client_data,
                        iso_dir=ws.client_isos,
                        mode="direct",
                        server_url=proxy.base_url,
                        session_id=op.session_id,
                        token=op.token,
                        driver="simulated",
                        headless=True,
                    ):
                        cid = op.wait_for_client(timeout=15.0)
                        iso_meta = op.get_iso_catalog()[0]
                        op.send_command(
                            cid,
                            proto.CommandType.START_INSTALL,
                            {"version": "2027", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                        )
                        rec_wl = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=25.0)
                        st_wl = extract_client_state(rec_wl)
                        self.assertTrue(st_wl.get("iso_skipped_download"))
                        self.assertEqual(len(proxy.file_downloads()), 0)
                        self.assertEqual(list(ws.client_isos.glob("*.part*")), [])

                        op.send_command(cid, proto.CommandType.CONFIRM_LICENSE, {})
                        op.wait_for_stage(cid, "POST_CONFIG", "waiting_operator", timeout=20.0)
                        op.send_command(cid, proto.CommandType.TRIGGER_POST_CONFIG, {})
                        rec_done = op.wait_for_stage(cid, "DONE", "completed", timeout=20.0)
                        self.assertTrue(extract_client_state(rec_done).get("summary"))

    @feature("F-03", "F-05", "F-07", "F-08", "F-09", "F-11", "F-21", "F-22")
    def test_scenario_03_remote_nat_relay_full_deployment(self) -> None:
        """S3: Full Operator <-> Relay <-> Client NAT-traversal deployment including chunked ISO via Relay and SSE."""
        proto = require_module("remote.protocol")
        with TempWorkspace("s03_relay_full_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=327680, seed=203)
            with LiveRelayHarness() as relay:
                with LiveOperatorHarness(
                    iso_dir=ws.operator_isos,
                    assets_dir=ws.operator_assets,
                    updates_dir=ws.operator_updates,
                    mode="relay",
                    relay_url=relay.base_url,
                ) as op:
                    with op.open_sse() as sse:
                        with LiveClientHarness(
                            data_dir=ws.client_data,
                            iso_dir=ws.client_isos,
                            mode="relay",
                            server_url=relay.base_url,
                            session_id=op.session_id,
                            token=op.token,
                            driver="simulated",
                            headless=True,
                        ):
                            cid = op.wait_for_client(timeout=15.0)
                            iso_meta = op.get_iso_catalog()[0]
                            op.send_command(
                                cid,
                                proto.CommandType.START_INSTALL,
                                {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                            )
                            rec_wl = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=35.0)
                            st_wl = extract_client_state(rec_wl)
                            self.assertFalse(st_wl.get("iso_skipped_download"))
                            self.assertEqual(sha256_file(pathlib.Path(st_wl["iso_local_path"])), iso.sha256)

                            op.send_command(cid, proto.CommandType.CONFIRM_LICENSE, {})
                            op.wait_for_stage(cid, "POST_CONFIG", "waiting_operator", timeout=25.0)
                            op.send_command(cid, proto.CommandType.TRIGGER_POST_CONFIG, {})
                            op.wait_for_stage(cid, "DONE", "completed", timeout=25.0)
                            self.assertGreater(len(sse.snapshot_events()), 0)

    @feature("F-03", "F-04", "F-11", "F-21", "F-22")
    def test_scenario_04_unstable_network_resumable_iso_recovery(self) -> None:
        """S4: Mid-transfer network drops + client crash/restart resumes from .part, verifies SHA-256, and installs."""
        proto = require_module("remote.protocol")
        with TempWorkspace("s04_unstable_net_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=524288, seed=204)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
                mode="direct",
            ) as op:
                with FaultInjectingProxy(op.base_url) as proxy:
                    proxy.fail_first_n_file_requests = 1
                    proxy.drop_after_bytes = 32768
                    proxy.drop_times = 1
                    proxy.chunk_delay_sec = 0.12

                    client = LiveClientHarness(
                        data_dir=ws.client_data,
                        iso_dir=ws.client_isos,
                        mode="direct",
                        server_url=proxy.base_url,
                        session_id=op.session_id,
                        token=op.token,
                        driver="simulated",
                        headless=True,
                    )
                    try:
                        client.start()
                        cid = op.wait_for_client(timeout=15.0)
                        iso_meta = op.get_iso_catalog()[0]
                        op.send_command(
                            cid,
                            proto.CommandType.START_INSTALL,
                            {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                        )
                        wait_until(
                            lambda: any(p.stat().st_size > 0 for p in ws.client_isos.glob("*.part")),
                            timeout=15.0,
                            desc="partial .part written before simulated client crash",
                        )
                        client.kill()

                        # Restore stable network and restart agent
                        proxy.chunk_delay_sec = 0.0
                        before_resume = len(proxy.file_downloads())
                        client.start()
                        op.wait_for_client(client_id=cid, timeout=15.0)
                        st_mid = extract_client_state(op.get_clients()[cid])
                        if st_mid.get("stage") == "DOWNLOAD_ISO" and st_mid.get("status") == "paused":
                            op.send_command(cid, proto.CommandType.RESUME, {})

                        rec_wl = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=30.0)
                        st_wl = extract_client_state(rec_wl)
                        self.assertEqual(sha256_file(pathlib.Path(st_wl["iso_local_path"])), iso.sha256)
                        self.assertEqual(list(ws.client_isos.glob("*.part*")), [])
                        after_reqs = proxy.file_downloads()[before_resume:]
                        self.assertTrue(
                            any(
                                r.get("range") and not str(r["range"]).startswith("bytes=0-")
                                for r in after_reqs
                            ),
                            f"Expected resumed Range offset > 0 after restart, got: {after_reqs}",
                        )
                    finally:
                        client.stop()

    @feature("F-03", "F-06", "F-16", "F-21", "F-22")
    def test_scenario_05_licensing_failure_recovery_and_rerun(self) -> None:
        """S5: Premature CONFIRM_LICENSE fails license check -> screenshot + bounce to WAIT_LICENSE -> re-confirm -> DONE."""
        proto = require_module("remote.protocol")
        calls = {"n": 0}

        class Scenario5FirstRunDriver:
            def run_first_run(
                self,
                version: str,
                *,
                log_cb: object,
                cancel_event: threading.Event,
                timeout_sec: int = 300,
                dump_dir: pathlib.Path | None = None,
            ) -> dict[str, object]:
                calls["n"] += 1
                if calls["n"] == 1:
                    shot_dir = dump_dir or pathlib.Path(".")
                    shot_dir.mkdir(parents=True, exist_ok=True)
                    shot = shot_dir / "s05_licensing_required.png"
                    shot.write_bytes(b"\x89PNG\r\n\x1a\nS05")
                    return {
                        "status": "licensing_required",
                        "detail": "Client 3ds Max not licensed yet (AdskLicensingAgent)",
                        "matched_rules": ["AdskLicensingAgent"],
                        "screenshots": [str(shot)],
                    }
                return {
                    "status": "ready",
                    "detail": "Main 3ds Max workspace ready",
                    "matched_rules": ["Privacy Consent", "Welcome Screen"],
                    "screenshots": [],
                }

        with TempWorkspace("s05_lic_recover_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=65536, seed=205)
            (ws.client_isos / iso.filename).write_bytes(iso.path.read_bytes())

            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
                mode="direct",
            ) as op:
                with CustomDriverAgentHarness(
                    server_url=op.base_url,
                    session_id=op.session_id,
                    token=op.token,
                    data_dir=ws.client_data,
                    iso_dir=ws.client_isos,
                    first_run=Scenario5FirstRunDriver(),
                    client_id="s05-client",
                ):
                    cid = op.wait_for_client(client_id="s05-client", timeout=15.0)
                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        proto.CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=20.0)

                    # Premature confirm -> FIRST_RUN detects licensing_required -> returns to WAIT_LICENSE
                    op.send_command(cid, proto.CommandType.CONFIRM_LICENSE, {})
                    wait_until(lambda: calls["n"] >= 1, timeout=15.0, desc="attempt 1 of FIRST_RUN")
                    rec_bounced = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=15.0)
                    self.assertIn("licens", (extract_client_state(rec_bounced).get("last_error") or "").lower())

                    # Operator completes licensing manually, then confirms again -> POST_CONFIG -> DONE
                    op.send_command(cid, proto.CommandType.CONFIRM_LICENSE, {})
                    op.wait_for_stage(cid, "POST_CONFIG", "waiting_operator", timeout=20.0)
                    op.send_command(cid, proto.CommandType.TRIGGER_POST_CONFIG, {})
                    op.wait_for_stage(cid, "DONE", "completed", timeout=20.0)
                    self.assertEqual(calls["n"], 2)

    @feature("F-04", "F-18", "F-19", "F-20", "F-21", "F-22")
    def test_scenario_06_live_background_auto_update_mid_engagement(self) -> None:
        """S6: Client paused at WAIT_LICENSE -> build 1.1.0 update -> Operator pushes -> exit 42 swap -> resumes WAIT_LICENSE -> DONE."""
        proto = require_module("remote.protocol")
        with TempWorkspace("s06_live_upd_") as ws:
            # 1. Build initial release 1.0.0 and install into portable app_root
            build_100 = ws.staging / "build_100"
            r100 = invoke_build_app_update("1.0.0", build_100, min_runtime_version="1.0.0")
            self.assertEqual(r100.returncode, 0, r100.stderr)
            zip_100 = next(build_100.glob("*.zip"))
            rel_100 = ws.app_root / "app" / "releases" / "1.0.0"
            rel_100.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_100, "r") as zf:
                zf.extractall(rel_100)

            active_path = ws.app_root / "app" / "active.json"
            active_path.write_text(
                json.dumps(
                    {
                        "version": "1.0.0",
                        "previous": None,
                        "pending_verify": False,
                        "attempts": 0,
                        "blocked_versions": [],
                    }
                ),
                encoding="utf-8",
            )
            boot_path = prepare_bootstrap_sandbox(ws.app_root)

            # 2. Pre-place ISO so client quickly reaches WAIT_LICENSE
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=65536, seed=206)
            (ws.client_isos / iso.filename).write_bytes(iso.path.read_bytes())

            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
                mode="direct",
            ) as op:
                connect_file = ws.client_data / "connect.json"
                connect_file.write_text(op.get_connect_json(), encoding="utf-8")
                status_port = find_free_port()

                boot_args = build_bootstrap_command_args(
                    boot_path,
                    ws.app_root,
                    extra_args=[
                        "--connect-file",
                        str(connect_file),
                        "--data-dir",
                        str(ws.client_data),
                        "--iso-dir",
                        str(ws.client_isos),
                        "--driver",
                        "simulated",
                        "--headless",
                        "--status-port",
                        str(status_port),
                    ],
                )
                sup = ManagedProcess(boot_args, name="s06-bootstrap", cwd=ws.app_root).start()
                try:
                    cid = op.wait_for_client(timeout=15.0)
                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        proto.CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=25.0)

                    # 3. Build 1.1.0 update directly into Operator's --updates-dir
                    r110 = invoke_build_app_update("1.1.0", ws.operator_updates, min_runtime_version="1.0.0")
                    self.assertEqual(r110.returncode, 0, r110.stderr)

                    hdrs = auth_headers(op.token, op.session_id)
                    wait_until(
                        lambda: http_request(op.base_url, "GET", "/api/v1/updates/latest", headers=hdrs).status == 200,
                        timeout=10.0,
                        desc="Operator serving 1.1.0 update manifest",
                    )

                    # 4. Push update while at WAIT_LICENSE -> exit 42 -> supervisor hot-swaps -> resumes WAIT_LICENSE
                    op.send_command(cid, proto.CommandType.PUSH_UPDATE, {})

                    def _swapped_and_verified() -> bool:
                        if not active_path.exists():
                            return False
                        act = json.loads(active_path.read_text(encoding="utf-8"))
                        if act.get("version") != "1.1.0" or act.get("pending_verify") is not False:
                            return False
                        rec = op.get_clients().get(cid)
                        if not rec:
                            return False
                        sys_st = extract_client_system_status(rec)
                        st = extract_client_state(rec)
                        return sys_st.get("agent_version") == "1.1.0" and st.get("stage") == "WAIT_LICENSE"

                    wait_until(
                        _swapped_and_verified,
                        timeout=30.0,
                        desc="hot-swap to 1.1.0 with WAIT_LICENSE preserved and pending_verify cleared",
                        diagnostics=lambda: f"active.json={active_path.read_text(encoding='utf-8')}\n{sup.output_tail()}",
                    )

                    # 5. Finish engagement from WAIT_LICENSE -> DONE under new 1.1.0 version
                    op.send_command(cid, proto.CommandType.CONFIRM_LICENSE, {})
                    op.wait_for_stage(cid, "POST_CONFIG", "waiting_operator", timeout=20.0)
                    op.send_command(cid, proto.CommandType.TRIGGER_POST_CONFIG, {})
                    op.wait_for_stage(cid, "DONE", "completed", timeout=20.0)
                finally:
                    sup.stop()

    @feature("F-18", "F-19", "F-20", "F-21", "F-22")
    def test_scenario_07_bad_auto_update_automatic_rollback(self) -> None:
        """S7: Broken 1.1.0 update pushed -> crashes on startup -> supervisor rolls back to 1.0.0 & blacklists 1.1.0."""
        proto = require_module("remote.protocol")
        with TempWorkspace("s07_bad_upd_") as ws:
            build_100 = ws.staging / "build_100"
            r100 = invoke_build_app_update("1.0.0", build_100, min_runtime_version="1.0.0")
            self.assertEqual(r100.returncode, 0, r100.stderr)
            zip_100 = next(build_100.glob("*.zip"))
            rel_100 = ws.app_root / "app" / "releases" / "1.0.0"
            rel_100.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(zip_100, "r") as zf:
                zf.extractall(rel_100)

            active_path = ws.app_root / "app" / "active.json"
            active_path.write_text(
                json.dumps(
                    {
                        "version": "1.0.0",
                        "previous": None,
                        "pending_verify": False,
                        "attempts": 0,
                        "blocked_versions": [],
                    }
                ),
                encoding="utf-8",
            )
            boot_path = prepare_bootstrap_sandbox(ws.app_root)

            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
                mode="direct",
            ) as op:
                connect_file = ws.client_data / "connect.json"
                connect_file.write_text(op.get_connect_json(), encoding="utf-8")
                status_port = find_free_port()

                boot_args = build_bootstrap_command_args(
                    boot_path,
                    ws.app_root,
                    extra_args=[
                        "--connect-file",
                        str(connect_file),
                        "--data-dir",
                        str(ws.client_data),
                        "--iso-dir",
                        str(ws.client_isos),
                        "--driver",
                        "simulated",
                        "--headless",
                        "--status-port",
                        str(status_port),
                    ],
                )
                sup = ManagedProcess(boot_args, name="s07-bootstrap", cwd=ws.app_root).start()
                try:
                    cid = op.wait_for_client(timeout=15.0)

                    # Publish a broken 1.1.0 zip with valid SHA-256 & manifest that immediately crashes on boot
                    bad_zip, bad_sha, bad_size = create_app_update_zip(
                        ws.operator_updates / "app-1.1.0.zip",
                        "1.1.0",
                        files={
                            "remote/__init__.py": b"__version__ = '1.1.0'\n",
                            "remote/client/__init__.py": b"",
                            "remote/client/main.py": b"import sys\nsys.stderr.write('simulated bad release crash\\n')\nsys.exit(1)\n",
                        },
                    )
                    (ws.operator_updates / "manifest.json").write_text(
                        json.dumps(
                            {
                                "version": "1.1.0",
                                "min_runtime_version": "1.0.0",
                                "file_id": "app-1.1.0",
                                "filename": bad_zip.name,
                                "size_bytes": bad_size,
                                "sha256": bad_sha,
                                "notes": "Deliberately broken release for rollback test",
                            }
                        ),
                        encoding="utf-8",
                    )

                    hdrs = auth_headers(op.token, op.session_id)
                    wait_until(
                        lambda: http_request(op.base_url, "GET", "/api/v1/updates/latest", headers=hdrs).status == 200,
                        timeout=10.0,
                        desc="Operator serving broken 1.1.0 update manifest",
                    )

                    op.send_command(cid, proto.CommandType.PUSH_UPDATE, {})

                    def _rolled_back() -> bool:
                        act = json.loads(active_path.read_text(encoding="utf-8"))
                        return (
                            act.get("version") == "1.0.0"
                            and "1.1.0" in (act.get("blocked_versions") or [])
                            and act.get("pending_verify") is False
                        )

                    wait_until(
                        _rolled_back,
                        timeout=30.0,
                        desc="automatic rollback to 1.0.0 and blacklisting of 1.1.0",
                        diagnostics=lambda: f"active.json={active_path.read_text(encoding='utf-8')}\n{sup.output_tail()}",
                    )

                    # Verify recovered 1.0.0 client responds cleanly to Operator
                    op.send_command(cid, proto.CommandType.REFRESH_STATUS, {})
                    op.wait_for_stage(cid, "PREP", "idle", timeout=15.0)
                finally:
                    sup.stop()

    @feature("F-01", "F-02", "F-05", "F-07", "F-20", "F-21", "F-22", "F-23")
    def test_scenario_08_portable_bundle_and_multi_client_operator_panel(self) -> None:
        """S8: Build portable client bundle & verify layout; control 2 concurrent clients independently via Operator UI API/SSE."""
        proto = require_module("remote.protocol")
        with TempWorkspace("s08_portable_multi_") as ws:
            # Part A: Build portable client bundle + verify structure
            r_port = invoke_build_portable_client(ws.out, version="1.0.0", offline=True)
            self.assertEqual(
                r_port.returncode,
                0,
                f"build_portable_client.py failed:\n{r_port.stdout}\n{r_port.stderr}",
            )
            built_files = [p.relative_to(ws.out).as_posix() for p in ws.out.rglob("*") if p.is_file()]
            self.assertTrue(
                any(f.endswith("Start-MaxAgent.bat") for f in built_files),
                f"Missing Start-MaxAgent.bat in portable build: {built_files[:30]}",
            )
            self.assertTrue(
                any(f.endswith("bootstrap.py") for f in built_files),
                f"Missing runtime/bootstrap.py in portable build: {built_files[:30]}",
            )
            self.assertTrue(
                any(f.endswith("active.json") for f in built_files),
                f"Missing app/active.json in portable build: {built_files[:30]}",
            )
            pth_files = [p for p in ws.out.rglob("*._pth")]
            if pth_files:
                pth_text = pth_files[0].read_text(encoding="utf-8")
                self.assertRegex(pth_text, r"(?m)^import site\s*$")

            r_upd = invoke_build_app_update("1.0.0", ws.operator_updates)
            self.assertEqual(r_upd.returncode, 0, r_upd.stderr)
            self.assertTrue(any(ws.operator_updates.glob("*.json")), "Missing manifest.json from update build")

            # Part B: 2 concurrent clients connected to 1 Operator Control Panel with strict isolation
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=65536, seed=208)
            data_a = ws.root / "client_a_data"
            iso_a = ws.root / "client_a_isos"
            data_b = ws.root / "client_b_data"
            iso_b = ws.root / "client_b_isos"
            for d in (data_a, iso_a, data_b, iso_b):
                d.mkdir(parents=True, exist_ok=True)
            (iso_a / iso.filename).write_bytes(iso.path.read_bytes())

            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
                mode="direct",
            ) as op:
                with op.open_sse() as sse:
                    with (
                        LiveClientHarness(
                            data_dir=data_a,
                            iso_dir=iso_a,
                            mode="direct",
                            server_url=op.base_url,
                            session_id=op.session_id,
                            token=op.token,
                            driver="simulated",
                            headless=True,
                        ) as client_a,
                        LiveClientHarness(
                            data_dir=data_b,
                            iso_dir=iso_b,
                            mode="direct",
                            server_url=op.base_url,
                            session_id=op.session_id,
                            token=op.token,
                            driver="simulated",
                            headless=True,
                        ) as client_b,
                    ):
                        wait_until(
                            lambda: len(op.get_clients()) >= 2,
                            timeout=15.0,
                            desc="2 concurrent clients registered on Operator",
                        )
                        client_ids = sorted(op.get_clients().keys())
                        self.assertEqual(len(client_ids), 2)
                        cid_a, cid_b = client_ids[0], client_ids[1]

                        # Verify both localhost Client Status UIs respond independently
                        self.assertEqual(client_a.wait_for_status_ui(timeout=10.0).status, 200)
                        self.assertEqual(client_b.wait_for_status_ui(timeout=10.0).status, 200)

                        # Drive Client A all the way to DONE while Client B stays in PREP/idle
                        iso_meta = op.get_iso_catalog()[0]
                        op.send_command(
                            cid_a,
                            proto.CommandType.START_INSTALL,
                            {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                        )
                        op.wait_for_stage(cid_a, "WAIT_LICENSE", "waiting_operator", timeout=25.0)
                        op.send_command(cid_a, proto.CommandType.CONFIRM_LICENSE, {})
                        op.wait_for_stage(cid_a, "POST_CONFIG", "waiting_operator", timeout=20.0)
                        op.send_command(cid_a, proto.CommandType.TRIGGER_POST_CONFIG, {})
                        op.wait_for_stage(cid_a, "DONE", "completed", timeout=20.0)

                        # Client B must still be untouched in PREP/idle
                        op.send_command(cid_b, proto.CommandType.REFRESH_STATUS, {})
                        st_b = extract_client_state(op.get_clients()[cid_b])
                        self.assertEqual(st_b["stage"], "PREP")
                        self.assertEqual(st_b["status"], "idle")
                        self.assertGreater(len(sse.snapshot_events()), 0)
