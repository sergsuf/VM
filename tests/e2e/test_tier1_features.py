"""Tier 1 — Isolated Happy-Path Feature Tests (60 tests across F-01..F-24).

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
import sys
import threading
import time
import unittest
import urllib.parse
import zipfile

from tests.e2e import ALL_FEATURE_IDS
from tests.e2e.helpers import (
    FaultInjectingProxy,
    FaultyRangeServer,
    LiveClientHarness,
    LiveOperatorHarness,
    LiveRelayHarness,
    REPO_ROOT,
    TempWorkspace,
    audit_python_file_stdlib_and_no_skips,
    auth_headers,
    create_app_update_zip,
    create_synthetic_iso,
    create_synthetic_post_config_assets,
    discover_simulated_drivers,
    extract_client_state,
    extract_client_system_status,
    feature,
    find_free_port,
    http_request,
    invoke_build_app_update,
    invoke_build_portable_client,
    parse_pyproject_toml,
    require_attr,
    require_file,
    require_module,
    run_bootstrap_in_sandbox,
    run_cli_subprocess,
    sha256_bytes,
    sha256_file,
    stdlib_check_yaml_well_formed,
    wait_until,
)


class TestTier1PackagingAndStack(unittest.TestCase):
    """Area A: Pure-Python stack, cross-platform metadata, packaging & docs (F-01, F-02, F-20)."""

    @feature("F-01")
    def test_pure_python_remote_has_zero_tauri_or_node_artifacts(self) -> None:
        """Verify pure-Python remote/ package with zero Tauri/Node artifacts or forbidden deps."""
        remote_dir = require_file("remote")
        self.assertTrue(remote_dir.is_dir())
        for forbidden in ("package.json", "Cargo.toml", "src-tauri", "tauri.conf.json", "node_modules"):
            self.assertFalse(
                (REPO_ROOT / forbidden).exists(),
                f"Forbidden Tauri/Node artifact found in pure-Python repo: {forbidden}",
            )
        py_files = sorted(remote_dir.rglob("*.py"))
        self.assertGreaterEqual(len(py_files), 8, "Expected core modules under remote/")

        forbidden_pkgs = {
            "fastapi",
            "uvicorn",
            "starlette",
            "aiohttp",
            "websockets",
            "requests",
            "httpx",
            "tkinter",
            "uiautomation",
            "psutil",
        }
        for py_file in py_files:
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in tree.body:
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".", 1)[0]
                        self.assertNotIn(
                            top,
                            forbidden_pkgs,
                            f"{py_file.relative_to(REPO_ROOT)} has forbidden top-level import {top!r}",
                        )
                elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                    top = node.module.split(".", 1)[0]
                    self.assertNotIn(
                        top,
                        forbidden_pkgs,
                        f"{py_file.relative_to(REPO_ROOT)} has forbidden top-level import {top!r}",
                    )

    @feature("F-02")
    def test_pyproject_cross_platform_metadata_and_entrypoints(self) -> None:
        """Verify pyproject.toml win32 marker on uiautomation, cli+remote packages, and 4 scripts."""
        pyproject_path = require_file("pyproject.toml")
        data = parse_pyproject_toml(pyproject_path)
        project = data.get("project", {})
        deps = project.get("dependencies", [])
        uia_deps = [d for d in deps if "uiautomation" in d.lower()]
        self.assertTrue(uia_deps, "Expected uiautomation dependency in pyproject.toml")
        for dep in uia_deps:
            self.assertRegex(
                dep,
                r"(sys_platform\s*==\s*['\"]win32['\"]|platform_system\s*==\s*['\"]Windows['\"])",
                f"uiautomation dependency must carry a Windows platform marker: {dep!r}",
            )

        scripts = project.get("scripts", {})
        for cmd in ("max-auto", "max-operator", "max-client", "max-relay"):
            self.assertIn(cmd, scripts, f"Missing console entrypoint {cmd!r} in pyproject.toml")

        raw = pyproject_path.read_text(encoding="utf-8")
        self.assertIn("cli", raw)
        self.assertIn("remote", raw)

    @feature("F-20", "F-18")
    def test_build_app_update_creates_verified_archive_and_manifest(self) -> None:
        """Run packaging/build_app_update.py and verify output zip, manifest, and SHA-256."""
        UpdateManifest = require_attr("remote.protocol", "UpdateManifest")
        with TempWorkspace("t1_app_upd_") as ws:
            res = invoke_build_app_update("1.2.3", ws.out, notes="Tier 1 test build")
            self.assertEqual(
                res.returncode,
                0,
                f"build_app_update.py failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
            )
            zips = list(ws.out.glob("*.zip"))
            self.assertEqual(len(zips), 1, f"Expected 1 update zip in {ws.out}, got {zips}")
            manifests = [p for p in ws.out.glob("*.json")]
            self.assertTrue(manifests, f"Expected manifest JSON in {ws.out}")
            manifest_path = next(
                (p for p in manifests if "manifest" in p.name.lower()), manifests[0]
            )
            manifest = UpdateManifest.from_dict(
                json.loads(manifest_path.read_text(encoding="utf-8"))
            )
            self.assertEqual(manifest.version, "1.2.3")
            zip_path = zips[0]
            self.assertEqual(manifest.size_bytes, zip_path.stat().st_size)
            self.assertEqual(manifest.sha256, sha256_file(zip_path))
            with zipfile.ZipFile(zip_path, "r") as zf:
                names = zf.namelist()
                self.assertTrue(any(n.startswith("remote/") for n in names), "Missing remote/ in zip")
                self.assertTrue(any(n.startswith("cli/") for n in names), "Missing cli/ in zip")
                self.assertTrue(any(n.startswith("config/") for n in names), "Missing config/ in zip")
                for n in names:
                    self.assertFalse(n.startswith("/") or ".." in pathlib.PurePosixPath(n).parts)
                    self.assertFalse(n.startswith(".git/"))
                    self.assertFalse(n.startswith(".agents/"))

    @feature("F-20", "F-19")
    def test_portable_client_packaging_artifacts_present_and_valid(self) -> None:
        """Verify Start-MaxAgent.bat and portable client builders declare required layout."""
        bat_path = require_file("packaging/Start-MaxAgent.bat")
        ps1_path = require_file("packaging/build_portable_client.ps1")
        py_path = require_file("packaging/build_portable_client.py")
        require_file("packaging/__init__.py")

        bat_text = bat_path.read_text(encoding="utf-8")
        self.assertIn("python.exe", bat_text.lower())
        self.assertIn("bootstrap.py", bat_text.lower())
        self.assertIn("%~dp0", bat_text)
        self.assertIn("%*", bat_text)
        for bad in (".vbs", "wscript", "cscript"):
            self.assertNotIn(bad, bat_text.lower(), f"Start-MaxAgent.bat must not invoke {bad}")

        combined_builder = (
            ps1_path.read_text(encoding="utf-8") + "\n" + py_path.read_text(encoding="utf-8")
        )
        self.assertIn("._pth", combined_builder)
        self.assertIn("import site", combined_builder)
        self.assertIn("AutoHotkey64.exe", combined_builder)
        self.assertIn("Start-MaxAgent.bat", combined_builder)

    @feature("F-20")
    def test_ci_workflow_valid_and_runs_cross_platform_tests(self) -> None:
        """Verify .github/workflows/ci.yml exists, is well-formed, and keeps vm-orchestrator.yml."""
        ci_path = require_file(".github/workflows/ci.yml")
        vm_path = require_file(".github/workflows/vm-orchestrator.yml")
        ci_text = stdlib_check_yaml_well_formed(ci_path)
        vm_text = stdlib_check_yaml_well_formed(vm_path)
        self.assertIn("jobs:", ci_text)
        self.assertRegex(ci_text, r"(?m)^\s*on\s*:")
        self.assertRegex(ci_text, r"(unittest|pytest|run_e2e)")
        self.assertIn("jobs:", vm_text)

    @feature("F-20")
    def test_deployment_docs_and_readme_document_remote_workflow(self) -> None:
        """Verify docs/REMOTE_DEPLOY.md and README.md cover core remote deployment concepts."""
        doc_path = require_file("docs/REMOTE_DEPLOY.md")
        readme_path = require_file("README.md")
        doc_text = doc_path.read_text(encoding="utf-8")
        readme_text = readme_path.read_text(encoding="utf-8")
        self.assertGreater(len(doc_text), 800, "docs/REMOTE_DEPLOY.md should provide thorough guide")
        combined = (doc_text + "\n" + readme_text).lower()
        for topic in ("direct", "relay", "smart app control", "defender", "wait_license", "post_config"):
            self.assertIn(topic, combined, f"Documentation missing coverage of {topic!r}")


class TestTier1StateMachineAndPersistence(unittest.TestCase):
    """Area B: Protocol models, 7-stage state machine, and atomic persistence (F-03, F-04)."""

    @feature("F-03")
    def test_protocol_enums_match_canonical_contract(self) -> None:
        """Verify Stage (7), StageStatus (7), and CommandType (16) enums match PROJECT.md."""
        Stage = require_attr("remote.protocol", "Stage")
        StageStatus = require_attr("remote.protocol", "StageStatus")
        CommandType = require_attr("remote.protocol", "CommandType")

        expected_stages = [
            "PREP",
            "DOWNLOAD_ISO",
            "INSTALL_MAX",
            "WAIT_LICENSE",
            "FIRST_RUN",
            "POST_CONFIG",
            "DONE",
        ]
        self.assertEqual([s.value for s in Stage], expected_stages)
        self.assertTrue(issubclass(Stage, str))

        expected_statuses = [
            "idle",
            "running",
            "waiting_operator",
            "paused",
            "failed",
            "aborted",
            "completed",
        ]
        self.assertEqual([s.value for s in StageStatus], expected_statuses)
        self.assertTrue(issubclass(StageStatus, str))

        expected_cmds = {
            "REFRESH_STATUS",
            "REQUEST_ELEVATION",
            "SMART_APP_CONTROL",
            "DEFENDER_CONTROL",
            "START_INSTALL",
            "CONFIRM_LICENSE",
            "TRIGGER_FIRST_RUN",
            "TRIGGER_POST_CONFIG",
            "SKIP_STAGE",
            "PAUSE",
            "RESUME",
            "RETRY",
            "ABORT",
            "RESET",
            "PUSH_UPDATE",
            "CAPTURE_SCREENSHOT",
        }
        self.assertEqual({c.name for c in CommandType}, expected_cmds)
        self.assertTrue(issubclass(CommandType, str))

    @feature("F-03", "F-04")
    def test_protocol_dataclasses_json_roundtrip(self) -> None:
        """Verify all 9 contracted dataclasses serialize to pure JSON and round-trip identically."""
        proto = require_module("remote.protocol")
        iso = proto.IsoSpec(
            file_id="iso-2025",
            filename="Autodesk_3ds_Max_2025_Win_64bit.iso",
            version="2025",
            size_bytes=1048576,
            sha256="a" * 64,
            download_url="http://127.0.0.1:9999/files/iso-2025",
        )
        transfer = proto.TransferProgress(
            phase="downloading",
            bytes_done=524288,
            bytes_total=1048576,
            percent=50.0,
            speed_bps=131072.0,
            eta_seconds=4.0,
            local_path="/tmp/max.iso.part",
        )
        step = proto.PostConfigStepState(
            step_id="corona",
            name="Install Corona",
            category="renderer",
            status="done",
            detail="Installed silently",
        )
        objs = [
            proto.SystemStatus(
                hostname="CLIENT-PC",
                os_version="Windows 11 24H2",
                is_admin=True,
                session_id=1,
                smart_app_control_state="off",
                smart_app_control_reboot_required=False,
                defender_rtp_enabled=False,
                defender_tamper_protected=False,
                defender_exclusions=["C:\\Install"],
                third_party_av=[],
                free_disk_bytes=100_000_000_000,
                installed_max_versions=["2025"],
                ahk_available=True,
                agent_version="1.0.0",
                runtime_version="1.0.0",
            ),
            iso,
            transfer,
            step,
            proto.SessionState(
                session_id="S-1001",
                client_id="client-01",
                stage=proto.Stage.DOWNLOAD_ISO,
                status=proto.StageStatus.RUNNING,
                target_version="2025",
                iso_spec=iso,
                iso_local_path=None,
                iso_skipped_download=False,
                transfer=transfer,
                install_percent=0,
                install_message="",
                post_config_steps=[step],
                summary={"ready": False},
                last_error=None,
                retry_count=0,
                updated_at="2026-11-20T14:00:00Z",
            ),
            proto.Command(
                cmd_id="cmd-1",
                seq=1,
                cmd_type=proto.CommandType.START_INSTALL,
                payload={"version": "2025"},
                created_at="2026-11-20T14:00:00Z",
            ),
            proto.CommandAck(
                cmd_id="cmd-1",
                status="completed",
                detail="Started",
                result={"ok": True},
            ),
            proto.LogEntry(
                ts="2026-11-20T14:00:00Z",
                level="INFO",
                stage="PREP",
                msg="Agent booted",
            ),
            proto.UpdateManifest(
                version="1.1.0",
                min_runtime_version="1.0.0",
                file_id="upd-110",
                filename="app-1.1.0.zip",
                size_bytes=4096,
                sha256="b" * 64,
                notes="Bug fixes",
                download_url=None,
            ),
        ]
        for obj in objs:
            raw_json = json.dumps(obj.to_dict())
            restored = type(obj).from_dict(json.loads(raw_json))
            self.assertEqual(restored, obj, f"Round-trip mismatch for {type(obj).__name__}")

    @feature("F-03")
    def test_state_machine_full_nominal_happy_path_prep_to_done(self) -> None:
        """Drive DeploymentStateMachine through all 7 stages from PREP to DONE."""
        proto = require_module("remote.protocol")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        sm = DeploymentStateMachine(session_id="S-HAPPY", client_id="c-1")
        self.assertEqual(sm.state.stage, proto.Stage.PREP)
        self.assertEqual(sm.state.status, proto.StageStatus.IDLE)

        iso = proto.IsoSpec("f1", "Autodesk_3ds_Max_2025.iso", "2025", 1024, "c" * 64)
        st = sm.start_install("2025", iso)
        self.assertEqual((st.stage, st.status), (proto.Stage.DOWNLOAD_ISO, proto.StageStatus.RUNNING))
        self.assertEqual(st.target_version, "2025")

        prog = proto.TransferProgress("downloading", 512, 1024, 50.0, 512.0, 1.0)
        st = sm.update_transfer_progress(prog)
        self.assertEqual(st.transfer, prog)

        st = sm.complete_iso_download("/tmp/max2025.iso", skipped=False)
        self.assertEqual((st.stage, st.status), (proto.Stage.INSTALL_MAX, proto.StageStatus.RUNNING))
        self.assertEqual(st.iso_local_path, "/tmp/max2025.iso")
        self.assertFalse(st.iso_skipped_download)

        st = sm.update_install_progress(65, "Installing Autodesk 3ds Max 2025")
        self.assertEqual(st.install_percent, 65)

        st = sm.complete_install()
        self.assertEqual((st.stage, st.status), (proto.Stage.WAIT_LICENSE, proto.StageStatus.WAITING_OPERATOR))

        st = sm.confirm_license()
        self.assertEqual((st.stage, st.status), (proto.Stage.FIRST_RUN, proto.StageStatus.RUNNING))

        st = sm.complete_first_run(auto_post_config=False)
        self.assertEqual((st.stage, st.status), (proto.Stage.POST_CONFIG, proto.StageStatus.WAITING_OPERATOR))

        steps = [
            proto.PostConfigStepState("corona", "Corona", "renderer", "pending"),
            proto.PostConfigStepState("verify", "Verify", "verify", "pending"),
        ]
        st = sm.start_post_config(steps)
        self.assertEqual((st.stage, st.status), (proto.Stage.POST_CONFIG, proto.StageStatus.RUNNING))

        st = sm.update_post_config_step("corona", "done", "Installed")
        self.assertEqual(st.post_config_steps[0].status, "done")
        st = sm.update_post_config_step("verify", "done", "Verified")

        st = sm.complete_post_config({"max_version": "2025", "verified": True})
        self.assertEqual((st.stage, st.status), (proto.Stage.DONE, proto.StageStatus.COMPLETED))
        self.assertTrue(st.summary.get("verified"))

    @feature("F-03")
    def test_state_machine_lifecycle_pause_resume_retry_skip_reset(self) -> None:
        """Verify orthogonal lifecycle transitions: pause/resume, fail/retry, abort/retry, skip, reset."""
        proto = require_module("remote.protocol")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        sm = DeploymentStateMachine(session_id="S-LIFE", client_id="c-2")
        sm.start_install("2025")
        st = sm.pause("operator paused")
        self.assertEqual((st.stage, st.status), (proto.Stage.DOWNLOAD_ISO, proto.StageStatus.PAUSED))
        st = sm.resume()
        self.assertEqual((st.stage, st.status), (proto.Stage.DOWNLOAD_ISO, proto.StageStatus.RUNNING))

        st = sm.fail("network reset")
        self.assertEqual(st.status, proto.StageStatus.FAILED)
        self.assertIn("network reset", st.last_error or "")
        st = sm.retry()
        self.assertEqual(st.status, proto.StageStatus.RUNNING)

        st = sm.abort("operator aborted")
        self.assertEqual(st.status, proto.StageStatus.ABORTED)
        st = sm.retry()
        self.assertEqual(st.status, proto.StageStatus.RUNNING)

        sm.complete_iso_download("/tmp/a.iso")
        sm.complete_install()
        st = sm.skip_stage(proto.Stage.POST_CONFIG)
        self.assertEqual(st.stage, proto.Stage.POST_CONFIG)
        st = sm.skip_stage(proto.Stage.DONE)
        self.assertEqual((st.stage, st.status), (proto.Stage.DONE, proto.StageStatus.COMPLETED))

        st = sm.reset(proto.Stage.PREP)
        self.assertEqual((st.stage, st.status), (proto.Stage.PREP, proto.StageStatus.IDLE))

    @feature("F-03")
    def test_state_machine_return_to_wait_license_and_auto_post_config(self) -> None:
        """Verify FIRST_RUN bounce-back to WAIT_LICENSE and auto_post_config=True direct entry."""
        proto = require_module("remote.protocol")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        sm = DeploymentStateMachine(session_id="S-LIC", client_id="c-3")
        sm.start_install("2025")
        sm.complete_iso_download("/tmp/a.iso")
        sm.complete_install()
        sm.confirm_license()

        st = sm.return_to_wait_license("licensing_required: AdskLicensingAgent shown")
        self.assertEqual((st.stage, st.status), (proto.Stage.WAIT_LICENSE, proto.StageStatus.WAITING_OPERATOR))
        self.assertIn("licensing", (st.last_error or "").lower())

        sm.confirm_license()
        st = sm.complete_first_run(auto_post_config=True)
        self.assertEqual((st.stage, st.status), (proto.Stage.POST_CONFIG, proto.StageStatus.RUNNING))

    @feature("F-04")
    def test_state_machine_atomic_persistence_and_reload(self) -> None:
        """Verify atomic JSON persistence to session_state.json and exact reload via load_or_create."""
        proto = require_module("remote.protocol")
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        with TempWorkspace("t1_sm_persist_") as ws:
            state_path = ws.client_data / "session_state.json"
            iso = proto.IsoSpec("f-2025", "Autodesk_3ds_Max_2025.iso", "2025", 4096, "d" * 64)
            sm = DeploymentStateMachine(state_path=state_path, session_id="S-P1", client_id="c-p1")
            sm.start_install("2025", iso)
            sm.complete_iso_download("/tmp/max2025.iso", skipped=True)
            sm.complete_install()

            self.assertTrue(state_path.exists())
            tmp_leftovers = [p for p in ws.client_data.iterdir() if ".tmp" in p.name]
            self.assertEqual(tmp_leftovers, [], f"Leftover temp files found: {tmp_leftovers}")

            reloaded = DeploymentStateMachine.load_or_create(state_path, "S-P1", "c-p1")
            self.assertEqual(reloaded.state.stage, proto.Stage.WAIT_LICENSE)
            self.assertEqual(reloaded.state.status, proto.StageStatus.WAITING_OPERATOR)
            self.assertEqual(reloaded.state.iso_local_path, "/tmp/max2025.iso")
            self.assertTrue(reloaded.state.iso_skipped_download)
            self.assertEqual(reloaded.state.iso_spec, iso)

    @feature("F-03", "F-18")
    def test_state_machine_can_accept_update_in_idle_states(self) -> None:
        """Verify can_accept_update() is True in all documented safe/idle states."""
        DeploymentStateMachine = require_attr("remote.state_machine", "DeploymentStateMachine")

        sm = DeploymentStateMachine(session_id="S-UPD", client_id="c-u1")
        self.assertTrue(sm.can_accept_update(), "PREP/idle must accept updates")

        sm.start_install("2025")
        sm.pause("wait")
        self.assertTrue(sm.can_accept_update(), "paused must accept updates")
        sm.resume()
        sm.fail("boom")
        self.assertTrue(sm.can_accept_update(), "failed must accept updates")
        sm.retry()
        sm.abort("stop")
        self.assertTrue(sm.can_accept_update(), "aborted must accept updates")
        sm.retry()
        sm.complete_iso_download("/tmp/x.iso")
        sm.complete_install()
        self.assertTrue(sm.can_accept_update(), "WAIT_LICENSE/waiting_operator must accept updates")

        sm.confirm_license()
        sm.complete_first_run(auto_post_config=False)
        self.assertTrue(sm.can_accept_update(), "POST_CONFIG/waiting_operator must accept updates")

        sm.start_post_config()
        sm.complete_post_config({"ok": True})
        self.assertTrue(sm.can_accept_update(), "DONE/completed must accept updates")


class TestTier1AuthAndPairing(unittest.TestCase):
    """Area C: Token generation, constant-time verification, connect.json & maxpair:// (F-05)."""

    @feature("F-05")
    def test_generate_session_id_and_high_entropy_token(self) -> None:
        """Verify generate_session_id() and generate_token() produce unique URL-safe values."""
        auth = require_module("remote.auth")
        sids = {auth.generate_session_id() for _ in range(30)}
        toks = {auth.generate_token() for _ in range(30)}
        self.assertEqual(len(sids), 30, "Session IDs must be unique")
        self.assertEqual(len(toks), 30, "Tokens must be unique")
        for sid in sids:
            self.assertTrue(sid and re.fullmatch(r"[A-Za-z0-9._-]+", sid))
        for tok in toks:
            self.assertGreaterEqual(len(tok), 16, "Token entropy length too short")
            self.assertRegex(tok, r"^[A-Za-z0-9._~=-]+$")

    @feature("F-05")
    def test_verify_token_accepts_exact_match_and_rejects_mismatch(self) -> None:
        """Verify verify_token() accepts exact matches and rejects mismatches/None."""
        verify_token = require_attr("remote.auth", "verify_token")
        tok = "SuperSecretToken_1234567890"
        self.assertTrue(verify_token(tok, tok))
        self.assertFalse(verify_token(tok, tok + "x"))
        self.assertFalse(verify_token(tok, tok[:-1]))
        self.assertFalse(verify_token(tok, tok.lower()))
        self.assertFalse(verify_token(tok, None))

    @feature("F-05")
    def test_pairing_config_connect_json_roundtrip(self) -> None:
        """Verify PairingConfig.to_connect_json() / from_connect_json() for direct and relay."""
        PairingConfig = require_attr("remote.auth", "PairingConfig")
        for mode, url in (
            ("direct", "http://192.168.1.50:8765"),
            ("relay", "https://relay.example.com:9443/base"),
        ):
            cfg = PairingConfig(mode=mode, server_url=url, session_id="S-4242", token="tok_abc123XYZ")
            raw = cfg.to_connect_json()
            parsed = json.loads(raw)
            self.assertIsInstance(parsed, dict)
            restored = PairingConfig.from_connect_json(raw)
            self.assertEqual(restored, cfg)

    @feature("F-05")
    def test_pairing_config_maxpair_uri_roundtrip(self) -> None:
        """Verify PairingConfig.to_pairing_uri() starts with maxpair:// and round-trips cleanly."""
        PairingConfig = require_attr("remote.auth", "PairingConfig")
        for mode, url in (
            ("direct", "http://10.0.0.5:8765"),
            ("relay", "https://tunnel.trycloudflare.com"),
        ):
            cfg = PairingConfig(mode=mode, server_url=url, session_id="S-URI1", token="tok_uri_987654")
            uri = cfg.to_pairing_uri()
            self.assertTrue(uri.startswith("maxpair://"), f"Expected maxpair:// URI, got {uri!r}")
            restored = PairingConfig.from_pairing_uri(uri)
            self.assertEqual(restored, cfg)

    @feature("F-05", "F-22")
    def test_operator_serves_prebaked_connect_json_endpoint(self) -> None:
        """Verify GET /api/v1/ui/pairing/connect.json returns valid PairingConfig matching server."""
        PairingConfig = require_attr("remote.auth", "PairingConfig")
        with TempWorkspace("t1_connect_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
                mode="direct",
            ) as op:
                raw = op.get_connect_json()
                cfg = PairingConfig.from_connect_json(raw)
                self.assertEqual(cfg.mode, "direct")
                self.assertEqual(cfg.session_id, op.session_id)
                self.assertEqual(cfg.token, op.token)
                self.assertIn(str(op.port), cfg.server_url)


class TestTier1TransportAndRelay(unittest.TestCase):
    """Area D: Direct HTTP/long-poll/SSE and Built-in Relay Server transport (F-07, F-08)."""

    @feature("F-07")
    def test_direct_client_register_heartbeat_state_logs_ack(self) -> None:
        """Verify all 5 client POST endpoints return 200 and populate Operator snapshot."""
        proto = require_module("remote.protocol")
        with TempWorkspace("t1_direct_api_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                cid = "client-direct-1"
                hdrs = auth_headers(op.token, op.session_id, cid)

                r_reg = http_request(
                    op.base_url,
                    "POST",
                    "/api/v1/clients/register",
                    headers=hdrs,
                    body={"client_id": cid, "hostname": "WIN-TEST"},
                )
                self.assertEqual(r_reg.status, 200, r_reg.text())
                self.assertTrue(r_reg.json().get("ok"))

                sys_status = proto.SystemStatus(
                    hostname="WIN-TEST",
                    os_version="Win11",
                    is_admin=True,
                    session_id=1,
                    smart_app_control_state="off",
                    smart_app_control_reboot_required=False,
                    defender_rtp_enabled=False,
                    defender_tamper_protected=False,
                    defender_exclusions=[],
                    third_party_av=[],
                    free_disk_bytes=50_000_000_000,
                    installed_max_versions=[],
                    ahk_available=True,
                    agent_version="1.0.0",
                    runtime_version="1.0.0",
                )
                r_hb = http_request(
                    op.base_url,
                    "POST",
                    f"/api/v1/clients/{cid}/heartbeat",
                    headers=hdrs,
                    body=sys_status.to_dict(),
                )
                self.assertEqual(r_hb.status, 200, r_hb.text())
                self.assertIn("server_time", r_hb.json())

                sm_state = proto.SessionState(
                    session_id=op.session_id,
                    client_id=cid,
                    stage=proto.Stage.PREP,
                    status=proto.StageStatus.IDLE,
                    target_version=None,
                    iso_spec=None,
                    iso_local_path=None,
                    iso_skipped_download=False,
                    transfer=None,
                    install_percent=0,
                    install_message="",
                    post_config_steps=[],
                    summary={},
                    last_error=None,
                    retry_count=0,
                    updated_at="2026-11-20T14:00:00Z",
                )
                r_st = http_request(
                    op.base_url,
                    "POST",
                    f"/api/v1/clients/{cid}/state",
                    headers=hdrs,
                    body=sm_state.to_dict(),
                )
                self.assertEqual(r_st.status, 200, r_st.text())

                logs = [
                    proto.LogEntry("2026-11-20T14:00:00Z", "INFO", "PREP", "Hello 1").to_dict(),
                    proto.LogEntry("2026-11-20T14:00:01Z", "INFO", "PREP", "Hello 2").to_dict(),
                ]
                r_logs = http_request(
                    op.base_url,
                    "POST",
                    f"/api/v1/clients/{cid}/logs",
                    headers=hdrs,
                    body={"entries": logs, "logs": logs},
                )
                self.assertEqual(r_logs.status, 200, r_logs.text())
                self.assertEqual(r_logs.json().get("accepted"), 2)

                ack = proto.CommandAck("cmd-99", "completed", "done", {"ok": True})
                r_ack = http_request(
                    op.base_url,
                    "POST",
                    f"/api/v1/clients/{cid}/ack",
                    headers=hdrs,
                    body=ack.to_dict(),
                )
                self.assertEqual(r_ack.status, 200, r_ack.text())
                self.assertIn(cid, op.get_clients())

    @feature("F-07")
    def test_direct_command_enqueue_and_long_poll_immediate_wake(self) -> None:
        """Verify long-poll wakes immediately when Operator enqueues a command."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t1_longpoll_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                cid = "client-lp-1"
                hdrs = auth_headers(op.token, op.session_id, cid)
                http_request(
                    op.base_url, "POST", "/api/v1/clients/register", headers=hdrs, body={"client_id": cid}
                )

                result_box: dict[str, object] = {}

                def _poll() -> None:
                    t0 = time.monotonic()
                    resp = http_request(
                        op.base_url,
                        "GET",
                        f"/api/v1/clients/{cid}/commands/poll?after_seq=0&wait=8",
                        headers=hdrs,
                        timeout=12.0,
                    )
                    result_box["resp"] = resp
                    result_box["elapsed"] = time.monotonic() - t0

                t = threading.Thread(target=_poll, daemon=True)
                t.start()
                time.sleep(0.25)
                enq = op.send_command(cid, CommandType.REFRESH_STATUS, {})
                t.join(timeout=5.0)
                self.assertFalse(t.is_alive(), "Long-poll did not wake up promptly")
                resp = result_box["resp"]
                self.assertEqual(resp.status, 200)
                cmds = resp.json().get("commands", [])
                self.assertEqual(len(cmds), 1)
                self.assertEqual(cmds[0]["cmd_id"], enq["cmd_id"])
                self.assertLess(float(result_box["elapsed"]), 3.0)

    @feature("F-07", "F-22")
    def test_operator_sse_stream_emits_live_events(self) -> None:
        """Verify GET /api/v1/ui/events streams text/event-stream frames on client updates."""
        with TempWorkspace("t1_sse_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with op.open_sse() as sse:
                    self.assertEqual(sse.status, 200)
                    self.assertIn("text/event-stream", sse.content_type.lower())
                    cid = "client-sse-1"
                    hdrs = auth_headers(op.token, op.session_id, cid)
                    r = http_request(
                        op.base_url,
                        "POST",
                        "/api/v1/clients/register",
                        headers=hdrs,
                        body={"client_id": cid},
                    )
                    self.assertEqual(r.status, 200)
                    ev = sse.wait_for_event(lambda e: cid in e.data_raw, timeout=8.0)
                    self.assertIn(cid, ev.data_raw)

    @feature("F-08")
    def test_relay_server_session_register_and_command_roundtrip(self) -> None:
        """Verify live Relay + Operator(relay) + Client(relay) registration and command flow."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t1_relay_cmd_") as ws:
            with LiveRelayHarness() as relay:
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
                        server_url=relay.base_url,
                        session_id=op.session_id,
                        token=op.token,
                        driver="simulated",
                        headless=True,
                    ):
                        cid = op.wait_for_client(timeout=15.0)
                        ack_cmd = op.send_command(cid, CommandType.REFRESH_STATUS, {})
                        self.assertIn("cmd_id", ack_cmd)
                        self.assertIn("seq", ack_cmd)
                        op.wait_for_stage(cid, "PREP", "idle", timeout=10.0)

    @feature("F-08", "F-09")
    def test_relay_server_streams_range_file_chunks(self) -> None:
        """Verify Relay streams HTTP 206 Partial Content chunks from Operator with exact SHA-256."""
        with TempWorkspace("t1_relay_range_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=262144, seed=11)
            with LiveRelayHarness() as relay:
                with LiveOperatorHarness(
                    iso_dir=ws.operator_isos,
                    assets_dir=ws.operator_assets,
                    updates_dir=ws.operator_updates,
                    mode="relay",
                    relay_url=relay.base_url,
                ) as op:
                    catalog = op.get_iso_catalog()
                    self.assertTrue(catalog, "Operator catalog must list synthetic 2025 ISO")
                    fid = catalog[0]["file_id"]
                    hdrs = auth_headers(op.token, op.session_id, "relay-dl-probe")
                    base_path = f"/relay/v1/sessions/{op.session_id}/files/{fid}"

                    r1 = http_request(
                        relay.base_url,
                        "GET",
                        base_path,
                        headers={**hdrs, "Range": "bytes=0-65535"},
                    )
                    self.assertEqual(r1.status, 206, r1.text())
                    self.assertEqual(len(r1.body), 65536)

                    r2 = http_request(
                        relay.base_url,
                        "GET",
                        base_path,
                        headers={**hdrs, "Range": "bytes=65536-262143"},
                    )
                    self.assertEqual(r2.status, 206, r2.text())
                    self.assertEqual(len(r2.body), 262144 - 65536)
                    self.assertEqual(sha256_bytes(r1.body + r2.body), iso.sha256)

    @feature("F-08", "F-11")
    def test_relay_mode_supports_direct_download_url_bypass(self) -> None:
        """Verify Relay control plane can direct ISO downloads via IsoSpec.download_url bypass."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t1_relay_bypass_") as ws:
            iso = create_synthetic_iso(ws.staging, version="2025", size_bytes=196608, seed=12)
            with FaultyRangeServer() as direct_srv:
                direct_url = direct_srv.register_file("bypass-2025", iso.path, iso.filename)
                with LiveRelayHarness() as relay:
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
                            server_url=relay.base_url,
                            session_id=op.session_id,
                            token=op.token,
                            driver="simulated",
                            headless=True,
                        ):
                            cid = op.wait_for_client(timeout=15.0)
                            iso_spec = {
                                "file_id": "bypass-2025",
                                "filename": iso.filename,
                                "version": "2025",
                                "size_bytes": iso.size_bytes,
                                "sha256": iso.sha256,
                                "download_url": direct_url,
                            }
                            op.send_command(
                                cid,
                                CommandType.START_INSTALL,
                                {"version": "2025", "iso_spec": iso_spec, "file_id": "bypass-2025"},
                            )
                            rec = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=25.0)
                            st = extract_client_state(rec)
                            self.assertFalse(st.get("iso_skipped_download"))
                            self.assertGreater(
                                len(direct_srv.get_file_requests()),
                                0,
                                "Expected direct download_url server to receive file requests",
                            )
                            local_iso = pathlib.Path(st["iso_local_path"])
                            self.assertTrue(local_iso.exists())
                            self.assertEqual(sha256_file(local_iso), iso.sha256)


class TestTier1IsoCatalogPrecheckAndTransfer(unittest.TestCase):
    """Area E: ISO Catalog, Local Pre-Check & Chunked Resumable Transfer (F-09, F-10, F-11)."""

    @feature("F-09")
    def test_iso_catalog_discovers_3dsmax_2024_to_2028_and_writes_sha256_sidecar(self) -> None:
        """Verify Operator catalog discovers 2025 & 2027 ISOs and caches <iso>.sha256.json sidecars."""
        with TempWorkspace("t1_iso_cat_") as ws:
            iso25 = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=131072, seed=21)
            iso27 = create_synthetic_iso(ws.operator_isos, version="2027", size_bytes=163840, seed=22)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                catalog = op.get_iso_catalog()
                by_ver = {str(item["version"]): item for item in catalog}
                self.assertIn("2025", by_ver)
                self.assertIn("2027", by_ver)
                self.assertEqual(by_ver["2025"]["sha256"], iso25.sha256)
                self.assertEqual(by_ver["2025"]["size_bytes"], iso25.size_bytes)
                self.assertEqual(by_ver["2027"]["sha256"], iso27.sha256)
                self.assertTrue(
                    pathlib.Path(f"{iso25.path}.sha256.json").exists(),
                    "Expected <file>.sha256.json sidecar cache to be created",
                )

    @feature("F-09")
    def test_iso_file_meta_and_http_206_range_streaming(self) -> None:
        """Verify GET /api/v1/files/{id}/meta and Range 206 Partial Content exact byte slice."""
        with TempWorkspace("t1_iso_206_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=131072, seed=23)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                fid = op.get_iso_catalog()[0]["file_id"]
                hdrs = auth_headers(op.token, op.session_id)
                r_meta = http_request(op.base_url, "GET", f"/api/v1/files/{fid}/meta", headers=hdrs)
                self.assertEqual(r_meta.status, 200, r_meta.text())
                meta = r_meta.json()
                for key in ("file_id", "filename", "size_bytes", "sha256", "chunk_size"):
                    self.assertIn(key, meta)
                self.assertEqual(meta["sha256"], iso.sha256)

                r_part = http_request(
                    op.base_url,
                    "GET",
                    f"/api/v1/files/{fid}",
                    headers={**hdrs, "Range": "bytes=1024-4095"},
                )
                self.assertEqual(r_part.status, 206, r_part.text())
                self.assertEqual(
                    r_part.header("content-range"), f"bytes 1024-4095/{iso.size_bytes}"
                )
                self.assertEqual(r_part.body, iso.path.read_bytes()[1024:4096])

    @feature("F-09")
    def test_iso_file_full_download_200_without_range_header(self) -> None:
        """Verify GET /api/v1/files/{id} without Range returns 200 OK with full exact bytes."""
        with TempWorkspace("t1_iso_200_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=65536, seed=24)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                fid = op.get_iso_catalog()[0]["file_id"]
                hdrs = auth_headers(op.token, op.session_id)
                resp = http_request(op.base_url, "GET", f"/api/v1/files/{fid}", headers=hdrs)
                self.assertEqual(resp.status, 200)
                self.assertEqual(sha256_bytes(resp.body), iso.sha256)

    @feature("F-10")
    def test_client_iso_local_precheck_finds_verified_iso_and_skips_download(self) -> None:
        """Verify valid pre-placed local ISO skips network download completely (0 file GETs)."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t1_precheck_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=196608, seed=25)
            local_copy = ws.client_isos / iso.filename
            local_copy.write_bytes(iso.path.read_bytes())

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
                        rec = op.wait_for_stage(cid, "WAIT_LICENSE", "waiting_operator", timeout=20.0)
                        st = extract_client_state(rec)
                        self.assertTrue(st.get("iso_skipped_download"), f"Expected download skip: {st}")
                        self.assertEqual(
                            len(proxy.file_downloads()),
                            0,
                            f"Expected zero HTTP file download calls on local hit, saw {proxy.file_downloads()}",
                        )

    @feature("F-11")
    def test_chunked_iso_transfer_downloads_verifies_sha256_and_renames_atomically(self) -> None:
        """Verify chunked transfer downloads ISO, verifies SHA-256, and leaves zero .part files."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t1_xfer_clean_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=393216, seed=26)
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
                    self.assertFalse(st.get("iso_skipped_download"))
                    dest = pathlib.Path(st["iso_local_path"])
                    self.assertTrue(dest.exists())
                    self.assertEqual(sha256_file(dest), iso.sha256)
                    part_files = [p for p in ws.client_isos.glob("*.part*")]
                    self.assertEqual(part_files, [], f"Leftover .part files remain: {part_files}")

    @feature("F-11")
    def test_chunked_iso_transfer_emits_bytes_percent_speed_eta_telemetry(self) -> None:
        """Verify TransferProgress telemetry reports bytes_done, bytes_total, percent, and speed."""
        CommandType = require_attr("remote.protocol", "CommandType")
        with TempWorkspace("t1_xfer_telem_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=327680, seed=27)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                with op.open_sse() as sse:
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
                        tr = st.get("transfer")
                        self.assertIsInstance(tr, dict, f"Expected TransferProgress dict in state: {st}")
                        self.assertEqual(tr["bytes_total"], iso.size_bytes)
                        self.assertEqual(tr["bytes_done"], iso.size_bytes)
                        self.assertGreaterEqual(float(tr["percent"]), 99.9)
                        self.assertGreaterEqual(float(tr["speed_bps"]), 0.0)
                        self.assertTrue(len(sse.snapshot_events()) >= 1)


class TestTier1DriversAndSecurityControls(unittest.TestCase):
    """Area F: Driver Protocols, Simulated Bundle, UAC, Smart App Control, Defender (F-06, F-12..F-14)."""

    @feature("F-06")
    def test_driver_protocols_and_simulated_bundle_contract(self) -> None:
        """Verify DriverBundle and all 4 driver Protocols execute cleanly on Linux."""
        base_mod = require_module("remote.drivers.base")
        for proto_name in ("SystemDriver", "InstallerDriver", "FirstRunDriver", "PostConfigDriver", "DriverBundle"):
            self.assertTrue(hasattr(base_mod, proto_name), f"Missing {proto_name} in remote.drivers.base")
        bundle = discover_simulated_drivers()
        self.assertIsNotNone(bundle.system)
        self.assertIsNotNone(bundle.installer)
        self.assertIsNotNone(bundle.first_run)
        self.assertIsNotNone(bundle.post_config)

        steps = bundle.post_config.load_steps(version="2025")
        self.assertTrue(len(steps) >= 1)
        fr = bundle.first_run.run_first_run(
            "2025", log_cb=lambda lvl, msg: None, cancel_event=threading.Event(), timeout_sec=10
        )
        self.assertIn(fr["status"], ("ready", "licensing_required", "timeout", "failed", "aborted"))
        self.assertIsInstance(fr["matched_rules"], list)
        self.assertIsInstance(fr["screenshots"], list)

    @feature("F-06", "F-12")
    def test_simulated_system_driver_collect_status_schema(self) -> None:
        """Verify SimulatedSystemDriver.collect_status() returns a complete SystemStatus."""
        SystemStatus = require_attr("remote.protocol", "SystemStatus")
        bundle = discover_simulated_drivers()
        status = bundle.system.collect_status(agent_version="1.2.0", runtime_version="1.0.0")
        self.assertIsInstance(status, SystemStatus)
        self.assertTrue(status.hostname)
        self.assertIsInstance(status.is_admin, bool)
        self.assertGreater(status.session_id, 0)
        self.assertIn(status.smart_app_control_state, ("off", "enforced", "evaluation", "unknown"))
        self.assertGreater(status.free_disk_bytes, 0)
        self.assertEqual(status.agent_version, "1.2.0")
        self.assertEqual(status.runtime_version, "1.0.0")

    @feature("F-12")
    def test_uac_elevation_and_interactive_session_contract(self) -> None:
        """Verify elevation contract in SimulatedSystemDriver and Windows/Bootstrap source contracts."""
        bundle = discover_simulated_drivers()
        res = bundle.system.request_elevation(sys.executable, ["-V"], str(REPO_ROOT))
        self.assertIsInstance(res, dict)

        win_src = require_file("remote/drivers/windows.py").read_text(encoding="utf-8")
        boot_src = require_file("remote/client/bootstrap.py").read_text(encoding="utf-8")
        combined = win_src + "\n" + boot_src
        self.assertIn("IsUserAnAdmin", combined)
        self.assertIn("ShellExecuteW", combined)
        self.assertIn("runas", combined)
        self.assertIn("MessageBoxW", combined)
        self.assertRegex(combined, r"(SessionId|ProcessIdToSessionId)")

    @feature("F-13")
    def test_smart_app_control_status_and_guided_disable_contract(self) -> None:
        """Verify SAC status/disable API and Windows registry/CiTool/URI contract."""
        bundle = discover_simulated_drivers()
        st = bundle.system.get_smart_app_control_status()
        self.assertIsInstance(st, dict)
        res = bundle.system.disable_smart_app_control(mode="guided")
        self.assertIsInstance(res, dict)

        win_src = require_file("remote/drivers/windows.py").read_text(encoding="utf-8")
        self.assertIn("VerifiedAndReputablePolicyState", win_src)
        self.assertIn("CiTool", win_src)
        self.assertIn("windowsdefender://smartapp/", win_src)

    @feature("F-14")
    def test_defender_status_exclusions_and_disable_restore_contract(self) -> None:
        """Verify Defender status, exclusion ledger add/restore, and Windows driver API contract."""
        bundle = discover_simulated_drivers()
        st = bundle.system.get_defender_status()
        self.assertIsInstance(st, dict)

        add_res = bundle.system.configure_defender("add_exclusions", ["/tmp/e2e_iso_dir"])
        self.assertIsInstance(add_res, dict)
        status_after = bundle.system.collect_status("1.0.0", "1.0.0")
        self.assertIn("/tmp/e2e_iso_dir", status_after.defender_exclusions)

        rest_res = bundle.system.configure_defender("restore")
        self.assertIsInstance(rest_res, dict)
        status_restored = bundle.system.collect_status("1.0.0", "1.0.0")
        self.assertNotIn("/tmp/e2e_iso_dir", status_restored.defender_exclusions)

        win_src = require_file("remote/drivers/windows.py").read_text(encoding="utf-8")
        for token in (
            "Get-MpComputerStatus",
            "IsTamperProtected",
            "Add-MpPreference",
            "Remove-MpPreference",
            "windowsdefender://threatsettings/",
        ):
            self.assertIn(token, win_src, f"Missing Defender contract token {token!r} in windows.py")

    @feature("F-06", "F-12", "F-13", "F-14")
    def test_windows_driver_module_imports_cleanly_on_linux_and_exposes_protocols(self) -> None:
        """Verify remote.drivers.windows imports safely on Linux and exposes all 4 driver classes."""
        win_mod = require_module("remote.drivers.windows")
        SystemStatus = require_attr("remote.protocol", "SystemStatus")
        for cls_name in (
            "WindowsSystemDriver",
            "WindowsInstallerDriver",
            "WindowsFirstRunDriver",
            "WindowsPostConfigDriver",
        ):
            self.assertTrue(hasattr(win_mod, cls_name), f"Missing {cls_name} in remote.drivers.windows")

        sys_drv = win_mod.WindowsSystemDriver()
        st = sys_drv.collect_status("1.0.0", "1.0.0")
        self.assertIsInstance(st, SystemStatus)
        self.assertIsInstance(sys_drv.get_smart_app_control_status(), dict)
        self.assertIsInstance(sys_drv.get_defender_status(), dict)


class TestTier1InstallerFirstRunAndPostConfig(unittest.TestCase):
    """Area G: Install-3dsMax.ps1 hardening, max-auto CLI, and 1-click POST_CONFIG (F-15..F-17)."""

    @feature("F-15")
    def test_install_ps1_hardening_parameters_and_status_format(self) -> None:
        """Verify vm/Install-3dsMax.ps1 adds -QuietConsole, wires -StatusRefreshSeconds, disk/MOTW/ODIS."""
        ps1 = require_file("vm/Install-3dsMax.ps1").read_text(encoding="utf-8")
        self.assertRegex(ps1, r"\[switch\]\s*\$QuietConsole")
        for legacy_param in (
            "$Version",
            "$IsoPath",
            "$InstallTasks",
            "$InstallerTimeoutSeconds",
            "$StatusRefreshSeconds",
            "$SelectIso",
            "$Interactive",
            "$KeepIsoMounted",
            "$ListOnly",
            "$PauseOnExit",
        ):
            self.assertIn(legacy_param, ps1, f"Legacy parameter {legacy_param} removed from Install-3dsMax.ps1")

        # Verify StatusRefreshSeconds is used multiple times (param + actual wait loop)
        self.assertGreaterEqual(
            ps1.count("StatusRefreshSeconds"),
            3,
            "-StatusRefreshSeconds must be wired into the installer monitoring loop",
        )
        self.assertIn("-status.txt", ps1)
        self.assertRegex(ps1, r"(Unblock-File|Zone\.Identifier)")
        self.assertIn("ODIS", ps1)
        self.assertRegex(ps1, r"(Get-PSDrive|DriveInfo|FreeSpace|Free\b)")

    @feature("F-15", "F-06")
    def test_installer_driver_progress_and_completion_contract(self) -> None:
        """Verify SimulatedInstallerDriver emits monotonic 0..100 progress and marks Max installed."""
        bundle = discover_simulated_drivers()
        progress_events: list[tuple[int, str]] = []
        logs: list[tuple[str, str]] = []
        with TempWorkspace("t1_inst_drv_") as ws:
            iso = create_synthetic_iso(ws.client_isos, version="2025", size_bytes=4096)
            res = bundle.installer.install_3dsmax(
                "2025",
                iso.path,
                progress_cb=lambda pct, msg: progress_events.append((pct, msg)),
                log_cb=lambda lvl, msg: logs.append((lvl, msg)),
                cancel_event=threading.Event(),
                timeout_sec=30,
            )
            self.assertIsInstance(res, dict)
            self.assertTrue(progress_events, "Expected progress_cb invocations")
            percents = [p for p, _ in progress_events]
            self.assertEqual(percents, sorted(percents), "Progress percentages must be non-decreasing")
            self.assertEqual(percents[-1], 100)
            self.assertTrue(bundle.installer.is_max_installed("2025"))

    @feature("F-16")
    def test_max_auto_cli_exposes_programmatic_run_flags(self) -> None:
        """Verify `python -m cli.main run --help` exposes all contracted F-16 flags and WatchResult."""
        WatchResult = require_attr("cli.watcher", "WatchResult")
        self.assertIsNotNone(WatchResult)
        res = run_cli_subprocess(["-m", "cli.main", "run", "--help"], timeout=15.0)
        self.assertEqual(res.returncode, 0, f"cli.main run --help failed:\n{res.stderr}")
        help_out = res.stdout + res.stderr
        for flag in ("--version", "--profile", "--max-timeout", "--dump-dir", "--json-result", "--ahk-exe"):
            self.assertIn(flag, help_out, f"Missing required CLI flag {flag!r} in max-auto run")

        rules_text = stdlib_check_yaml_well_formed(require_file("config/rules.yaml"))
        self.assertIn("target_processes:", rules_text)
        self.assertIn("ignore_windows:", rules_text)
        self.assertIn("rules:", rules_text)

    @feature("F-16")
    def test_ahk_bridge_resolves_bundled_exe_and_env_override(self) -> None:
        """Verify cli.ahk_bridge.AHKBridge honors MAX_AUTO_AHK_EXE override."""
        AHKBridge = require_attr("cli.ahk_bridge", "AHKBridge")
        with TempWorkspace("t1_ahk_") as ws:
            fake_ahk = ws.root / "AutoHotkey64.exe"
            fake_ahk.write_bytes(b"MZ_FAKE_AHK")
            old = os.environ.get("MAX_AUTO_AHK_EXE")
            os.environ["MAX_AUTO_AHK_EXE"] = str(fake_ahk)
            try:
                bridge = AHKBridge(dry_run=True)
                self.assertEqual(pathlib.Path(bridge.ahk_exe).resolve(), fake_ahk.resolve())
            finally:
                if old is None:
                    os.environ.pop("MAX_AUTO_AHK_EXE", None)
                else:
                    os.environ["MAX_AUTO_AHK_EXE"] = old

    @feature("F-16", "F-17")
    def test_max_deployer_supports_de_elevate_and_recursive_scripts(self) -> None:
        """Verify launch_3dsmax accepts de_elevate and deploy_scripts handles nested scripts + .mzp."""
        deployer = require_module("cli.max_deployer")
        sig = inspect.signature(deployer.launch_3dsmax)
        self.assertIn("de_elevate", sig.parameters, "launch_3dsmax must accept de_elevate parameter")

        with TempWorkspace("t1_deploy_") as ws:
            assets = create_synthetic_post_config_assets(ws.operator_assets, version="2025")
            old_local = os.environ.get("LOCALAPPDATA")
            os.environ["LOCALAPPDATA"] = str(ws.client_enu)
            try:
                deployer.deploy_scripts(assets["ms"].parent, max_version="2025")
                deployed_files = [p.name for p in ws.client_enu.rglob("*") if p.is_file()]
                self.assertIn("studio_startup.ms", deployed_files)
                self.assertIn("helper_module.ms", deployed_files)
                self.assertIn("StudioTools.mcr", deployed_files)
                self.assertIn("pack_init.ms", deployed_files)
            finally:
                if old_local is None:
                    os.environ.pop("LOCALAPPDATA", None)
                else:
                    os.environ["LOCALAPPDATA"] = old_local

    @feature("F-17")
    def test_post_config_yaml_schema_and_all_categories(self) -> None:
        """Verify config/post_config.yaml, config/agent.example.yaml, and WindowsPostConfigDriver.load_steps."""
        pc_text = stdlib_check_yaml_well_formed(require_file("config/post_config.yaml"))
        stdlib_check_yaml_well_formed(require_file("config/agent.example.yaml"))
        lower = pc_text.lower()
        for keyword in ("corona", "vray", "theme", "quad", "script", "verify"):
            self.assertTrue(
                keyword in lower or keyword.replace("vray", "v-ray") in lower,
                f"config/post_config.yaml missing coverage of {keyword!r}",
            )

        WindowsPostConfigDriver = require_attr("remote.drivers.windows", "WindowsPostConfigDriver")
        steps = WindowsPostConfigDriver().load_steps(
            config_path=REPO_ROOT / "config/post_config.yaml", version="2025"
        )
        self.assertTrue(len(steps) >= 4)
        valid_cats = {"renderer", "theme", "quad_menu", "hotkey", "scripts", "verify"}
        for s in steps:
            self.assertIn(s.category, valid_cats)

    @feature("F-17")
    def test_verify_install_ms_is_version_dynamic_and_writes_json(self) -> None:
        """Verify max_scripts/verify_install.ms queries maxVersion() dynamically and writes max_verify.json."""
        ms_text = require_file("max_scripts/verify_install.ms").read_text(encoding="utf-8")
        self.assertIn("max_verify.json", ms_text)
        self.assertIn("maxVersion", ms_text)
        self.assertRegex(ms_text, r"rendererClass|renderers")


class TestTier1AutoUpdaterAndBootstrap(unittest.TestCase):
    """Area H: Atomic background app/ updater and elevated supervisor bootstrap (F-18, F-19)."""

    @feature("F-19")
    def test_bootstrap_defines_update_restart_exit_code_42(self) -> None:
        """Verify UPDATE_RESTART_EXIT_CODE == 42 in remote.client.bootstrap."""
        code = require_attr("remote.client.bootstrap", "UPDATE_RESTART_EXIT_CODE")
        self.assertEqual(code, 42)

    @feature("F-18", "F-22")
    def test_operator_serves_updates_latest_manifest(self) -> None:
        """Verify GET /api/v1/updates/latest serves valid UpdateManifest when update bundle exists."""
        UpdateManifest = require_attr("remote.protocol", "UpdateManifest")
        with TempWorkspace("t1_upd_latest_") as ws:
            res = invoke_build_app_update("2.0.0", ws.operator_updates, min_runtime_version="1.0.0")
            self.assertEqual(res.returncode, 0, res.stderr)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                hdrs = auth_headers(op.token, op.session_id)
                resp = http_request(op.base_url, "GET", "/api/v1/updates/latest", headers=hdrs)
                self.assertEqual(resp.status, 200, resp.text())
                manifest = UpdateManifest.from_dict(resp.json())
                self.assertEqual(manifest.version, "2.0.0")
                self.assertEqual(len(manifest.sha256), 64)
                self.assertGreater(manifest.size_bytes, 0)

    @feature("F-18")
    def test_updater_stages_verified_release_and_swaps_active_json(self) -> None:
        """Verify remote.updater verifies SHA-256, extracts app/releases/<ver>/, and swaps active.json."""
        UpdateManifest = require_attr("remote.protocol", "UpdateManifest")
        updater_mod = require_module("remote.updater")
        with TempWorkspace("t1_upd_stage_") as ws:
            (ws.app_root / "app" / "releases" / "1.0.0").mkdir(parents=True, exist_ok=True)
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
            zip_path, digest, size = create_app_update_zip(ws.staging / "app-2.0.0.zip", "2.0.0")
            with FaultyRangeServer() as srv:
                url = srv.register_file("upd-200", zip_path, zip_path.name)
                manifest = UpdateManifest(
                    version="2.0.0",
                    min_runtime_version="1.0.0",
                    file_id="upd-200",
                    filename=zip_path.name,
                    size_bytes=size,
                    sha256=digest,
                    download_url=url,
                )
                updater_cls = getattr(updater_mod, "AppUpdater", None) or getattr(
                    updater_mod, "Updater", None
                )
                self.assertIsNotNone(updater_cls, "remote.updater must expose AppUpdater or Updater")
                sig = inspect.signature(updater_cls.__init__)
                kw: dict[str, object] = {}
                for k, v in (
                    ("app_dir", ws.app_root / "app"),
                    ("root_dir", ws.app_root),
                    ("base_dir", ws.app_root),
                    ("runtime_version", "1.0.0"),
                    ("current_version", "1.0.0"),
                ):
                    if k in sig.parameters:
                        kw[k] = v
                updater = updater_cls(**kw)
                apply_fn = (
                    getattr(updater, "apply_update", None)
                    or getattr(updater, "stage_and_activate", None)
                    or getattr(updater, "download_and_apply", None)
                )
                self.assertIsNotNone(apply_fn, "Updater must expose an apply/stage method")
                a_sig = inspect.signature(apply_fn)
                a_kw: dict[str, object] = {}
                if "download_url" in a_sig.parameters:
                    a_kw["download_url"] = url
                if "archive_path" in a_sig.parameters:
                    a_kw["archive_path"] = zip_path
                apply_fn(manifest, **a_kw)

            self.assertTrue((ws.app_root / "app" / "releases" / "2.0.0" / "VERSION.txt").exists())
            active = json.loads(active_path.read_text(encoding="utf-8"))
            self.assertEqual(active["version"], "2.0.0")
            self.assertEqual(active["previous"], "1.0.0")
            self.assertTrue(active["pending_verify"])

    @feature("F-19")
    def test_bootstrap_supervisor_restarts_child_on_exit_42_without_re_elevation(self) -> None:
        """Run bootstrap.py as real subprocess: child 1.0.0 swaps to 1.1.0 + exits 42; 1.1.0 boots."""
        with TempWorkspace("t1_boot42_") as ws:
            log_file = ws.logs / "boot_order.txt"
            rel1 = ws.app_root / "app" / "releases" / "1.0.0" / "remote" / "client"
            rel2 = ws.app_root / "app" / "releases" / "1.1.0" / "remote" / "client"
            rel1.mkdir(parents=True, exist_ok=True)
            rel2.mkdir(parents=True, exist_ok=True)
            for r in (rel1, rel2):
                (r.parent / "__init__.py").write_text("", encoding="utf-8")
                (r / "__init__.py").write_text("", encoding="utf-8")

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
            (rel1 / "main.py").write_text(
                f"import json, pathlib, sys\n"
                f"with open({str(log_file)!r}, 'a', encoding='utf-8') as f: f.write('1.0.0\\n')\n"
                f"p = pathlib.Path({str(active_path)!r})\n"
                f"p.write_text(json.dumps({{'version':'1.1.0','previous':'1.0.0','pending_verify':True,'attempts':0,'blocked_versions':[]}}), encoding='utf-8')\n"
                f"sys.exit(42)\n",
                encoding="utf-8",
            )
            (rel2 / "main.py").write_text(
                f"import json, pathlib, sys\n"
                f"with open({str(log_file)!r}, 'a', encoding='utf-8') as f: f.write('1.1.0\\n')\n"
                f"p = pathlib.Path({str(active_path)!r})\n"
                f"d = json.loads(p.read_text(encoding='utf-8'))\n"
                f"d['pending_verify'] = False\n"
                f"d['attempts'] = 0\n"
                f"p.write_text(json.dumps(d), encoding='utf-8')\n"
                f"sys.exit(0)\n",
                encoding="utf-8",
            )
            res = run_bootstrap_in_sandbox(ws.app_root, timeout=15.0)
            self.assertEqual(res.returncode, 0, f"bootstrap failed:\n{res.stdout}\n{res.stderr}")
            lines = log_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(lines, ["1.0.0", "1.1.0"])
            active = json.loads(active_path.read_text(encoding="utf-8"))
            self.assertEqual(active["version"], "1.1.0")
            self.assertFalse(active["pending_verify"])

    @feature("F-18", "F-19")
    def test_bootstrap_clears_pending_verify_on_healthy_boot(self) -> None:
        """Verify healthy boot helper clears pending_verify and resets attempts to 0."""
        updater_mod = require_module("remote.updater")
        with TempWorkspace("t1_healthy_") as ws:
            app_dir = ws.app_root / "app"
            (app_dir / "releases" / "1.1.0").mkdir(parents=True, exist_ok=True)
            active_path = app_dir / "active.json"
            active_path.write_text(
                json.dumps(
                    {
                        "version": "1.1.0",
                        "previous": "1.0.0",
                        "pending_verify": True,
                        "attempts": 1,
                        "blocked_versions": [],
                    }
                ),
                encoding="utf-8",
            )
            fn = getattr(updater_mod, "mark_healthy_boot", None) or getattr(
                updater_mod, "confirm_healthy_boot", None
            )
            if callable(fn):
                fn(app_dir)
            else:
                updater_cls = getattr(updater_mod, "AppUpdater", None) or getattr(
                    updater_mod, "Updater"
                )
                sig = inspect.signature(updater_cls.__init__)
                kw = {
                    k: (app_dir if k == "app_dir" else ws.app_root)
                    for k in ("app_dir", "root_dir", "base_dir")
                    if k in sig.parameters
                }
                upd = updater_cls(**kw)
                m = getattr(upd, "mark_healthy_boot", None) or getattr(upd, "confirm_healthy_boot")
                m()

            active = json.loads(active_path.read_text(encoding="utf-8"))
            self.assertFalse(active["pending_verify"])
            self.assertEqual(active["attempts"], 0)

    @feature("F-18")
    def test_updater_prunes_old_releases_keeping_active_and_previous(self) -> None:
        """Verify release pruning removes obsolete releases while preserving active and previous."""
        updater_mod = require_module("remote.updater")
        with TempWorkspace("t1_prune_") as ws:
            app_dir = ws.app_root / "app"
            for ver in ("1.0.0", "1.1.0", "1.2.0", "1.3.0"):
                d = app_dir / "releases" / ver
                d.mkdir(parents=True, exist_ok=True)
                (d / "VERSION.txt").write_text(ver, encoding="utf-8")
            (app_dir / "active.json").write_text(
                json.dumps(
                    {
                        "version": "1.3.0",
                        "previous": "1.2.0",
                        "pending_verify": False,
                        "attempts": 0,
                        "blocked_versions": [],
                    }
                ),
                encoding="utf-8",
            )
            fn = getattr(updater_mod, "prune_releases", None)
            if callable(fn):
                fn(app_dir)
            else:
                updater_cls = getattr(updater_mod, "AppUpdater", None) or getattr(
                    updater_mod, "Updater"
                )
                sig = inspect.signature(updater_cls.__init__)
                kw = {
                    k: (app_dir if k == "app_dir" else ws.app_root)
                    for k in ("app_dir", "root_dir", "base_dir")
                    if k in sig.parameters
                }
                upd = updater_cls(**kw)
                getattr(upd, "prune_releases")()

            remaining = {p.name for p in (app_dir / "releases").iterdir() if p.is_dir()}
            self.assertIn("1.3.0", remaining)
            self.assertIn("1.2.0", remaining)
            self.assertNotIn("1.0.0", remaining)


class TestTier1AgentOperatorAndClientUIs(unittest.TestCase):
    """Area I: Client Agent, Operator Control Panel Web UI, and Client Status UI (F-21..F-23)."""

    @feature("F-22")
    def test_operator_serves_html_css_js_control_panel_assets(self) -> None:
        """Verify Operator serves /, /static/styles.css, /static/app.js with full UI controls."""
        with TempWorkspace("t1_op_ui_") as ws:
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                r_html = http_request(op.base_url, "GET", "/")
                r_css = http_request(op.base_url, "GET", "/static/styles.css")
                r_js = http_request(op.base_url, "GET", "/static/app.js")
                self.assertEqual(r_html.status, 200)
                self.assertIn("text/html", r_html.header("content-type").lower())
                self.assertEqual(r_css.status, 200)
                self.assertIn("text/css", r_css.header("content-type").lower())
                self.assertEqual(r_js.status, 200)
                self.assertIn("javascript", r_js.header("content-type").lower())

                combined = (r_html.text() + "\n" + r_js.text()).lower()
                for token in (
                    "eventsource",
                    "connect.json",
                    "prep",
                    "download_iso",
                    "install_max",
                    "wait_license",
                    "first_run",
                    "post_config",
                    "done",
                    "defender",
                    "smart",
                ):
                    self.assertIn(token, combined, f"Operator UI missing control/token {token!r}")

    @feature("F-22")
    def test_operator_ui_snapshot_contains_all_required_sections(self) -> None:
        """Verify GET /api/v1/ui/snapshot exposes pairing, clients, ISO catalog, post-config, updates."""
        with TempWorkspace("t1_op_snap_") as ws:
            create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=65536)
            with LiveOperatorHarness(
                iso_dir=ws.operator_isos,
                assets_dir=ws.operator_assets,
                updates_dir=ws.operator_updates,
            ) as op:
                snap = op.snapshot()
                self.assertIsInstance(snap, dict)
                self.assertIn("pairing", snap)
                self.assertIn("clients", snap)
                self.assertTrue(
                    any(k in snap for k in ("iso_catalog", "isos", "catalog")),
                    f"Missing ISO catalog key in snapshot: {list(snap.keys())}",
                )
                self.assertTrue(
                    any(k in snap for k in ("post_config_steps", "post_config", "steps")),
                    f"Missing post-config steps key in snapshot: {list(snap.keys())}",
                )
                self.assertTrue(
                    any(k in snap for k in ("update_manifest", "latest_update", "update")),
                    f"Missing update manifest key in snapshot: {list(snap.keys())}",
                )

    @feature("F-21")
    def test_client_agent_registers_and_reports_heartbeats_to_operator(self) -> None:
        """Verify live Client Agent connects, registers, reports SystemStatus, and enters PREP/idle."""
        with TempWorkspace("t1_agent_reg_") as ws:
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
                    rec = op.wait_for_stage(cid, "PREP", "idle", timeout=10.0)
                    sys_st = extract_client_system_status(rec)
                    self.assertTrue(sys_st.get("hostname"), f"Expected hostname in heartbeat: {rec}")

    @feature("F-21", "F-17")
    def test_client_agent_produces_done_verification_summary(self) -> None:
        """Verify Client Agent completes POST_CONFIG into DONE with non-empty verification summary."""
        proto = require_module("remote.protocol")
        with TempWorkspace("t1_agent_done_") as ws:
            iso = create_synthetic_iso(ws.operator_isos, version="2025", size_bytes=65536, seed=31)
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
                ):
                    cid = op.wait_for_client(timeout=15.0)
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
                    rec = op.wait_for_stage(cid, "DONE", "completed", timeout=20.0)
                    st = extract_client_state(rec)
                    self.assertIsInstance(st.get("summary"), dict)
                    self.assertTrue(len(st["summary"]) > 0, f"Expected non-empty DONE summary: {st}")

    @feature("F-23")
    def test_client_status_ui_serves_teamviewer_dashboard_over_localhost(self) -> None:
        """Verify Client Status UI serves localhost HTML dashboard visible over TeamViewer."""
        with TempWorkspace("t1_status_ui_") as ws:
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
                    resp = client.wait_for_status_ui(timeout=12.0)
                    self.assertEqual(resp.status, 200)
                    self.assertIn("text/html", resp.header("content-type").lower())
                    body_l = resp.text().lower()
                    for token in ("admin", "defender", "smart", "log"):
                        self.assertIn(token, body_l, f"Client Status UI missing {token!r}")

    @feature("F-23")
    def test_client_headless_mode_skips_browser_launch_cleanly(self) -> None:
        """Verify remote/client/status_ui.py has zero tkinter imports and runs cleanly headless."""
        ui_path = require_file("remote/client/status_ui.py")
        tree = ast.parse(ui_path.read_text(encoding="utf-8"), filename=str(ui_path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    self.assertNotEqual(alias.name.split(".", 1)[0], "tkinter")
            elif isinstance(node, ast.ImportFrom) and node.module:
                self.assertNotEqual(node.module.split(".", 1)[0], "tkinter")

        with TempWorkspace("t1_headless_") as ws:
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
                    op.wait_for_client(timeout=15.0)
                    assert client.proc is not None
                    self.assertTrue(client.proc.is_alive())


class TestTier1E2EMetaContracts(unittest.TestCase):
    """Area J: E2E Runner & Suite Integrity Meta-Contracts (F-24)."""

    @feature("F-24")
    def test_runner_check_syntax_exits_zero(self) -> None:
        """Verify python3 tests/e2e/run_e2e.py --check-syntax exits 0."""
        res = run_cli_subprocess(["tests/e2e/run_e2e.py", "--check-syntax"], timeout=20.0)
        self.assertEqual(
            res.returncode,
            0,
            f"--check-syntax failed:\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}",
        )
        self.assertIn("24/24", res.stdout)

    @feature("F-24")
    def test_runner_list_outputs_all_4_tiers_and_24_features(self) -> None:
        """Verify python3 tests/e2e/run_e2e.py --list lists all 4 tiers and F-01..F-24."""
        res = run_cli_subprocess(["tests/e2e/run_e2e.py", "--list"], timeout=20.0)
        self.assertEqual(res.returncode, 0, res.stderr)
        for label in ("Tier 1", "Tier 2", "Tier 3", "Tier 4"):
            self.assertIn(label, res.stdout)
        for fid in ALL_FEATURE_IDS:
            self.assertIn(fid, res.stdout, f"Feature {fid} missing from --list output")

    @feature("F-24")
    def test_runner_list_filters_by_single_tier(self) -> None:
        """Verify python3 tests/e2e/run_e2e.py --list --tier 4 lists all 8 Tier 4 scenarios only."""
        res = run_cli_subprocess(["tests/e2e/run_e2e.py", "--list", "--tier", "4"], timeout=20.0)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn("Tier 4", res.stdout)
        self.assertNotIn("Tier 1 —", res.stdout)
        for n in range(1, 9):
            self.assertIn(f"test_scenario_0{n}_", res.stdout)

    @feature("F-24")
    def test_e2e_suite_contains_zero_skip_calls(self) -> None:
        """AST-verify that no file in tests/e2e/*.py uses any unittest skip mechanism."""
        e2e_dir = REPO_ROOT / "tests" / "e2e"
        for py_file in sorted(e2e_dir.glob("*.py")):
            errs = [
                e for e in audit_python_file_stdlib_and_no_skips(py_file) if "skip" in e.lower()
            ]
            self.assertEqual(errs, [], f"Forbidden skip usage found: {errs}")

    @feature("F-24", "F-01")
    def test_e2e_suite_imports_are_stdlib_only(self) -> None:
        """AST-verify that all imports across tests/e2e/*.py are strictly Python stdlib or tests.e2e."""
        e2e_dir = REPO_ROOT / "tests" / "e2e"
        for py_file in sorted(e2e_dir.glob("*.py")):
            errs = [
                e for e in audit_python_file_stdlib_and_no_skips(py_file) if "import" in e.lower()
            ]
            self.assertEqual(errs, [], f"Forbidden non-stdlib imports found: {errs}")
