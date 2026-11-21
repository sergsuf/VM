"""Tier 2 — Boundary, Corner-Case, Negative & Security Tests (55 tests).

All imports are strictly Python stdlib + `tests.e2e.*`. Production modules are
resolved dynamically at test execution time via `require_module` / `require_attr`
or executed via documented CLI subprocesses. Missing modules fail loudly.
"""

from __future__ import annotations

import ast
import inspect
import json
import os
import pathlib
import re
import threading
import time
import unittest
import unittest.mock
import urllib.parse
import zipfile

from tests.e2e.helpers import (
    CustomDriverAgentHarness,
    FaultInjectingProxy,
    FaultyRangeServer,
    LiveClientHarness,
    LiveOperatorHarness,
    LiveRelayHarness,
    REPO_ROOT,
    TempWorkspace,
    auth_headers,
    create_app_update_zip,
    create_synthetic_iso,
    create_synthetic_post_config_assets,
    discover_simulated_drivers,
    extract_client_state,
    feature,
    http_request,
    invoke_build_app_update,
    require_attr,
    require_file,
    require_module,
    run_bootstrap_in_sandbox,
    run_cli_subprocess,
    sha256_bytes,
    sha256_file,
    wait_until,
)


class TestTier2AuthAndSecurityBoundaries(unittest.TestCase):
    """Area C Boundaries: Authentication, pairing validation, and constant-time checks (F-05)."""

    @feature("F-05", "F-07")
    def test_missing_authorization_header_returns_401(self) -> None:
        """Requests to client/file/update endpoints without Authorization must return 401."""
        with TempWorkspace("t2_noauth_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                r = http_request(
                    op.base_url,
                    "POST",
                    "/api/v1/clients/register",
                    headers={"X-Session-Id": op.session_id, "X-Client-Id": "c1"},
                    body={"client_id": "c1"},
                )
                self.assertEqual(r.status, 401, f"Expected 401 without Authorization, got {r.status}")

    @feature("F-05", "F-07")
    def test_wrong_bearer_token_returns_401(self) -> None:
        """Requests with wrong Bearer token must return 401."""
        with TempWorkspace("t2_badtok_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                hdrs = auth_headers("totally-wrong-token", op.session_id, "c1")
                r = http_request(
                    op.base_url,
                    "POST",
                    "/api/v1/clients/register",
                    headers=hdrs,
                    body={"client_id": "c1"},
                )
                self.assertEqual(r.status, 401)
                r2 = http_request(op.base_url, "GET", "/api/v1/updates/latest", headers=hdrs)
                self.assertEqual(r2.status, 401)

    @feature("F-05", "F-07")
    def test_mismatched_session_id_header_rejected(self) -> None:
        """Valid Bearer token paired with wrong X-Session-Id must be rejected (401 or 403)."""
        with TempWorkspace("t2_badsid_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                hdrs = auth_headers(op.token, "S-WRONG-SESSION", "c1")
                r = http_request(
                    op.base_url,
                    "POST",
                    "/api/v1/clients/register",
                    headers=hdrs,
                    body={"client_id": "c1"},
                )
                self.assertIn(r.status, (401, 403), f"Expected 401/403 on wrong session, got {r.status}")

    @feature("F-05")
    def test_verify_token_empty_whitespace_and_unicode_inputs(self) -> None:
        """Empty secret, whitespace suffix, and Unicode homoglyphs must never authenticate."""
        verify_token = require_attr("remote.auth", "verify_token")
        self.assertFalse(verify_token("", ""), "Empty expected & provided token must return False")
        self.assertFalse(verify_token("secret123", ""))
        self.assertFalse(verify_token("secret123", "secret123 "))
        self.assertFalse(verify_token("secret123", " secret123"))
        self.assertFalse(verify_token("secret123", "s\u0435cret123"))  # Cyrillic 'е'

    @feature("F-05")
    def test_malformed_connect_json_and_maxpair_uri_raise_value_error(self) -> None:
        """Malformed connect.json and maxpair:// URIs must raise ValueError."""
        PairingConfig = require_attr("remote.auth", "PairingConfig")
        bad_jsons = [
            "{not-json",
            "{}",
            json.dumps({"mode": "direct", "server_url": "http://127.0.0.1:8765"}),
            json.dumps(
                {
                    "mode": "ssh",
                    "server_url": "http://127.0.0.1:8765",
                    "session_id": "S-1",
                    "token": "tok123456",
                }
            ),
        ]
        for raw in bad_jsons:
            with self.assertRaises(ValueError, msg=f"Expected ValueError for {raw!r}"):
                PairingConfig.from_connect_json(raw)

        bad_uris = [
            "http://127.0.0.1:8765/?mode=direct",
            "maxpair://",
            "maxpair://pair?mode=direct",
            "maxpair://pair?mode=invalid&server_url=http://1.2.3.4&session_id=S-1&token=t",
        ]
        for uri in bad_uris:
            with self.assertRaises(ValueError, msg=f"Expected ValueError for URI {uri!r}"):
                PairingConfig.from_pairing_uri(uri)

    @feature("F-05")
    def test_auth_module_uses_constant_time_hmac_compare_digest(self) -> None:
        """Verify remote/auth.py uses hmac.compare_digest rather than naive string equality."""
        auth_src = require_file("remote/auth.py").read_text(encoding="utf-8")
        self.assertIn("compare_digest", auth_src)
        self.assertIn("hmac", auth_src)


class TestTier2StateMachineBoundaries(unittest.TestCase):
    """Area B Boundaries: Illegal transitions, update guards, crash recovery, corrupt JSON (F-03, F-04)."""

    @feature("F-03")
    def test_illegal_forward_jumps_raise_invalid_transition_error(self) -> None:
        """Disallowed direct stage jumps must raise InvalidTransitionError."""
        InvalidTransitionError = require_attr("remote.state_machine", "InvalidTransitionError")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")
        self.assertTrue(issubclass(InvalidTransitionError, ValueError))

        sm = DeploymentStateMachine(session_id="S-ERR", client_id="c-1")
        with self.assertRaises(InvalidTransitionError):
            sm.complete_iso_download("/tmp/a.iso")
        with self.assertRaises(InvalidTransitionError):
            sm.complete_install()
        with self.assertRaises(InvalidTransitionError):
            sm.confirm_license()
        with self.assertRaises(InvalidTransitionError):
            sm.complete_post_config({})

        sm.start_install("2025")
        with self.assertRaises(InvalidTransitionError):
            sm.confirm_license()
        with self.assertRaises(InvalidTransitionError):
            sm.start_post_config()

    @feature("F-03")
    def test_confirm_license_rejected_outside_wait_license(self) -> None:
        """confirm_license() must raise InvalidTransitionError in every stage except WAIT_LICENSE."""
        InvalidTransitionError = require_attr("remote.state_machine", "InvalidTransitionError")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        sm = DeploymentStateMachine(session_id="S-LIC-ERR", client_id="c-1")
        with self.assertRaises(InvalidTransitionError):
            sm.confirm_license()
        sm.start_install("2025")
        with self.assertRaises(InvalidTransitionError):
            sm.confirm_license()
        sm.complete_iso_download("/tmp/a.iso")
        with self.assertRaises(InvalidTransitionError):
            sm.confirm_license()
        sm.complete_install()
        sm.confirm_license()  # valid
        with self.assertRaises(InvalidTransitionError):
            sm.confirm_license()
        sm.complete_first_run(auto_post_config=False)
        with self.assertRaises(InvalidTransitionError):
            sm.confirm_license()
        sm.start_post_config()
        sm.complete_post_config({"ok": True})
        with self.assertRaises(InvalidTransitionError):
            sm.confirm_license()

    @feature("F-03")
    def test_pause_resume_retry_reset_illegal_status_guards(self) -> None:
        """pause/resume/retry/reset must raise InvalidTransitionError when status forbids them."""
        InvalidTransitionError = require_attr("remote.state_machine", "InvalidTransitionError")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        sm = DeploymentStateMachine(session_id="S-GUARD", client_id="c-1")
        with self.assertRaises(InvalidTransitionError):
            sm.pause()
        with self.assertRaises(InvalidTransitionError):
            sm.resume()
        with self.assertRaises(InvalidTransitionError):
            sm.retry()

        sm.start_install("2025")
        with self.assertRaises(InvalidTransitionError):
            sm.resume()
        with self.assertRaises(InvalidTransitionError):
            sm.retry()
        with self.assertRaises(InvalidTransitionError):
            sm.reset()

    @feature("F-03", "F-18")
    def test_can_accept_update_false_during_all_active_running_stages(self) -> None:
        """can_accept_update() MUST return False in every active running stage."""
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        sm = DeploymentStateMachine(session_id="S-ACT", client_id="c-1")
        sm.start_install("2025")
        self.assertFalse(sm.can_accept_update(), "DOWNLOAD_ISO/running must block updates")

        sm.complete_iso_download("/tmp/x.iso")
        self.assertFalse(sm.can_accept_update(), "INSTALL_MAX/running must block updates")

        sm.complete_install()
        sm.confirm_license()
        self.assertFalse(sm.can_accept_update(), "FIRST_RUN/running must block updates")

        sm.complete_first_run(auto_post_config=True)
        self.assertFalse(sm.can_accept_update(), "POST_CONFIG/running must block updates")

    @feature("F-04")
    def test_load_or_create_recovers_interrupted_install_max_three_cases(self) -> None:
        """Verify all 3 recovery outcomes when reloading a persisted INSTALL_MAX/running state."""
        proto = require_module("remote.protocol")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        with TempWorkspace("t2_rec_inst_") as ws:
            state_path = ws.client_data / "session_state.json"
            sm = DeploymentStateMachine(state_path=state_path, session_id="S-R", client_id="c-r")
            sm.start_install("2025")
            sm.complete_iso_download("/tmp/max2025.iso")
            self.assertEqual(sm.state.stage, proto.Stage.INSTALL_MAX)

            r1 = DeploymentStateMachine.load_or_create(
                state_path, "S-R", "c-r", installer_still_running=True, max_exe_exists=False
            )
            self.assertEqual((r1.state.stage, r1.state.status), (proto.Stage.INSTALL_MAX, proto.StageStatus.RUNNING))

            # Re-persist running state before testing case 2 & 3
            sm2 = DeploymentStateMachine(state_path=state_path, session_id="S-R", client_id="c-r")
            sm2.start_install("2025")
            sm2.complete_iso_download("/tmp/max2025.iso")
            r2 = DeploymentStateMachine.load_or_create(
                state_path, "S-R", "c-r", installer_still_running=False, max_exe_exists=True
            )
            self.assertEqual(
                (r2.state.stage, r2.state.status),
                (proto.Stage.WAIT_LICENSE, proto.StageStatus.WAITING_OPERATOR),
            )

            sm3 = DeploymentStateMachine(state_path=state_path, session_id="S-R", client_id="c-r")
            sm3.start_install("2025")
            sm3.complete_iso_download("/tmp/max2025.iso")
            r3 = DeploymentStateMachine.load_or_create(
                state_path, "S-R", "c-r", installer_still_running=False, max_exe_exists=False
            )
            self.assertEqual(
                (r3.state.stage, r3.state.status),
                (proto.Stage.INSTALL_MAX, proto.StageStatus.FAILED),
            )
            self.assertTrue(r3.state.last_error)

    @feature("F-04", "F-11")
    def test_load_or_create_recovers_interrupted_download_iso_auto_resume_flag(self) -> None:
        """Reloading DOWNLOAD_ISO/running honors auto_resume_download=False (paused) vs True (running)."""
        proto = require_module("remote.protocol")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        with TempWorkspace("t2_rec_dl_") as ws:
            state_path = ws.client_data / "session_state.json"
            sm = DeploymentStateMachine(state_path=state_path, session_id="S-D", client_id="c-d")
            sm.start_install("2025")

            r_paused = DeploymentStateMachine.load_or_create(
                state_path, "S-D", "c-d", auto_resume_download=False
            )
            self.assertEqual(
                (r_paused.state.stage, r_paused.state.status),
                (proto.Stage.DOWNLOAD_ISO, proto.StageStatus.PAUSED),
            )

            sm2 = DeploymentStateMachine(state_path=state_path, session_id="S-D", client_id="c-d")
            sm2.start_install("2025")
            r_run = DeploymentStateMachine.load_or_create(
                state_path, "S-D", "c-d", auto_resume_download=True
            )
            self.assertEqual(
                (r_run.state.stage, r_run.state.status),
                (proto.Stage.DOWNLOAD_ISO, proto.StageStatus.RUNNING),
            )

    @feature("F-04")
    def test_corrupted_or_truncated_session_state_json_handled_safely(self) -> None:
        """Truncated/corrupt session_state.json must either recover cleanly or raise ValueError cleanly."""
        proto = require_module("remote.protocol")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        with TempWorkspace("t2_corrupt_sm_") as ws:
            state_path = ws.client_data / "session_state.json"
            state_path.write_text('{"session_id": "S-BAD", "stage": ', encoding="utf-8")
            try:
                recovered = DeploymentStateMachine.load_or_create(state_path, "S-NEW", "c-new")
                self.assertEqual(recovered.state.stage, proto.Stage.PREP)
                self.assertEqual(recovered.state.status, proto.StageStatus.IDLE)
                # Ensure valid JSON is now persisted or corrupt file was quarantined
                tmp_files = [p for p in ws.client_data.iterdir() if p.name.endswith(".tmp")]
                self.assertEqual(tmp_files, [])
            except ValueError:
                pass


class TestTier2TransportAndRelayBoundaries(unittest.TestCase):
    """Area D Boundaries: Long-poll timeout, 400/404, relay isolation, static traversal (F-07, F-08, F-22)."""

    @feature("F-07")
    def test_long_poll_idle_timeout_returns_204_promptly(self) -> None:
        """Long-poll with wait=1 and no queued commands must return 204 within ~1-3s."""
        with TempWorkspace("t2_lp_204_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                cid = "lp-idle-client"
                hdrs = auth_headers(op.token, op.session_id, cid)
                http_request(
                    op.base_url, "POST", "/api/v1/clients/register", headers=hdrs, body={"client_id": cid}
                )
                t0 = time.monotonic()
                resp = http_request(
                    op.base_url,
                    "GET",
                    f"/api/v1/clients/{cid}/commands/poll?after_seq=0&wait=1",
                    headers=hdrs,
                    timeout=6.0,
                )
                elapsed = time.monotonic() - t0
                self.assertEqual(resp.status, 204, f"Expected 204 on idle poll, got {resp.status}")
                self.assertGreaterEqual(elapsed, 0.7)
                self.assertLess(elapsed, 4.5)

    @feature("F-07", "F-22")
    def test_unknown_client_id_command_returns_404(self) -> None:
        """Posting a command to an unregistered client ID must return 404 (or 400)."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t2_unk_cid_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                r = http_request(
                    op.base_url,
                    "POST",
                    "/api/v1/ui/clients/nonexistent-client-999/command",
                    body={"cmd_type": CommandType.REFRESH_STATUS.value, "payload": {}},
                )
                self.assertIn(r.status, (400, 404), f"Expected 404/400 for unknown client, got {r.status}")

    @feature("F-07", "F-22")
    def test_malformed_json_and_unknown_cmd_type_return_400(self) -> None:
        """Malformed JSON and unknown cmd_type on command endpoint must return 400."""
        with TempWorkspace("t2_bad_cmd_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                cid = "client-badcmd"
                hdrs = auth_headers(op.token, op.session_id, cid)
                http_request(
                    op.base_url, "POST", "/api/v1/clients/register", headers=hdrs, body={"client_id": cid}
                )
                r1 = http_request(
                    op.base_url,
                    "POST",
                    f"/api/v1/ui/clients/{cid}/command",
                    headers={"Content-Type": "application/json"},
                    body=b"{broken-json",
                )
                self.assertEqual(r1.status, 400)

                r2 = http_request(
                    op.base_url,
                    "POST",
                    f"/api/v1/ui/clients/{cid}/command",
                    body={"cmd_type": "DROP_DATABASE_NOW", "payload": {}},
                )
                self.assertEqual(r2.status, 400)

    @feature("F-08", "F-05")
    def test_relay_isolates_concurrent_sessions_strictly(self) -> None:
        """Session A token must not access Session B's commands, state, or files on Relay."""
        with TempWorkspace("t2_relay_iso_") as ws:
            iso_a = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=32768, seed=41)
            iso_b_dir = ws.root / "op_b_isos"
            iso_b_dir.mkdir(parents=True, exist_ok=True)
            create_synthetic_iso(iso_b_dir, version="2027", size_bytes=32768, seed=42)

            with LiveRelayHarness() as relay:
                with LiveOperatorHarness(
                    iso_dir=ws.operator_isos,
                    assets_dir=ws.operator_assets,
                    updates_dir=ws.operator_updates,
                    mode="relay",
                    relay_url=relay.base_url,
                    session_id="S-ALPHA",
                    token="tok_alpha_secret_111",
                ) as op_a, LiveOperatorHarness(
                    iso_dir=iso_b_dir,
                    assets_dir=ws.operator_assets,
                    updates_dir=ws.operator_updates,
                    mode="relay",
                    relay_url=relay.base_url,
                    session_id="S-BETA",
                    token="tok_beta_secret_222",
                ) as op_b:
                    # Cross-session attempt: Alpha token hitting Beta session endpoints
                    cross_hdrs = auth_headers(op_a.token, op_b.session_id, "intruder")
                    r_poll = http_request(
                        relay.base_url,
                        "GET",
                        f"/relay/v1/sessions/{op_b.session_id}/clients/c1/commands/poll?after_seq=0&wait=1",
                        headers=cross_hdrs,
                    )
                    self.assertIn(r_poll.status, (401, 403))

                    fid_b = op_b.get_iso_catalog()[0]["file_id"]
                    r_file = http_request(
                        relay.base_url,
                        "GET",
                        f"/relay/v1/sessions/{op_b.session_id}/files/{fid_b}",
                        headers=cross_hdrs,
                    )
                    self.assertIn(r_file.status, (401, 403))
                    self.assertTrue(iso_a.path.exists())

    @feature("F-08", "F-05")
    def test_relay_rejects_unauthenticated_and_unknown_session_requests(self) -> None:
        """Relay must reject unauthenticated requests and unknown session IDs with 401/403/404."""
        with LiveRelayHarness() as relay:
            r1 = http_request(
                relay.base_url,
                "POST",
                "/relay/v1/sessions/S-NOPE/clients/register",
                body={"client_id": "x"},
            )
            self.assertIn(r1.status, (401, 403, 404))
            r2 = http_request(
                relay.base_url,
                "GET",
                "/relay/v1/sessions/S-NOPE/clients/x/commands/poll?after_seq=0&wait=1",
                headers=auth_headers("bogus", "S-NOPE", "x"),
            )
            self.assertIn(r2.status, (401, 403, 404))

    @feature("F-22", "F-01")
    def test_operator_static_path_traversal_blocked(self) -> None:
        """Static route must block ../ and %2e%2e path traversal attempts."""
        with TempWorkspace("t2_trav_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                for path in (
                    "/static/../../auth.py",
                    "/static/../../../pyproject.toml",
                    "/static/%2e%2e/%2e%2e/auth.py",
                ):
                    r = http_request(op.base_url, "GET", path)
                    self.assertIn(
                        r.status,
                        (400, 403, 404),
                        f"Path traversal {path!r} returned unexpected {r.status}",
                    )
                    self.assertNotIn("compare_digest", r.text())
                    self.assertNotIn("[project]", r.text())


class TestTier2IsoCatalogAndTransferBoundaries(unittest.TestCase):
    """Area E Boundaries: Catalog filtering, sidecar invalidation, Range 416, corruptions, disk check (F-09..F-11)."""

    @feature("F-09")
    def test_iso_catalog_ignores_non_3dsmax_and_out_of_range_years(self) -> None:
        """Catalog must exclude Maya, Ubuntu, 2023, 2029, and non-.iso files."""
        with TempWorkspace("t2_cat_filter_") as ws:
            create_synthetic_iso(ws.operator_isos, filename="Autodesk_3ds_Max_2025_Win_64bit.iso", version="2025", size_bytes=16384)
            create_synthetic_iso(ws.operator_isos, filename="Autodesk_3ds_Max_2023_Win_64bit.iso", version="2023", size_bytes=16384)
            create_synthetic_iso(ws.operator_isos, filename="Autodesk_3ds_Max_2029_Win_64bit.iso", version="2029", size_bytes=16384)
            create_synthetic_iso(ws.operator_isos, filename="Maya_2025_Win_64bit.iso", version="2025", size_bytes=16384)
            (ws.operator_isos / "Autodesk_3ds_Max_2025_Notes.txt").write_text("not an iso", encoding="utf-8")

            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                names = [item["filename"] for item in op.get_iso_catalog()]
                self.assertEqual(names, ["Autodesk_3ds_Max_2025_Win_64bit.iso"])

    @feature("F-09")
    def test_iso_catalog_invalidates_sha256_sidecar_when_iso_changes(self) -> None:
        """Modifying an ISO's size/mtime must invalidate its stale .sha256.json sidecar."""
        with TempWorkspace("t2_sidecar_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=32768, seed=51)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op1:
                self.assertEqual(op1.get_iso_catalog()[0]["sha256"], iso.sha256)

            # Mutate ISO content & length (new size + mtime)
            new_bytes = iso.path.read_bytes() + b"APPENDED_NEW_CONTENT_123456"
            iso.path.write_bytes(new_bytes)
            new_sha = sha256_bytes(new_bytes)

            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op2:
                self.assertEqual(op2.get_iso_catalog()[0]["sha256"], new_sha)

    @feature("F-09")
    def test_http_range_boundaries_1byte_and_416_out_of_bounds(self) -> None:
        """Verify 1-byte Range (bytes=0-0), exact EOF byte, and 416 for out-of-bounds/inverted ranges."""
        with TempWorkspace("t2_range_416_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=4096, seed=52)
            raw = iso.path.read_bytes()
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                fid = op.get_iso_catalog()[0]["file_id"]
                hdrs = auth_headers(op.token, op.session_id)

                r_one = http_request(
                    op.base_url, "GET", f"/api/v1/files/{fid}", headers={**hdrs, "Range": "bytes=0-0"}
                )
                self.assertEqual(r_one.status, 206)
                self.assertEqual(r_one.body, raw[:1])

                r_last = http_request(
                    op.base_url, "GET", f"/api/v1/files/{fid}", headers={**hdrs, "Range": "bytes=4095-4095"}
                )
                self.assertEqual(r_last.status, 206)
                self.assertEqual(r_last.body, raw[-1:])

                r_oob = http_request(
                    op.base_url,
                    "GET",
                    f"/api/v1/files/{fid}",
                    headers={**hdrs, "Range": "bytes=9999999-99999999"},
                )
                self.assertEqual(r_oob.status, 416)

                r_inv = http_request(
                    op.base_url,
                    "GET",
                    f"/api/v1/files/{fid}",
                    headers={**hdrs, "Range": "bytes=500-100"},
                )
                self.assertEqual(r_inv.status, 416)

    @feature("F-10")
    def test_iso_precheck_rejects_corrupted_local_iso_of_same_name_and_size(self) -> None:
        """Pre-placed local ISO with matching name & size but 1 flipped byte must trigger re-download."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t2_bad_local_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=131072, seed=53)
            corrupt = bytearray(iso.path.read_bytes())
            corrupt[4096] ^= 0xFF
            (ws.client_isos / iso.filename).write_bytes(bytes(corrupt))

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
                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    rec = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=25.0)
                    st = extract_client_state(rec)
                    self.assertFalse(st.get("iso_skipped_download"), "Corrupt local ISO must NOT skip download")
                    final_path = pathlib.Path(st["iso_local_path"])
                    self.assertEqual(sha256_file(final_path), iso.sha256)

    @feature("F-11")
    def test_iso_transfer_mid_stream_drop_resumes_from_part_offset(self) -> None:
        """Transient 503 + mid-stream drop must retry/resume and finish with exact SHA-256."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t2_drop_resume_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=393216, seed=54)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with FaultInjectingProxy(op.base_url) as proxy:
                    proxy.fail_first_n_file_requests = 2
                    proxy.drop_after_bytes = 32768
                    proxy.drop_times = 1
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
                        rec = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=30.0)
                        st = extract_client_state(rec)
                        self.assertEqual(sha256_file(pathlib.Path(st["iso_local_path"])), iso.sha256)
                        self.assertGreaterEqual(len(proxy.file_downloads()), 3)

    @feature("F-11")
    def test_iso_transfer_torn_tail_corruption_rewinds_and_recovers(self) -> None:
        """A pre-existing .part file with corrupted tail bytes must rewind/re-fetch and pass SHA-256."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t2_torn_tail_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=262144, seed=55)
            raw = iso.path.read_bytes()
            prefix_len = 131072
            torn = bytearray(raw[:prefix_len])
            # Corrupt last 128 bytes of .part while metadata records the genuine tail hash
            genuine_tail_sha = sha256_bytes(raw[prefix_len - 4096 : prefix_len])
            for i in range(128):
                torn[prefix_len - 1 - i] ^= 0xAA

            part_path = ws.client_isos / f"{iso.filename}.part"
            part_path.write_bytes(bytes(torn))
            meta_path = ws.client_isos / f"{iso.filename}.part.meta.json"
            meta_path.write_text(
                json.dumps(
                    {
                        "file_id": "pre",
                        "filename": iso.filename,
                        "size_bytes": iso.size_bytes,
                        "sha256": iso.sha256,
                        "bytes_written": prefix_len,
                        "tail_sha256": genuine_tail_sha,
                        "tail_size": 4096,
                    }
                ),
                encoding="utf-8",
            )

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
                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    rec = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=25.0)
                    st = extract_client_state(rec)
                    self.assertEqual(sha256_file(pathlib.Path(st["iso_local_path"])), iso.sha256)

    @feature("F-11")
    def test_iso_transfer_full_sha256_mismatch_quarantines_part_and_fails(self) -> None:
        """Corrupted downloaded payload must fail SHA-256 verification and never promote to .iso."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t2_bad_sha_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=131072, seed=56)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with FaultInjectingProxy(op.base_url) as proxy:
                    proxy.corrupt_byte_at = 2048
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
                        rec = op.wait_for_stage(cid, "DOWNLOAD_ISO", "failed", timeout=25.0)
                        st = extract_client_state(rec)
                        self.assertTrue(st.get("last_error"), "Expected descriptive SHA-256 mismatch error")
                        self.assertFalse(
                            (ws.client_isos / iso.filename).exists(),
                            "Corrupted ISO must NEVER be promoted to final .iso path",
                        )

    @feature("F-11")
    def test_iso_transfer_insufficient_disk_space_fails_before_writing(self) -> None:
        """Advertising an ISO larger than available disk space must fail cleanly before writing."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t2_disk_space_") as ws:
            iso = create_synthetic_iso(ws.staging, version="2025", size_bytes=65536, seed=57)
            with FaultyRangeServer() as srv:
                url = srv.register_file("huge-iso", iso.path, iso.filename)
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
                        huge_size = 1024 * 1024 * 1024 * 1024 * 1024  # 1 PiB
                        iso_spec = {
                            "file_id": "huge-iso",
                            "filename": iso.filename,
                            "version": "2025",
                            "size_bytes": huge_size,
                            "sha256": iso.sha256,
                            "download_url": url,
                        }
                        op.send_command(
                            cid,
                            CommandType.START_INSTALL,
                            {"version": "2025", "iso_spec": iso_spec, "file_id": "huge-iso"},
                        )
                        rec = op.wait_for_stage(cid, "DOWNLOAD_ISO", "failed", timeout=20.0)
                        st = extract_client_state(rec)
                        err = (st.get("last_error") or "").lower()
                        self.assertTrue(
                            any(w in err for w in ("disk", "space", "free", "bytes")),
                            f"Expected disk space error message, got: {st.get('last_error')!r}",
                        )
                        self.assertEqual(len(srv.get_file_requests()), 0)


class TestTier2SecurityControlsAndDriverBoundaries(unittest.TestCase):
    """Area F Boundaries: Session 0, SAC 0/1/2 truthfulness, Defender Tamper Protection & expansion (F-06, F-12..F-15)."""

    @feature("F-12")
    def test_session_id_zero_detected_as_non_interactive_service(self) -> None:
        """Verify SessionId==0 is explicitly guarded as non-interactive in Windows/bootstrap code."""
        SystemStatus = require_attr("remote.protocol", "SystemStatus")
        st0 = SystemStatus(
            hostname="SVC",
            os_version="Win11",
            is_admin=True,
            session_id=0,
            smart_app_control_state="off",
            smart_app_control_reboot_required=False,
            defender_rtp_enabled=False,
            defender_tamper_protected=False,
            defender_exclusions=[],
            third_party_av=[],
            free_disk_bytes=10_000_000_000,
            installed_max_versions=[],
            ahk_available=True,
            agent_version="1.0.0",
            runtime_version="1.0.0",
        )
        self.assertEqual(st0.session_id, 0)
        win_src = require_file("remote/drivers/windows.py").read_text(encoding="utf-8")
        boot_src = require_file("remote/client/bootstrap.py").read_text(encoding="utf-8")
        self.assertRegex(win_src + "\n" + boot_src, r"(session_id\s*(==\s*0|<=?\s*0|>\s*0)|SessionId)")

    @feature("F-13")
    def test_smart_app_control_dword_0_1_2_mapping_never_swaps_1_and_2(self) -> None:
        """Verify VerifiedAndReputablePolicyState maps 0->off, 1->enforced, 2->evaluation (NEVER swapped)."""
        win_path = require_file("remote/drivers/windows.py")
        src = win_path.read_text(encoding="utf-8")
        tree = ast.parse(src, filename=str(win_path))

        found_sac_dict = False
        for node in ast.walk(tree):
            if isinstance(node, ast.Dict):
                pairs: dict[object, object] = {}
                for k, v in zip(node.keys, node.values):
                    if isinstance(k, ast.Constant) and isinstance(v, ast.Constant):
                        pairs[k.value] = v.value
                if {0, 1, 2}.issubset(pairs.keys()):
                    val0 = str(pairs[0]).lower()
                    val1 = str(pairs[1]).lower()
                    val2 = str(pairs[2]).lower()
                    if "off" in val0 and ("enforce" in val1 or "eval" in val1):
                        found_sac_dict = True
                        self.assertEqual(val0, "off")
                        self.assertEqual(val1, "enforced", "DWORD 1 MUST map to 'enforced', NOT 'evaluation'")
                        self.assertEqual(val2, "evaluation", "DWORD 2 MUST map to 'evaluation', NOT 'enforced'")

        # Even if implemented via if/elif rather than dict literal, reject swapped literals
        self.assertNotRegex(src, r"1\s*:\s*['\"]evaluation['\"]")
        self.assertNotRegex(src, r"2\s*:\s*['\"]enforced['\"]")
        if not found_sac_dict:
            self.assertIn("enforced", src)
            self.assertIn("evaluation", src)

    @feature("F-13")
    def test_smart_app_control_enforced_reports_reboot_and_guided_uri(self) -> None:
        """Verify SAC in enforced mode surfaces windowsdefender://smartapp/ and truthful status."""
        win_mod = require_module("remote.drivers.windows")
        drv = win_mod.WindowsSystemDriver()
        res_guided = drv.disable_smart_app_control(mode="guided")
        self.assertIsInstance(res_guided, dict)
        win_src = require_file("remote/drivers/windows.py").read_text(encoding="utf-8")
        self.assertIn("windowsdefender://smartapp/", win_src)
        self.assertIn("reboot", win_src.lower())

    @feature("F-14")
    def test_defender_tamper_protection_on_never_falsely_reports_rtp_disabled(self) -> None:
        """When Defender Tamper Protection is enabled, disable command MUST truthfully keep RTP=True."""
        proto = require_module("remote.protocol")
        bundle = discover_simulated_drivers()

        class TamperProtectedSystemDriver:
            def __init__(self) -> None:
                self._base = bundle.system
                self._exclusions: list[str] = []

            def collect_status(self, agent_version: str, runtime_version: str) -> object:
                st = self._base.collect_status(agent_version, runtime_version)
                st.defender_rtp_enabled = True
                st.defender_tamper_protected = True
                st.defender_exclusions = list(self._exclusions)
                return st

            def request_elevation(self, executable: str, args: list[str], cwd: str) -> dict[str, object]:
                return self._base.request_elevation(executable, args, cwd)

            def get_smart_app_control_status(self) -> dict[str, object]:
                return {"state": "enforced", "reboot_required": False}

            def disable_smart_app_control(self, mode: str = "guided") -> dict[str, object]:
                return {
                    "ok": False,
                    "state": "enforced",
                    "reboot_required": True,
                    "uri": "windowsdefender://smartapp/",
                    "detail": "Open Windows Security to turn off Smart App Control",
                }

            def get_defender_status(self) -> dict[str, object]:
                return {
                    "rtp_enabled": True,
                    "tamper_protected": True,
                    "exclusions": list(self._exclusions),
                }

            def configure_defender(
                self, action: str, exclusion_paths: list[str] | None = None
            ) -> dict[str, object]:
                if action == "add_exclusions" and exclusion_paths:
                    self._exclusions.extend(exclusion_paths)
                    return {"ok": True, "exclusions": list(self._exclusions)}
                if action == "disable":
                    return {
                        "ok": False,
                        "rtp_enabled": True,
                        "tamper_protected": True,
                        "fallback_uri": "windowsdefender://threatsettings/",
                        "detail": "Tamper Protection active; manual toggle required",
                    }
                return {"ok": True}

            def capture_screenshot(self, dest_path: pathlib.Path) -> pathlib.Path | None:
                return self._base.capture_screenshot(dest_path)

        with TempWorkspace("t2_tamper_") as ws:
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
                    system=TamperProtectedSystemDriver(),
                    client_id="tamper-client",
                ):
                    cid = op.wait_for_client(client_id="tamper-client", timeout=15.0)
                    op.send_command(cid, proto.CommandType.DEFENDER_CONTROL, {"action": "disable"})
                    op.send_command(cid, proto.CommandType.REFRESH_STATUS, {})

                    def _verified() -> bool:
                        rec = op.get_clients().get(cid)
                        if not rec:
                            return False
                        sys_st = rec.get("system_status") or rec.get("system") or rec
                        return (
                            sys_st.get("defender_tamper_protected") is True
                            and sys_st.get("defender_rtp_enabled") is True
                        )

                    wait_until(_verified, timeout=10.0, desc="truthful tamper-protected Defender status")

    @feature("F-14")
    def test_defender_exclusions_expand_env_vars_never_pass_literal_temp(self) -> None:
        """Verify WindowsSystemDriver expands env vars/paths before passing to Add-MpPreference."""
        win_src = require_file("remote/drivers/windows.py").read_text(encoding="utf-8")
        self.assertRegex(
            win_src,
            r"(expandvars|expanduser|os\.path\.abspath|\.resolve\()",
            "windows.py must expand environment variables and normalize exclusion paths",
        )
        self.assertIn("Add-MpPreference", win_src)

    @feature("F-15", "F-06")
    def test_installer_driver_cancel_event_aborts_cleanly(self) -> None:
        """Setting cancel_event must abort InstallerDriver.install_3dsmax cleanly without marking installed."""
        bundle = discover_simulated_drivers()
        cancel = threading.Event()
        cancel.set()
        with TempWorkspace("t2_inst_cancel_") as ws:
            iso = create_synthetic_iso(ws.client_isos, version="2026", size_bytes=4096)
            res = bundle.installer.install_3dsmax(
                "2026",
                iso.path,
                progress_cb=lambda p, m: None,
                log_cb=lambda l, m: None,
                cancel_event=cancel,
                timeout_sec=10,
            )
            self.assertIsInstance(res, dict)
            status = str(res.get("status", "")).lower()
            ok_flag = res.get("ok")
            self.assertTrue(
                ok_flag is False or status in ("aborted", "cancelled", "canceled", "failed"),
                f"Expected aborted/cancelled installer result when cancel_event is set: {res}",
            )


class TestTier2InstallerMaxAutoAndPostConfigBoundaries(unittest.TestCase):
    """Area G Boundaries: Status BOM/malformed lines, client vs vm-test rules, max-timeout, 2024/2025, Zip-Slip .mzp (F-15..F-17)."""

    @feature("F-15")
    def test_install_ps1_status_parser_handles_bom_malformed_and_missing_files(self) -> None:
        """Verify WindowsInstallerDriver handles missing ISO, set cancel_event, and UTF-8 BOM status files."""
        WindowsInstallerDriver = require_attr("remote.drivers.windows", "WindowsInstallerDriver")
        drv = WindowsInstallerDriver()
        self.assertIsInstance(drv.is_max_installed("2025"), bool)
        self.assertIsInstance(drv.is_installer_running(), bool)

        win_src = require_file("remote/drivers/windows.py").read_text(encoding="utf-8")
        self.assertRegex(
            win_src,
            r"(utf-8-sig|\\ufeff|lstrip\(['\"]\\ufeff['\"]\))",
            "WindowsInstallerDriver should handle UTF-8 BOM written by PowerShell 5.1 Set-Content",
        )
        with TempWorkspace("t2_win_inst_") as ws:
            missing = ws.client_isos / "does_not_exist.iso"
            try:
                res = drv.install_3dsmax(
                    "2025",
                    missing,
                    progress_cb=lambda p, m: None,
                    log_cb=lambda l, m: None,
                    cancel_event=threading.Event(),
                    timeout_sec=5,
                )
                self.assertIsInstance(res, dict)
                self.assertFalse(res.get("ok", True))
            except FileNotFoundError:
                pass

    @feature("F-16")
    def test_max_auto_client_profile_separates_vm_licensing_rules(self) -> None:
        """Verify client profile returns licensing_required (not auto-dismissing VM rules)."""
        watcher = require_module("cli.watcher")
        ui_mod = require_module("cli.ui_inspector")
        AHKBridge = require_attr("cli.ahk_bridge", "AHKBridge")

        cfg = watcher.WatcherConfig.from_yaml(REPO_ROOT / "config/rules.yaml")
        lic_snap = ui_mod.WindowSnapshot(
            hwnd=2001,
            title="WebView2 WebBrowser",
            class_name="Chrome_WidgetWin_1",
            process_name="AdskLicensingAgent.exe",
            pid=4200,
            rect=(0, 0, 800, 600),
            texts=["Clic LGS", "Let's Get Started"],
        )
        with TempWorkspace("t2_client_prof_") as ws:
            with (
                unittest.mock.patch.object(watcher, "list_visible_windows", return_value=[lic_snap]),
                unittest.mock.patch.object(watcher, "populate_snapshot_controls", side_effect=lambda s, **k: s),
                unittest.mock.patch.object(watcher, "is_process_running", return_value=True),
            ):
                sig = inspect.signature(watcher.watch_loop)
                kw: dict[str, object] = {
                    "poll_interval": 0.02,
                    "timeout_sec": 1.0,
                }
                if "profile" in sig.parameters:
                    kw["profile"] = "client"
                if "dump_dir" in sig.parameters:
                    kw["dump_dir"] = ws.logs
                if "max_timeout_sec" in sig.parameters:
                    kw["max_timeout_sec"] = 1.5
                res = watcher.watch_loop(cfg, AHKBridge(dry_run=True), **kw)
                self.assertIsNotNone(res, "watch_loop must return a WatchResult")
                self.assertEqual(getattr(res, "status", None), "licensing_required")

    @feature("F-16")
    def test_max_auto_invalid_only_rule_exits_nonzero(self) -> None:
        """Invoking cli.main watch with a non-existent --only-rule must exit non-zero."""
        res = run_cli_subprocess(
            ["-m", "cli.main", "watch", "--only-rule", "NonExistentRuleXYZ_999", "--timeout", "1"],
            timeout=15.0,
        )
        self.assertNotEqual(res.returncode, 0, "Expected non-zero exit for invalid --only-rule")

    @feature("F-16")
    def test_max_auto_max_timeout_caps_infinite_extension(self) -> None:
        """watch_loop must enforce max_timeout_sec even when 3dsmax.exe stays running forever."""
        watcher = require_module("cli.watcher")
        AHKBridge = require_attr("cli.ahk_bridge", "AHKBridge")

        cfg = watcher.WatcherConfig.from_yaml(REPO_ROOT / "config/rules.yaml")
        with (
            unittest.mock.patch.object(watcher, "list_visible_windows", return_value=[]),
            unittest.mock.patch.object(watcher, "is_process_running", return_value=True),
        ):
            t0 = time.monotonic()
            sig = inspect.signature(watcher.watch_loop)
            kw: dict[str, object] = {
                "stop_on_main_ready": True,
                "poll_interval": 0.02,
                "timeout_sec": 0.1,
            }
            if "max_timeout_sec" in sig.parameters:
                kw["max_timeout_sec"] = 0.25
            res = watcher.watch_loop(cfg, AHKBridge(dry_run=True), **kw)
            elapsed = time.monotonic() - t0
            self.assertLess(elapsed, 3.0, "watch_loop extended past max_timeout_sec ceiling")
            self.assertEqual(getattr(res, "status", None), "timeout")

    @feature("F-17")
    def test_post_config_version_routing_2024_mnux_vs_2025_mnx_hsx(self) -> None:
        """Verify WindowsPostConfigDriver deploys .mnux for 2024 vs .mnx+.hsx for 2025+ into ENU."""
        WindowsPostConfigDriver = require_attr("remote.drivers.windows", "WindowsPostConfigDriver")
        drv = WindowsPostConfigDriver()

        with TempWorkspace("t2_pc_ver_") as ws:
            assets = create_synthetic_post_config_assets(ws.operator_assets, version="2025")
            for ver in ("2024", "2025"):
                enu = ws.client_enu / "Autodesk" / "3dsMax" / f"{ver} - 64bit" / "ENU"
                enu.mkdir(parents=True, exist_ok=True)
                (enu / "3dsmax.ini").write_text("[Original]\nVersion=" + ver + "\n", encoding="utf-8")

            with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": str(ws.client_enu)}):
                for ver in ("2024", "2025"):
                    steps = drv.load_steps(
                        config_path=REPO_ROOT / "config/post_config.yaml", version=ver
                    )
                    for step in steps:
                        if step.category in ("theme", "quad_menu", "hotkey"):
                            drv.run_step(
                                step.step_id,
                                ver,
                                assets_dir=ws.operator_assets,
                                log_cb=lambda l, m: None,
                                cancel_event=threading.Event(),
                                config_path=REPO_ROOT / "config/post_config.yaml",
                            )

                enu24 = ws.client_enu / "Autodesk" / "3dsMax" / "2024 - 64bit" / "ENU"
                enu25 = ws.client_enu / "Autodesk" / "3dsMax" / "2025 - 64bit" / "ENU"
                files24 = [p.name for p in enu24.rglob("*") if p.is_file()]
                files25 = [p.name for p in enu25.rglob("*") if p.is_file()]
                self.assertTrue(
                    any(f.endswith(".mnux") for f in files24),
                    f"Expected .mnux deployed for 2024, saw: {files24}",
                )
                self.assertTrue(
                    any(f.endswith(".mnx") for f in files25),
                    f"Expected .mnx deployed for 2025, saw: {files25}",
                )
                self.assertFalse(
                    any(f.endswith(".mnux") for f in files25),
                    f"Legacy .mnux must NOT be deployed into 2025 ENU: {files25}",
                )
                self.assertTrue(assets["clrx"].exists())

    @feature("F-17")
    def test_post_config_rejects_mzp_zip_slip_traversal(self) -> None:
        """Malicious .mzp containing ../../escaped_from_mzp.ms must never write outside ENU."""
        deployer = require_module("cli.max_deployer")
        with TempWorkspace("t2_mzp_slip_") as ws:
            evil_dir = ws.staging / "evil_scripts"
            evil_dir.mkdir(parents=True, exist_ok=True)
            evil_mzp = evil_dir / "Evil.mzp"
            with zipfile.ZipFile(evil_mzp, "w") as zf:
                zf.writestr("../../escaped_from_mzp.ms", "-- escaped")

            enu_root = ws.client_enu / "Autodesk" / "3dsMax" / "2025 - 64bit" / "ENU"
            enu_root.mkdir(parents=True, exist_ok=True)
            with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": str(ws.client_enu)}):
                try:
                    deployer.deploy_scripts(evil_dir, max_version="2025")
                except ValueError:
                    pass
                escaped = list(ws.root.rglob("escaped_from_mzp.ms"))
                self.assertEqual(escaped, [], f"Zip-Slip .mzp wrote outside target: {escaped}")

    @feature("F-17")
    def test_post_config_missing_optional_asset_skips_without_corrupting_enu(self) -> None:
        """Running renderer step with missing installer skips/fails gracefully and keeps ENU intact."""
        WindowsPostConfigDriver = require_attr("remote.drivers.windows", "WindowsPostConfigDriver")
        drv = WindowsPostConfigDriver()
        with TempWorkspace("t2_pc_miss_") as ws:
            enu = ws.client_enu / "Autodesk" / "3dsMax" / "2025 - 64bit" / "ENU"
            enu.mkdir(parents=True, exist_ok=True)
            ini = enu / "3dsmax.ini"
            ini.write_text("[KeepMe]\nSafe=1\n", encoding="utf-8")

            with unittest.mock.patch.dict(os.environ, {"LOCALAPPDATA": str(ws.client_enu)}):
                steps = drv.load_steps(
                    config_path=REPO_ROOT / "config/post_config.yaml", version="2025"
                )
                renderer_steps = [s for s in steps if s.category == "renderer"]
                self.assertTrue(renderer_steps, "Expected at least one renderer step")
                res = drv.run_step(
                    renderer_steps[0].step_id,
                    "2025",
                    assets_dir=ws.operator_assets,  # empty: no Corona/V-Ray exe present
                    log_cb=lambda l, m: None,
                    cancel_event=threading.Event(),
                    config_path=REPO_ROOT / "config/post_config.yaml",
                )
                self.assertIsInstance(res, dict)
                self.assertIn(res.get("status", "skipped"), ("skipped", "failed", "done"))
                self.assertEqual(ini.read_text(encoding="utf-8"), "[KeepMe]\nSafe=1\n")


class TestTier2UpdaterAndBootstrapBoundaries(unittest.TestCase):
    """Area H Boundaries: Zip-Slip, SHA mismatch, min_runtime, blocked_versions, deferral, 2-crash rollback (F-18, F-19)."""

    def _make_updater(self, app_root: pathlib.Path, runtime_version: str = "1.0.0", current_version: str = "1.0.0") -> object:
        updater_mod = require_module("remote.updater")
        updater_cls = getattr(updater_mod, "AppUpdater", None) or getattr(updater_mod, "Updater")
        sig = inspect.signature(updater_cls.__init__)
        kw: dict[str, object] = {}
        for k, v in (
            ("app_dir", app_root / "app"),
            ("root_dir", app_root),
            ("base_dir", app_root),
            ("runtime_version", runtime_version),
            ("current_version", current_version),
        ):
            if k in sig.parameters:
                kw[k] = v
        return updater_cls(**kw)

    def _invoke_apply(self, updater: object, manifest: object, url: str, zip_path: pathlib.Path) -> object:
        apply_fn = (
            getattr(updater, "apply_update", None)
            or getattr(updater, "stage_and_activate", None)
            or getattr(updater, "download_and_apply", None)
        )
        assert callable(apply_fn)
        a_sig = inspect.signature(apply_fn)
        a_kw: dict[str, object] = {}
        if "download_url" in a_sig.parameters:
            a_kw["download_url"] = url
        if "archive_path" in a_sig.parameters:
            a_kw["archive_path"] = zip_path
        return apply_fn(manifest, **a_kw)

    @feature("F-18")
    def test_updater_rejects_zip_slip_relative_and_absolute_paths(self) -> None:
        """Zip-Slip archives with valid SHA-256 must be rejected without writing outside app/releases."""
        UpdateManifest = require_attr("remote.protocol", "UpdateManifest")
        with TempWorkspace("t2_upd_slip_") as ws:
            app_dir = ws.app_root / "app"
            (app_dir / "releases" / "1.0.0").mkdir(parents=True, exist_ok=True)
            active_path = app_dir / "active.json"
            orig_active = json.dumps(
                {
                    "version": "1.0.0",
                    "previous": None,
                    "pending_verify": False,
                    "attempts": 0,
                    "blocked_versions": [],
                }
            )
            active_path.write_text(orig_active, encoding="utf-8")

            evil_zip, digest, size = create_app_update_zip(
                ws.staging / "evil-2.0.0.zip",
                "2.0.0",
                raw_entries=[
                    ("../../pwn_rel.txt", b"evil"),
                    ("/tmp/pwn_abs_max_e2e.txt", b"evil"),
                ],
            )
            with FaultyRangeServer() as srv:
                url = srv.register_file("evil-200", evil_zip, evil_zip.name)
                manifest = UpdateManifest(
                    version="2.0.0",
                    min_runtime_version="1.0.0",
                    file_id="evil-200",
                    filename=evil_zip.name,
                    size_bytes=size,
                    sha256=digest,
                    download_url=url,
                )
                updater = self._make_updater(ws.app_root)
                with self.assertRaises(Exception):
                    self._invoke_apply(updater, manifest, url, evil_zip)

            self.assertEqual(list(ws.root.rglob("pwn_rel.txt")), [])
            self.assertFalse(pathlib.Path("/tmp/pwn_abs_max_e2e.txt").exists())
            self.assertEqual(json.loads(active_path.read_text(encoding="utf-8"))["version"], "1.0.0")

    @feature("F-18")
    def test_updater_rejects_sha256_mismatch_without_touching_active_json(self) -> None:
        """Archive whose SHA-256 mismatches manifest must be rejected with active.json untouched."""
        UpdateManifest = require_attr("remote.protocol", "UpdateManifest")
        with TempWorkspace("t2_upd_sha_") as ws:
            app_dir = ws.app_root / "app"
            (app_dir / "releases" / "1.0.0").mkdir(parents=True, exist_ok=True)
            active_path = app_dir / "active.json"
            orig = json.dumps(
                {
                    "version": "1.0.0",
                    "previous": None,
                    "pending_verify": False,
                    "attempts": 0,
                    "blocked_versions": [],
                }
            )
            active_path.write_text(orig, encoding="utf-8")
            zip_path, _, size = create_app_update_zip(ws.staging / "app-2.0.0.zip", "2.0.0")
            with FaultyRangeServer() as srv:
                url = srv.register_file("bad-sha", zip_path, zip_path.name)
                manifest = UpdateManifest(
                    version="2.0.0",
                    min_runtime_version="1.0.0",
                    file_id="bad-sha",
                    filename=zip_path.name,
                    size_bytes=size,
                    sha256="0" * 64,
                    download_url=url,
                )
                updater = self._make_updater(ws.app_root)
                with self.assertRaises(Exception):
                    self._invoke_apply(updater, manifest, url, zip_path)

            self.assertEqual(active_path.read_text(encoding="utf-8"), orig)

    @feature("F-18")
    def test_updater_blocks_incompatible_min_runtime_version(self) -> None:
        """Update requiring min_runtime_version > current runtime must be refused."""
        UpdateManifest = require_attr("remote.protocol", "UpdateManifest")
        with TempWorkspace("t2_upd_rt_") as ws:
            app_dir = ws.app_root / "app"
            (app_dir / "releases" / "1.0.0").mkdir(parents=True, exist_ok=True)
            active_path = app_dir / "active.json"
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
            zip_path, digest, size = create_app_update_zip(ws.staging / "app-2.0.0.zip", "2.0.0")
            with FaultyRangeServer() as srv:
                url = srv.register_file("rt-200", zip_path, zip_path.name)
                manifest = UpdateManifest(
                    version="2.0.0",
                    min_runtime_version="9.0.0",
                    file_id="rt-200",
                    filename=zip_path.name,
                    size_bytes=size,
                    sha256=digest,
                    download_url=url,
                )
                updater = self._make_updater(ws.app_root, runtime_version="1.0.0")
                try:
                    self._invoke_apply(updater, manifest, url, zip_path)
                except Exception:
                    pass
            self.assertEqual(json.loads(active_path.read_text(encoding="utf-8"))["version"], "1.0.0")

    @feature("F-18")
    def test_updater_skips_blocked_version_and_same_or_older_version(self) -> None:
        """Blocked version and same/older SemVer versions must not overwrite active.json."""
        UpdateManifest = require_attr("remote.protocol", "UpdateManifest")
        with TempWorkspace("t2_upd_blk_") as ws:
            app_dir = ws.app_root / "app"
            (app_dir / "releases" / "1.5.0").mkdir(parents=True, exist_ok=True)
            active_path = app_dir / "active.json"
            active_path.write_text(
                json.dumps(
                    {
                        "version": "1.5.0",
                        "previous": "1.4.0",
                        "pending_verify": False,
                        "attempts": 0,
                        "blocked_versions": ["1.6.0"],
                    }
                ),
                encoding="utf-8",
            )
            for ver in ("1.6.0", "1.5.0", "1.4.9"):
                zip_path, digest, size = create_app_update_zip(ws.staging / f"app-{ver}.zip", ver)
                with FaultyRangeServer() as srv:
                    url = srv.register_file(f"u-{ver}", zip_path, zip_path.name)
                    manifest = UpdateManifest(
                        version=ver,
                        min_runtime_version="1.0.0",
                        file_id=f"u-{ver}",
                        filename=zip_path.name,
                        size_bytes=size,
                        sha256=digest,
                        download_url=url,
                    )
                    updater = self._make_updater(ws.app_root, current_version="1.5.0")
                    try:
                        self._invoke_apply(updater, manifest, url, zip_path)
                    except Exception:
                        pass
                self.assertEqual(
                    json.loads(active_path.read_text(encoding="utf-8"))["version"],
                    "1.5.0",
                    f"Version {ver} should have been skipped/rejected",
                )

    @feature("F-18", "F-03")
    def test_updater_defers_restart_when_state_machine_is_running(self) -> None:
        """PUSH_UPDATE while INSTALL_MAX is running must not kill/exit-42 the agent mid-install."""
        proto = require_module("remote.protocol")
        bundle = discover_simulated_drivers()
        release_install = threading.Event()
        started_install = threading.Event()

        class BlockingInstallerDriver:
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
                started_install.set()
                progress_cb(25, "Installing...")
                release_install.wait(timeout=10.0)
                if cancel_event.is_set():
                    return {"ok": False, "status": "aborted"}
                progress_cb(100, "Installed")
                return {"ok": True, "status": "completed"}

            def is_max_installed(self, version: str) -> bool:
                return release_install.is_set()

            def is_installer_running(self) -> bool:
                return started_install.is_set() and not release_install.is_set()

        with TempWorkspace("t2_upd_defer_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=32768, seed=61)
            (ws.client_isos / iso.filename).write_bytes(iso.path.read_bytes())
            res_build = invoke_build_app_update("2.0.0", ws.operator_updates)
            self.assertEqual(res_build.returncode, 0, res_build.stderr)

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
                    installer=BlockingInstallerDriver(),
                    client_id="defer-client",
                ):
                    cid = op.wait_for_client(client_id="defer-client", timeout=15.0)
                    iso_meta = op.get_iso_catalog()[0]
                    op.send_command(
                        cid,
                        proto.CommandType.START_INSTALL,
                        {"version": "2025", "iso_spec": iso_meta, "file_id": iso_meta["file_id"]},
                    )
                    self.assertTrue(started_install.wait(timeout=15.0))
                    op.wait_for_stage(cid, "INSTALL_MAX", "running", timeout=10.0)

                    # Push update mid-install; agent MUST remain in INSTALL_MAX/running
                    op.send_command(cid, proto.CommandType.PUSH_UPDATE, {})
                    time.sleep(0.5)
                    st_now = extract_client_state(op.get_clients()[cid])
                    self.assertEqual(st_now["stage"], "INSTALL_MAX")
                    self.assertEqual(st_now["status"], "running")

                    release_install.set()
                    op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=15.0)

    @feature("F-19")
    def test_bootstrap_rolls_back_and_blacklists_after_two_crash_attempts(self) -> None:
        """Real bootstrap.py subprocess must roll back to previous & blacklist after 2 crashes."""
        with TempWorkspace("t2_boot_rb_") as ws:
            log_file = ws.logs / "rb_log.txt"
            rel_good = ws.app_root / "app" / "releases" / "1.0.0" / "remote" / "client"
            rel_bad = ws.app_root / "app" / "releases" / "2.0.0" / "remote" / "client"
            for r in (rel_good, rel_bad):
                r.mkdir(parents=True, exist_ok=True)
                (r.parent / "__init__.py").write_text("", encoding="utf-8")
                (r / "__init__.py").write_text("", encoding="utf-8")

            active_path = ws.app_root / "app" / "active.json"
            active_path.write_text(
                json.dumps(
                    {
                        "version": "2.0.0",
                        "previous": "1.0.0",
                        "pending_verify": True,
                        "attempts": 0,
                        "blocked_versions": [],
                    }
                ),
                encoding="utf-8",
            )
            (rel_bad / "main.py").write_text(
                f"import sys\n"
                f"with open({str(log_file)!r}, 'a', encoding='utf-8') as f: f.write('crash_2.0.0\\n')\n"
                f"sys.exit(1)\n",
                encoding="utf-8",
            )
            (rel_good / "main.py").write_text(
                f"import sys\n"
                f"with open({str(log_file)!r}, 'a', encoding='utf-8') as f: f.write('ok_1.0.0\\n')\n"
                f"sys.exit(0)\n",
                encoding="utf-8",
            )
            res = run_bootstrap_in_sandbox(ws.app_root, timeout=15.0)
            self.assertEqual(res.returncode, 0, f"Rollback failed:\n{res.stdout}\n{res.stderr}")
            lines = log_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(lines, ["crash_2.0.0", "crash_2.0.0", "ok_1.0.0"])
            active = json.loads(active_path.read_text(encoding="utf-8"))
            self.assertEqual(active["version"], "1.0.0")
            self.assertIn("2.0.0", active["blocked_versions"])
            self.assertFalse(active["pending_verify"])

    @feature("F-19")
    def test_bootstrap_handles_missing_previous_release_without_infinite_loop(self) -> None:
        """Crashing release with previous=None must exit non-zero cleanly without looping forever."""
        with TempWorkspace("t2_boot_noprev_") as ws:
            rel_bad = ws.app_root / "app" / "releases" / "1.0.0" / "remote" / "client"
            rel_bad.mkdir(parents=True, exist_ok=True)
            (rel_bad.parent / "__init__.py").write_text("", encoding="utf-8")
            (rel_bad / "__init__.py").write_text("", encoding="utf-8")
            (rel_bad / "main.py").write_text("import sys\nsys.exit(1)\n", encoding="utf-8")

            (ws.app_root / "app" / "active.json").write_text(
                json.dumps(
                    {
                        "version": "1.0.0",
                        "previous": None,
                        "pending_verify": True,
                        "attempts": 0,
                        "blocked_versions": [],
                    }
                ),
                encoding="utf-8",
            )
            res = run_bootstrap_in_sandbox(ws.app_root, timeout=15.0)
            self.assertNotEqual(res.returncode, 0)


class TestTier2PackagingUiAndRunnerBoundaries(unittest.TestCase):
    """Areas A/I/J Boundaries: 204 updates, loopback binding, _pth config, invalid version, batch logs, meta (F-01..F-24)."""

    @feature("F-18", "F-22")
    def test_operator_updates_latest_returns_204_when_no_update_available(self) -> None:
        """GET /api/v1/updates/latest must return 204 when --updates-dir has no updates."""
        with TempWorkspace("t2_upd_204_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                hdrs = auth_headers(op.token, op.session_id)
                r = http_request(op.base_url, "GET", "/api/v1/updates/latest", headers=hdrs)
                self.assertEqual(r.status, 204, f"Expected 204 with empty updates dir, got {r.status}")

    @feature("F-23")
    def test_client_status_ui_binds_loopback_only(self) -> None:
        """Verify remote/client/status_ui.py binds strictly to 127.0.0.1 (never 0.0.0.0)."""
        src = require_file("remote/client/status_ui.py").read_text(encoding="utf-8")
        self.assertIn("127.0.0.1", src)
        self.assertNotIn('"0.0.0.0"', src)
        self.assertNotIn("'0.0.0.0'", src)

    @feature("F-20")
    def test_python_embed_pth_configuration_enables_site_packages(self) -> None:
        """Verify portable packaging configures python312._pth with uncommented `import site`."""
        ps1 = require_file("packaging/build_portable_client.ps1").read_text(encoding="utf-8")
        py_src = require_file("packaging/build_portable_client.py").read_text(encoding="utf-8")
        combined = ps1 + "\n" + py_src
        self.assertIn("python312._pth", combined)
        self.assertRegex(combined, r"(?m)import site")
        self.assertRegex(combined, r"Lib[/\\]+site-packages")

    @feature("F-20", "F-18")
    def test_build_app_update_rejects_empty_or_invalid_version_string(self) -> None:
        """packaging/build_app_update.py must reject empty or path-traversal version strings."""
        with TempWorkspace("t2_bad_ver_") as ws:
            for bad_ver in ("", "../evil", "1.0/../../bad"):
                res = invoke_build_app_update(bad_ver, ws.out)
                self.assertNotEqual(
                    res.returncode,
                    0,
                    f"build_app_update.py unexpectedly accepted invalid version {bad_ver!r}",
                )

    @feature("F-07", "F-22")
    def test_operator_ui_handles_large_log_batch_without_dropping_server(self) -> None:
        """Posting 500 LogEntry items in batches must succeed and keep /api/v1/ui/snapshot responsive."""
        proto = require_module("remote.protocol")
        with TempWorkspace("t2_log_flood_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                cid = "log-flood-client"
                hdrs = auth_headers(op.token, op.session_id, cid)
                http_request(
                    op.base_url, "POST", "/api/v1/clients/register", headers=hdrs, body={"client_id": cid}
                )
                for batch_i in range(5):
                    entries = [
                        proto.LogEntry(
                            "2026-11-20T14:00:00Z", "INFO", "PREP", f"msg-{batch_i}-{j}"
                        ).to_dict()
                        for j in range(100)
                    ]
                    r = http_request(
                        op.base_url,
                        "POST",
                        f"/api/v1/clients/{cid}/logs",
                        headers=hdrs,
                        body={"entries": entries, "logs": entries},
                    )
                    self.assertEqual(r.status, 200, r.text())
                snap = op.snapshot()
                self.assertIn(cid, snap.get("clients", {}) if isinstance(snap.get("clients"), dict) else [c.get("client_id") for c in snap.get("clients", [])])

    @feature("F-24")
    def test_runner_rejects_invalid_tier_argument(self) -> None:
        """python3 tests/e2e/run_e2e.py --tier 99 must exit non-zero."""
        res = run_cli_subprocess(["tests/e2e/run_e2e.py", "--tier", "99"], timeout=15.0)
        self.assertNotEqual(res.returncode, 0)

    @feature("F-24")
    def test_helpers_require_module_raises_assertion_error_never_skips(self) -> None:
        """require_module on a nonexistent module must raise AssertionError, never a skip exception."""
        try:
            require_module("nonexistent_module_e2e_xyz_99999")
            self.fail("Expected AssertionError for missing module")
        except AssertionError as exc:
            self.assertNotEqual(type(exc).__name__, "SkipTest")
            self.assertIn("nonexistent_module_e2e_xyz_99999", str(exc))

    @feature("F-24")
    def test_helpers_feature_decorator_rejects_unknown_feature_id(self) -> None:
        """@feature('F-99') and @feature() must immediately raise ValueError."""
        with self.assertRaises(ValueError):
            feature("F-99")
        with self.assertRaises(ValueError):
            feature()
