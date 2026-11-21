"""Tier 3 — Cross-Feature Pairwise Combination Tests (16 tests).

Exercises pairwise interactions between transport, state persistence, ISO pre-check,
resumable transfer, pause/resume/abort/retry, licensing bounce-back, security
controls, version-aware POST_CONFIG, auto-update deferral/staging, and UI sync.
"""

from __future__ import annotations

import inspect
import json
import os
import pathlib
import threading
import time
import unittest
import unittest.mock

from tests.e2e.helpers import (
    CustomDriverAgentHarness,
    FaultInjectingProxy,
    FaultyRangeServer,
    LiveClientHarness,
    LiveOperatorHarness,
    LiveRelayHarness,
    ManagedProcess,
    REPO_ROOT,
    TempWorkspace,
    auth_headers,
    build_bootstrap_command_args,
    create_synthetic_iso,
    create_synthetic_post_config_assets,
    discover_simulated_drivers,
    extract_client_state,
    extract_client_system_status,
    feature,
    find_free_port,
    http_request,
    invoke_build_app_update,
    prepare_bootstrap_sandbox,
    require_attr,
    require_module,
    sha256_file,
    wait_until,
)


class TestTier3TransportTransferAndRecoveryCombinations(unittest.TestCase):
    """Pairwise combinations of Direct/Relay transport, ISO catalog/pre-check, transfer & recovery."""

    @feature("F-03", "F-04", "F-07", "F-09", "F-11")
    def test_direct_http_iso_transfer_plus_state_persistence(self) -> None:
        """Direct HTTP Range ISO transfer updates TransferProgress and persists session_state.json."""
        proto = require_module("remote.protocol")
        with TempWorkspace("t3_direct_persist_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=393216, seed=101)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with LiveClientHarness(
                    data_dir=ws.client_data,
                    iso_dir=ws.client_isos,
                    mode="direct",
                    server_url=op.base_url,
                    session_id=op.session_id,
                    token=op.token,
                    driver="simulated",
                    headless=True,
                ) as client:
                    cid = op.wait_for_client(timeout=15.0)
                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        proto.CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    rec = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=25.0)
                    st = extract_client_state(rec)
                    self.assertTrue(client.state_file_path.exists())
                    persisted = json.loads(client.state_file_path.read_text(encoding="utf-8"))
                    self.assertEqual(persisted["stage"], "WAIT_LICENSE")
                    self.assertEqual(persisted["iso_local_path"], st["iso_local_path"])
                    self.assertEqual(sha256_file(pathlib.Path(st["iso_local_path"])), iso.sha256)

    @feature("F-05", "F-07", "F-08", "F-22")
    def test_relay_command_long_poll_plus_operator_sse_stream(self) -> None:
        """Commands sent in Relay mode traverse Relay long-poll and trigger Operator SSE updates."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t3_relay_sse_") as ws:
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
                            cmd = op.send_command(cid, CommandType.REFRESH_STATUS, {})
                            ev = sse.wait_for_event(
                                lambda e: cid in e.data_raw or cmd["cmd_id"] in e.data_raw,
                                timeout=10.0,
                            )
                            self.assertTrue(ev.data_raw)

    @feature("F-08", "F-09", "F-11")
    def test_relay_streamed_range_iso_transfer_plus_sha256_verify(self) -> None:
        """Multi-chunk ISO download routed entirely through Relay verifies exact SHA-256."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t3_relay_iso_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=327680, seed=102)
            with LiveRelayHarness() as relay:
                with LiveOperatorHarness(
                    iso_dir=ws.operator_isos,
                    assets_dir=ws.operator_assets,
                    updates_dir=ws.operator_updates,
                    mode="relay",
                    relay_url=relay.base_url,
                ) as op:
                    with FaultInjectingProxy(relay.base_url) as proxy:
                        proxy.fail_first_n_file_requests = 1
                        with LiveClientHarness(
                            data_dir=ws.client_data,
                            iso_dir=ws.client_isos,
                            mode="relay",
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
                                CommandType.START_INSTALL,
                                {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                            )
                            rec = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=30.0)
                            st = extract_client_state(rec)
                            self.assertFalse(st.get("iso_skipped_download"))
                            self.assertEqual(sha256_file(pathlib.Path(st["iso_local_path"])), iso.sha256)
                            self.assertGreaterEqual(len(proxy.file_downloads()), 2)

    @feature("F-08", "F-09", "F-11")
    def test_relay_control_plane_plus_direct_download_url_bypass(self) -> None:
        """Relay handles control messages while direct download_url handles all ISO Range bytes."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t3_relay_bypass_") as ws:
            iso = create_synthetic_iso(ws.staging, version="2027", size_bytes=262144, seed=103)
            with FaultyRangeServer() as direct_srv:
                direct_url = direct_srv.register_file("direct-2027", iso.path, iso.filename)
                with LiveRelayHarness() as relay:
                    with FaultInjectingProxy(relay.base_url) as relay_proxy:
                        with LiveOperatorHarness(
                            iso_dir=ws.operator_isos,
                            assets_dir=ws.operator_assets,
                            updates_dir=ws.operator_updates,
                            mode="relay",
                            relay_url=relay.base_url,
                        ) as op:
                            with LiveClientHarness(
                                data_dir=ws.client_data,
                                iso_dir=ws.client_isos,
                                mode="relay",
                                server_url=relay_proxy.base_url,
                                session_id=op.session_id,
                                token=op.token,
                                driver="simulated",
                                headless=True,
                            ):
                                cid = op.wait_for_client(timeout=15.0)
                                spec = {
                                    "file_id": "direct-2027",
                                    "filename": iso.filename,
                                    "version": "2027",
                                    "size_bytes": iso.size_bytes,
                                    "sha256": iso.sha256,
                                    "download_url": direct_url,
                                }
                                op.send_command(
                                    cid,
                                    CommandType.START_INSTALL,
                                    {"version": "2027", "iso_spec": spec, "file_id": "direct-2027"},
                                )
                                rec = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=25.0)
                                st = extract_client_state(rec)
                                self.assertEqual(sha256_file(pathlib.Path(st["iso_local_path"])), iso.sha256)
                                self.assertGreater(len(direct_srv.get_file_requests()), 0)
                                self.assertEqual(
                                    len(relay_proxy.file_downloads()),
                                    0,
                                    "Relay must not carry ISO bytes when download_url bypass is used",
                                )

    @feature("F-03", "F-10", "F-15", "F-21")
    def test_local_iso_precheck_hit_plus_skip_download_into_install_max(self) -> None:
        """Local verified ISO hit skips download, runs INSTALL_MAX, and stops at WAIT_LICENSE."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t3_precheck_inst_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=196608, seed=104)
            (ws.client_isos / iso.filename).write_bytes(iso.path.read_bytes())

            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
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
                            CommandType.START_INSTALL,
                            {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                        )
                        rec = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=25.0)
                        st = extract_client_state(rec)
                        self.assertTrue(st.get("iso_skipped_download"))
                        self.assertEqual(st.get("install_percent"), 100)
                        self.assertEqual(len(proxy.file_downloads()), 0)

    @feature("F-04", "F-11", "F-21")
    def test_interrupted_iso_download_plus_agent_restart_resume(self) -> None:
        """Killing client mid-download and restarting resumes from .part offset and completes."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t3_restart_resume_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=524288, seed=105)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with FaultInjectingProxy(op.base_url) as proxy:
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
                            CommandType.START_INSTALL,
                            {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                        )
                        # Wait until at least one chunk has been written to .part
                        wait_until(
                            lambda: any(p.stat().st_size > 0 for p in ws.client_isos.glob("*.part")),
                            timeout=12.0,
                            desc="partial .part file written during throttled transfer",
                        )
                        client.kill()

                        # Clear throttle and restart client with same data-dir & iso-dir
                        proxy.chunk_delay_sec = 0.0
                        req_count_before = len(proxy.file_downloads())
                        client.start()
                        op.wait_for_client(client_id=cid, timeout=15.0)
                        # If recovered to DOWNLOAD_ISO/paused, send RESUME
                        cur_st = extract_client_state(op.get_clients()[cid])
                        if cur_st.get("stage") == "DOWNLOAD_ISO" and cur_st.get("status") == "paused":
                            op.send_command(cid, CommandType.RESUME, {})
                        rec = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=25.0)
                        st = extract_client_state(rec)
                        self.assertEqual(sha256_file(pathlib.Path(st["iso_local_path"])), iso.sha256)
                        resumed_reqs = proxy.file_downloads()[req_count_before:]
                        self.assertTrue(
                            any(
                                r.get("range") and not str(r["range"]).startswith("bytes=0-")
                                for r in resumed_reqs
                            ),
                            f"Expected resumed Range request from offset > 0, got: {resumed_reqs}",
                        )
                    finally:
                        client.stop()


class TestTier3LifecycleSecurityAndPostConfigCombinations(unittest.TestCase):
    """Pairwise combinations of lifecycle controls, licensing bounce, security controls & POST_CONFIG."""

    @feature("F-03", "F-04", "F-17", "F-21")
    def test_pause_resume_lifecycle_across_post_config_idempotency(self) -> None:
        """Partial POST_CONFIG failure followed by 1-click retry skips already-done steps."""
        proto = require_module("remote.protocol")
        executed_steps: list[str] = []
        fail_once = {"triggered": False}

        class FlakyPostConfigDriver:
            def load_steps(
                self, config_path: pathlib.Path | None = None, version: str | None = None
            ) -> list[object]:
                return [
                    proto.PostConfigStepState("s1_corona", "Install Corona", "renderer", "pending"),
                    proto.PostConfigStepState("s2_theme", "Deploy Theme", "theme", "pending"),
                    proto.PostConfigStepState("s3_scripts", "Deploy Scripts", "scripts", "pending"),
                    proto.PostConfigStepState("s4_verify", "Verify Max", "verify", "pending"),
                ]

            def run_step(
                self,
                step_id: str,
                version: str,
                *,
                assets_dir: pathlib.Path | None,
                log_cb: object,
                cancel_event: threading.Event,
                config_path: pathlib.Path | None = None,
            ) -> dict[str, object]:
                executed_steps.append(step_id)
                if step_id == "s3_scripts" and not fail_once["triggered"]:
                    fail_once["triggered"] = True
                    return {"ok": False, "status": "failed", "detail": "Transient script copy error"}
                return {"ok": True, "status": "done", "detail": f"{step_id} ok"}

            def verify_installation(self, version: str) -> dict[str, object]:
                return {"version": version, "verified": True}

        with TempWorkspace("t3_pc_idemp_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=32768, seed=106)
            (ws.client_isos / iso.filename).write_bytes(iso.path.read_bytes())
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with CustomDriverAgentHarness(
                    server_url=op.base_url,
                    session_id=op.session_id,
                    token=op.token,
                    data_dir=ws.client_data,
                    iso_dir=ws.client_isos,
                    post_config=FlakyPostConfigDriver(),
                    client_id="pc-idemp-client",
                ):
                    cid = op.wait_for_client(client_id="pc-idemp-client", timeout=15.0)
                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        proto.CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=20.0)
                    op.send_command(cid, proto.CommandType.CONFIRM_LICENSE, {})
                    op.wait_for_stage(cid, "POST_CONFIG", "waiting_operator", timeout=20.0)
                    op.send_command(cid, proto.CommandType.TRIGGER_POST_CONFIG, {})
                    op.wait_for_stage(cid, "POST_CONFIG", "failed", timeout=20.0)

                    # Retry POST_CONFIG: s1_corona & s2_theme must NOT re-execute!
                    op.send_command(cid, proto.CommandType.RETRY, {})
                    op.wait_for_stage(cid, "DONE", "completed", timeout=20.0)
                    self.assertEqual(executed_steps.count("s1_corona"), 1, f"s1 re-ran: {executed_steps}")
                    self.assertEqual(executed_steps.count("s2_theme"), 1, f"s2 re-ran: {executed_steps}")
                    self.assertEqual(executed_steps.count("s3_scripts"), 2)
                    self.assertEqual(executed_steps.count("s4_verify"), 1)

    @feature("F-03", "F-16", "F-21", "F-22")
    def test_first_run_licensing_required_bounce_to_wait_license_and_recovery(self) -> None:
        """FIRST_RUN licensing_required bounces to WAIT_LICENSE, then succeeds on re-confirm."""
        proto = require_module("remote.protocol")
        attempts = {"count": 0}

        class BounceFirstRunDriver:
            def run_first_run(
                self,
                version: str,
                *,
                log_cb: object,
                cancel_event: threading.Event,
                timeout_sec: int = 300,
                dump_dir: pathlib.Path | None = None,
            ) -> dict[str, object]:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    shot = (dump_dir or pathlib.Path(".")) / "license_dialog.png"
                    shot.parent.mkdir(parents=True, exist_ok=True)
                    shot.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
                    return {
                        "status": "licensing_required",
                        "detail": "Autodesk licensing sign-in dialog detected",
                        "matched_rules": ["AdskLicensingAgent"],
                        "screenshots": [str(shot)],
                    }
                return {
                    "status": "ready",
                    "detail": "Main 3ds Max workspace ready",
                    "matched_rules": ["Privacy Consent"],
                    "screenshots": [],
                }

        with TempWorkspace("t3_lic_bounce_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=32768, seed=107)
            (ws.client_isos / iso.filename).write_bytes(iso.path.read_bytes())
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with CustomDriverAgentHarness(
                    server_url=op.base_url,
                    session_id=op.session_id,
                    token=op.token,
                    data_dir=ws.client_data,
                    iso_dir=ws.client_isos,
                    first_run=BounceFirstRunDriver(),
                    client_id="lic-bounce-client",
                ):
                    cid = op.wait_for_client(client_id="lic-bounce-client", timeout=15.0)
                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        proto.CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=20.0)

                    # First confirm hits licensing_required and bounces back to WAIT_LICENSE
                    op.send_command(cid, proto.CommandType.CONFIRM_LICENSE, {})
                    wait_until(lambda: attempts["count"] >= 1, timeout=15.0, desc="first_run attempt 1")
                    rec_bounce = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=15.0)
                    st_bounce = extract_client_state(rec_bounce)
                    self.assertIn("licens", (st_bounce.get("last_error") or "").lower())

                    # Second confirm succeeds into POST_CONFIG
                    op.send_command(cid, proto.CommandType.CONFIRM_LICENSE, {})
                    op.wait_for_stage(cid, "POST_CONFIG", "waiting_operator", timeout=15.0)
                    self.assertEqual(attempts["count"], 2)

    @feature("F-05", "F-07", "F-21", "F-22")
    def test_connect_json_and_maxpair_uri_handoff_to_live_client_boot(self) -> None:
        """Fetch connect.json -> maxpair:// -> connect.json -> boot live client via --connect-file."""
        PairingConfig = require_attr("remote.auth", "PairingConfig")
        with TempWorkspace("t3_pair_handoff_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                raw_json = op.get_connect_json()
                cfg1 = PairingConfig.from_connect_json(raw_json)
                uri = cfg1.to_pairing_uri()
                cfg2 = PairingConfig.from_pairing_uri(uri)
                connect_file = ws.client_data / "connect.json"
                connect_file.write_text(cfg2.to_connect_json(), encoding="utf-8")

                with LiveClientHarness(
                    data_dir=ws.client_data,
                    iso_dir=ws.client_isos,
                    connect_file=connect_file,
                    driver="simulated",
                    headless=True,
                ):
                    cid = op.wait_for_client(timeout=15.0)
                    op.wait_for_stage(cid, "PREP", "idle", timeout=10.0)

    @feature("F-13", "F-14", "F-21", "F-22")
    def test_remote_sac_and_defender_commands_with_tamper_protection_honesty(self) -> None:
        """Operator SAC + Defender exclusion/restore commands update Client SystemStatus truthfully."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t3_sec_cmds_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with LiveClientHarness(
                    data_dir=ws.client_data,
                    iso_dir=ws.client_isos,
                    mode="direct",
                    server_url=op.base_url,
                    session_id=op.session_id,
                    token=op.token,
                    driver="simulated",
                    headless=True,
                ):
                    cid = op.wait_for_client(timeout=15.0)
                    excl_path = str(ws.client_isos)
                    op.send_command(cid, CommandType.SMART_APP_CONTROL, {"mode": "guided"})
                    op.send_command(
                        cid,
                        CommandType.DEFENDER_CONTROL,
                        {"action": "add_exclusions", "exclusion_paths": [excl_path]},
                    )
                    op.send_command(cid, CommandType.REFRESH_STATUS, {})

                    wait_until(
                        lambda: excl_path
                        in (
                            extract_client_system_status(op.get_clients().get(cid, {})).get(
                                "defender_exclusions"
                            )
                            or []
                        ),
                        timeout=12.0,
                        desc="Defender exclusion recorded in Operator snapshot",
                    )
                    op.send_command(cid, CommandType.DEFENDER_CONTROL, {"action": "restore"})
                    op.send_command(cid, CommandType.REFRESH_STATUS, {})
                    wait_until(
                        lambda: excl_path
                        not in (
                            extract_client_system_status(op.get_clients().get(cid, {})).get(
                                "defender_exclusions"
                            )
                            or []
                        ),
                        timeout=12.0,
                        desc="Defender exclusion restored/removed",
                    )

    @feature("F-03", "F-04", "F-18", "F-19")
    def test_auto_update_deferred_during_install_max_and_applied_at_wait_license(self) -> None:
        """Auto-update is refused during INSTALL_MAX/running, then accepted at WAIT_LICENSE preserving state."""
        proto = require_module("remote.protocol")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")
        with TempWorkspace("t3_upd_wait_lic_") as ws:
            state_path = ws.client_data / "session_state.json"
            sm = DeploymentStateMachine(state_path=state_path, session_id="S-UWL", client_id="c-uwl")
            sm.start_install("2025")
            sm.complete_iso_download("/tmp/max2025.iso", skipped=True)
            self.assertFalse(sm.can_accept_update())

            sm.complete_install()
            self.assertTrue(sm.can_accept_update())
            self.assertEqual(sm.state.stage, proto.Stage.WAIT_LICENSE)

            # Simulate supervisor restart after update swap: state MUST survive intact
            reloaded = DeploymentStateMachine.load_or_create(state_path, "S-UWL", "c-uwl")
            self.assertEqual(reloaded.state.stage, proto.Stage.WAIT_LICENSE)
            self.assertEqual(reloaded.state.status, proto.StageStatus.WAITING_OPERATOR)
            self.assertTrue(reloaded.state.iso_skipped_download)

    @feature("F-18", "F-20", "F-22")
    def test_build_app_update_to_operator_catalog_to_client_staging(self) -> None:
        """build_app_update.py -> Operator /api/v1/updates/latest -> remote.updater stages & swaps active.json."""
        UpdateManifest = require_attr("remote.protocol", "UpdateManifest")
        updater_mod = require_module("remote.updater")
        with TempWorkspace("t3_build_serve_stage_") as ws:
            res = invoke_build_app_update("1.2.0", ws.operator_updates, min_runtime_version="1.0.0")
            self.assertEqual(res.returncode, 0, res.stderr)

            app_dir = ws.app_root / "app"
            (app_dir / "releases" / "1.0.0").mkdir(parents=True, exist_ok=True)
            (app_dir / "active.json").write_text(
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

            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                hdrs = auth_headers(op.token, op.session_id)
                r_man = http_request(op.base_url, "GET", "/api/v1/updates/latest", headers=hdrs)
                self.assertEqual(r_man.status, 200, r_man.text())
                manifest = UpdateManifest.from_dict(r_man.json())
                self.assertEqual(manifest.version, "1.2.0")

                r_zip = http_request(
                    op.base_url, "GET", f"/api/v1/files/{manifest.file_id}", headers=hdrs
                )
                self.assertEqual(r_zip.status, 200, r_zip.text())
                downloaded = ws.staging / manifest.filename
                downloaded.write_bytes(r_zip.body)
                self.assertEqual(sha256_file(downloaded), manifest.sha256)

                updater_cls = getattr(updater_mod, "AppUpdater", None) or getattr(updater_mod, "Updater")
                sig = inspect.signature(updater_cls.__init__)
                kw: dict[str, object] = {}
                for k, v in (
                    ("app_dir", app_dir),
                    ("root_dir", ws.app_root),
                    ("base_dir", ws.app_root),
                    ("runtime_version", "1.0.0"),
                    ("current_version", "1.0.0"),
                ):
                    if k in sig.parameters:
                        kw[k] = v
                upd = updater_cls(**kw)
                apply_fn = (
                    getattr(upd, "apply_update", None)
                    or getattr(upd, "stage_and_activate", None)
                    or getattr(upd, "download_and_apply")
                )
                a_sig = inspect.signature(apply_fn)
                a_kw: dict[str, object] = {}
                if "archive_path" in a_sig.parameters:
                    a_kw["archive_path"] = downloaded
                elif "download_url" in a_sig.parameters:
                    a_kw["download_url"] = f"{op.base_url}/api/v1/files/{manifest.file_id}"
                if "headers" in a_sig.parameters:
                    a_kw["headers"] = hdrs
                apply_fn(manifest, **a_kw)

            self.assertTrue((app_dir / "releases" / "1.2.0" / "remote" / "protocol.py").exists())
            active = json.loads((app_dir / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["version"], "1.2.0")
            self.assertEqual(active["previous"], "1.0.0")
            self.assertTrue(active["pending_verify"])

    @feature("F-17", "F-21")
    def test_post_config_2024_vs_2025_version_assets_plus_enu_backup_and_verify(self) -> None:
        """Full WindowsPostConfigDriver pipeline backs up ENU, deploys versioned UI+scripts, and verifies."""
        WindowsPostConfigDriver = require_attr("remote.drivers.windows", "WindowsPostConfigDriver")
        drv = WindowsPostConfigDriver()
        with TempWorkspace("t3_pc_full_") as ws:
            create_synthetic_post_config_assets(ws.operator_assets, version="2025")
            for ver in ("2024", "2025"):
                enu = ws.client_enu / "Autodesk" / "3dsMax" / f"{ver} - 64bit" / "ENU"
                enu.mkdir(parents=True, exist_ok=True)
                (enu / "3dsmax.ini").write_text(f"[PreBackup]\nVer={ver}\n", encoding="utf-8")

            with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": str(ws.client_enu)}):
                for ver in ("2024", "2025"):
                    steps = drv.load_steps(
                        config_path=REPO_ROOT / "config/post_config.yaml", version=ver
                    )
                    for step in steps:
                        if step.category in ("theme", "quad_menu", "hotkey", "scripts"):
                            res = drv.run_step(
                                step.step_id,
                                ver,
                                assets_dir=ws.operator_assets,
                                log_cb=lambda l, m: None,
                                cancel_event=threading.Event(),
                                config_path=REPO_ROOT / "config/post_config.yaml",
                            )
                            self.assertIsInstance(res, dict)
                    ver_res = drv.verify_installation(ver)
                    self.assertIsInstance(ver_res, dict)

                # Confirm ENU backups were created for both versions
                all_dirs = [p.name.lower() for p in ws.client_enu.rglob("*") if p.is_dir() or p.suffix == ".zip"]
                self.assertTrue(
                    any("backup" in name or "bak" in name for name in all_dirs),
                    f"Expected ENU backup artifact in {ws.client_enu}, found: {all_dirs}",
                )

    @feature("F-03", "F-04", "F-15", "F-21")
    def test_abort_during_install_max_plus_retry_to_wait_license(self) -> None:
        """ABORT during INSTALL_MAX cancels cleanly into aborted, then RETRY succeeds into WAIT_LICENSE."""
        proto = require_module("remote.protocol")
        started = threading.Event()
        attempts = {"count": 0}

        class AbortableInstallerDriver:
            def install_3dsmax(
                self,
                version: str,
                iso_path: pathlib.Path,
                *,
                progress_cb: object,
                log_cb: object,
                cancel_event: threading.Event,
                timeout_sec: int = 2400,
            ) -> dict[str, object]:
                attempts["count"] += 1
                if attempts["count"] == 1:
                    started.set()
                    progress_cb(30, "Installing...")
                    if cancel_event.wait(timeout=10.0):
                        return {"ok": False, "status": "aborted", "detail": "Operator aborted"}
                progress_cb(100, "Installed")
                return {"ok": True, "status": "completed"}

            def is_max_installed(self, version: str) -> bool:
                return attempts["count"] >= 2

            def is_installer_running(self) -> bool:
                return False

        with TempWorkspace("t3_abort_retry_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=32768, seed=108)
            (ws.client_isos / iso.filename).write_bytes(iso.path.read_bytes())
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with CustomDriverAgentHarness(
                    server_url=op.base_url,
                    session_id=op.session_id,
                    token=op.token,
                    data_dir=ws.client_data,
                    iso_dir=ws.client_isos,
                    installer=AbortableInstallerDriver(),
                    client_id="abort-retry-client",
                ):
                    cid = op.wait_for_client(client_id="abort-retry-client", timeout=15.0)
                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        proto.CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    self.assertTrue(started.wait(timeout=15.0))
                    op.send_command(cid, proto.CommandType.ABORT, {"reason": "operator test abort"})
                    op.wait_for_stage(cid, "INSTALL_MAX", "aborted", timeout=15.0)

                    op.send_command(cid, proto.CommandType.RETRY, {})
                    op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=20.0)
                    self.assertEqual(attempts["count"], 2)

    @feature("F-03", "F-21", "F-22")
    def test_skip_stage_wait_license_and_post_config_to_done_summary(self) -> None:
        """SKIP_STAGE advances WAIT_LICENSE -> POST_CONFIG -> DONE and persists DONE state."""
        proto = require_module("remote.protocol")
        with TempWorkspace("t3_skip_stage_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=32768, seed=109)
            (ws.client_isos / iso.filename).write_bytes(iso.path.read_bytes())
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with LiveClientHarness(
                    data_dir=ws.client_data,
                    iso_dir=ws.client_isos,
                    mode="direct",
                    server_url=op.base_url,
                    session_id=op.session_id,
                    token=op.token,
                    driver="simulated",
                    headless=True,
                ) as client:
                    cid = op.wait_for_client(timeout=15.0)
                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        proto.CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=20.0)
                    op.send_command(
                        cid,
                        proto.CommandType.SKIP_STAGE,
                        {"target_stage": proto.Stage.POST_CONFIG.value},
                    )
                    op.wait_for_stage(cid, "POST_CONFIG", timeout=15.0)
                    op.send_command(
                        cid,
                        proto.CommandType.SKIP_STAGE,
                        {"target_stage": proto.Stage.DONE.value},
                    )
                    op.wait_for_stage(cid, "DONE", "completed", timeout=15.0)
                    persisted = json.loads(client.state_file_path.read_text(encoding="utf-8"))
                    self.assertEqual(persisted["stage"], "DONE")
                    self.assertEqual(persisted["status"], "completed")

    @feature("F-21", "F-22", "F-23")
    def test_client_status_ui_live_sync_with_operator_commands_and_logs(self) -> None:
        """Client localhost Status UI dynamically reflects Operator-driven stage transitions."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t3_ui_sync_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=32768, seed=110)
            (ws.client_isos / iso.filename).write_bytes(iso.path.read_bytes())
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with LiveClientHarness(
                    data_dir=ws.client_data,
                    iso_dir=ws.client_isos,
                    mode="direct",
                    server_url=op.base_url,
                    session_id=op.session_id,
                    token=op.token,
                    driver="simulated",
                    headless=True,
                ) as client:
                    cid = op.wait_for_client(timeout=15.0)
                    r_init = client.wait_for_status_ui(timeout=12.0)
                    self.assertEqual(r_init.status, 200)

                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=20.0)

                    # Client Status UI (HTML or its JSON API) must now reflect WAIT_LICENSE
                    def _ui_shows_wait_license() -> bool:
                        for path in ("/", "/api/status", "/status"):
                            r = http_request(client.status_url, "GET", path, timeout=2.0)
                            if r.status == 200 and "wait_license" in r.text().lower():
                                return True
                        return False

                    wait_until(
                        _ui_shows_wait_license,
                        timeout=10.0,
                        desc="Client Status UI reflecting WAIT_LICENSE stage",
                    )
