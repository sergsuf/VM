"""Unit tests for pyproject metadata, protocol models, auth/pairing, and simulated drivers."""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import tomllib
import unittest
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import remote
from remote.auth import (
    PairingConfig,
    extract_bearer_token,
    generate_session_id,
    generate_token,
    verify_token,
)
from remote.drivers import (
    DriverBundle,
    FirstRunDriver,
    InstallerDriver,
    PostConfigDriver,
    SimulatedFirstRunDriver,
    SimulatedInstallerDriver,
    SimulatedPostConfigDriver,
    SimulatedSystemDriver,
    SystemDriver,
    create_simulated_bundle,
)
from remote.protocol import (
    Command,
    CommandAck,
    CommandType,
    IsoSpec,
    LogEntry,
    PostConfigStepState,
    SessionState,
    Stage,
    StageStatus,
    SystemStatus,
    TransferProgress,
    UpdateManifest,
    utc_now_iso,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPackageAndPyproject(unittest.TestCase):
    def test_remote_version_and_exports(self) -> None:
        self.assertEqual(remote.__version__, "0.2.0")
        self.assertEqual(remote.RUNTIME_VERSION, "1.0.0")
        for name in remote.__all__:
            self.assertTrue(hasattr(remote, name), f"Missing export: {name}")

    def test_pyproject_metadata(self) -> None:
        pyproject_path = REPO_ROOT / "pyproject.toml"
        data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
        project = data["project"]
        self.assertEqual(project["name"], "max-vm-auto")
        self.assertEqual(project["version"], "0.2.0")
        self.assertEqual(project["requires-python"], ">=3.10")

        deps = project["dependencies"]
        self.assertTrue(
            any("uiautomation" in d and "sys_platform" in d and "win32" in d for d in deps),
            f"Expected win32 marker on uiautomation in {deps}",
        )

        scripts = project["scripts"]
        self.assertEqual(scripts["max-auto"], "cli.main:cli")
        self.assertEqual(scripts["max-operator"], "remote.operator.main:main")
        self.assertEqual(scripts["max-client"], "remote.client.main:main")
        self.assertEqual(scripts["max-relay"], "remote.relay_server:main")

        dev_deps = project["optional-dependencies"]["dev"]
        self.assertTrue(any(d.startswith("pytest") for d in dev_deps))
        self.assertTrue(any(d.startswith("ruff") for d in dev_deps))

        find_cfg = data["tool"]["setuptools"]["packages"]["find"]
        self.assertEqual(set(find_cfg["include"]), {"cli*", "remote*", "packaging*"})
        pkg_data = data["tool"]["setuptools"]["package-data"]
        self.assertEqual(pkg_data["remote.operator"], ["static/*"])


class TestProtocolEnumsAndUtc(unittest.TestCase):
    def test_stage_enum(self) -> None:
        expected = [
            "PREP",
            "DOWNLOAD_ISO",
            "INSTALL_MAX",
            "WAIT_LICENSE",
            "FIRST_RUN",
            "POST_CONFIG",
            "DONE",
        ]
        self.assertEqual([s.value for s in Stage], expected)
        for member in Stage:
            self.assertIsInstance(member, str)
            self.assertEqual(str(member), member.value)

    def test_stage_status_enum(self) -> None:
        expected = [
            "idle",
            "running",
            "waiting_operator",
            "paused",
            "failed",
            "aborted",
            "completed",
        ]
        self.assertEqual([s.value for s in StageStatus], expected)
        for member in StageStatus:
            self.assertIsInstance(member, str)
            self.assertEqual(str(member), member.value)

    def test_command_type_enum(self) -> None:
        expected = [
            "refresh_status",
            "request_elevation",
            "smart_app_control",
            "defender_control",
            "start_install",
            "confirm_license",
            "trigger_first_run",
            "trigger_post_config",
            "skip_stage",
            "pause",
            "resume",
            "retry",
            "abort",
            "reset",
            "push_update",
            "capture_screenshot",
        ]
        self.assertEqual([c.value for c in CommandType], expected)
        self.assertEqual(len(CommandType), 16)
        for member in CommandType:
            self.assertIsInstance(member, str)
            self.assertEqual(str(member), member.value)

    def test_utc_now_iso(self) -> None:
        ts = utc_now_iso()
        self.assertTrue(ts.endswith("Z"))
        parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        self.assertEqual(parsed.tzinfo, timezone.utc)


class TestProtocolRoundTrip(unittest.TestCase):
    def _json_round_trip(self, obj, cls):
        raw = json.dumps(obj.to_dict())
        rebuilt = cls.from_dict(json.loads(raw))
        self.assertEqual(rebuilt, obj)
        return rebuilt

    def test_all_dataclasses_round_trip(self) -> None:
        sys_status = SystemStatus(
            hostname="CLIENT-01",
            os_version="Windows 11 Pro 24H2",
            is_admin=True,
            session_id=1,
            smart_app_control_state="evaluation",
            smart_app_control_reboot_required=False,
            defender_rtp_enabled=True,
            defender_tamper_protected=None,
            defender_exclusions=[r"C:\Install"],
            third_party_av=["ESET"],
            free_disk_bytes=50_000_000_000,
            installed_max_versions=["2025"],
            ahk_available=True,
            agent_version="0.2.0",
            runtime_version="1.0.0",
        )
        self._json_round_trip(sys_status, SystemStatus)

        iso = IsoSpec(
            file_id="iso-2025",
            filename="3dsMax2025.iso",
            version="2025",
            size_bytes=4096,
            sha256="deadbeef",
            download_url="https://cdn.example.com/3dsMax2025.iso",
        )
        self._json_round_trip(iso, IsoSpec)
        self._json_round_trip(
            IsoSpec("id2", "a.iso", "2026", 0, ""), IsoSpec
        )

        for phase in ("checking", "downloading", "verifying", "skipped", "done", "failed"):
            tp = TransferProgress(
                phase=phase,
                bytes_done=512,
                bytes_total=1024,
                percent=50,  # int coerced to float
                speed_bps=256,
                eta_seconds=2 if phase == "downloading" else None,
                local_path=r"C:\Install\a.iso",
            )
            rebuilt = self._json_round_trip(tp, TransferProgress)
            self.assertIsInstance(rebuilt.percent, float)
            self.assertIsInstance(rebuilt.speed_bps, float)

        for cat in ("renderer", "theme", "quad_menu", "hotkey", "scripts", "verify"):
            for st in ("pending", "running", "done", "skipped", "failed"):
                step = PostConfigStepState(f"s_{cat}_{st}", "Step", cat, st, "detail")
                self._json_round_trip(step, PostConfigStepState)

        session_minimal = SessionState(stage=Stage.PREP, status=StageStatus.IDLE)
        self._json_round_trip(session_minimal, SessionState)

        session_full = SessionState(
            session_id="S-ABCD",
            client_id="client-1",
            stage=Stage.POST_CONFIG,
            status=StageStatus.RUNNING,
            target_version="2025",
            iso_spec=iso,
            iso_local_path=r"C:\Install\3dsMax2025.iso",
            iso_skipped_download=True,
            transfer=TransferProgress(phase="skipped", percent=100.0),
            install_percent=100,
            install_message="Done",
            post_config_steps=[
                PostConfigStepState("renderer_corona", "Corona", "renderer", "done", "ok")
            ],
            summary={"ok": True, "nested": [1, 2, 3]},
            last_error=None,
            retry_count=2,
            updated_at=utc_now_iso(),
        )
        self._json_round_trip(session_full, SessionState)

        cmd = Command(
            cmd_id="cmd-1",
            seq=1,
            cmd_type=CommandType.START_INSTALL,
            payload={"version": "2025", "nested": {"k": "v"}},
        )
        self._json_round_trip(cmd, Command)
        cmd_from_str = Command(cmd_id="cmd-2", seq=2, cmd_type="pause")  # type: ignore[arg-type]
        self.assertEqual(cmd_from_str.cmd_type, CommandType.PAUSE)

        for ack_st in ("accepted", "completed", "rejected", "failed"):
            ack = CommandAck(cmd_id="cmd-1", status=ack_st, detail="ok", result={"a": 1})
            self._json_round_trip(ack, CommandAck)

        for lvl in ("DEBUG", "INFO", "WARN", "ERROR"):
            log = LogEntry(ts=utc_now_iso(), level=lvl, stage=Stage.INSTALL_MAX, msg="hello")  # type: ignore[arg-type]
            self.assertEqual(log.stage, "INSTALL_MAX")
            self._json_round_trip(log, LogEntry)

        manifest = UpdateManifest(
            version="0.2.1",
            min_runtime_version="1.0.0",
            file_id="upd-0.2.1",
            filename="app-0.2.1.zip",
            size_bytes=123456,
            sha256="b" * 64,
            notes="Bug fixes",
            download_url="https://updates.example.com/app-0.2.1.zip",
        )
        self._json_round_trip(manifest, UpdateManifest)

    def test_to_dict_non_aliasing(self) -> None:
        st = SystemStatus(
            hostname="H",
            os_version="Win",
            is_admin=True,
            session_id=1,
            defender_exclusions=["C:\\A"],
        )
        d = st.to_dict()
        d["defender_exclusions"].append("C:\\B")
        self.assertEqual(st.defender_exclusions, ["C:\\A"])

        cmd = Command(cmd_id="c1", seq=1, cmd_type=CommandType.PAUSE, payload={"a": [1]})
        cd = cmd.to_dict()
        cd["payload"]["a"].append(2)
        self.assertEqual(cmd.payload["a"], [1])


class TestProtocolValidation(unittest.TestCase):
    def test_non_mapping_inputs_rejected(self) -> None:
        classes = [
            SystemStatus,
            IsoSpec,
            TransferProgress,
            PostConfigStepState,
            SessionState,
            Command,
            CommandAck,
            LogEntry,
            UpdateManifest,
        ]
        for cls in classes:
            for bad in (None, [], "abc", 42):
                with self.subTest(cls=cls.__name__, bad=bad):
                    with self.assertRaises(ValueError):
                        cls.from_dict(bad)  # type: ignore[arg-type]

    def test_missing_required_fields_rejected(self) -> None:
        with self.assertRaises(ValueError):
            SessionState.from_dict({})
        with self.assertRaises(ValueError):
            SystemStatus.from_dict({"hostname": "H"})
        with self.assertRaises(ValueError):
            IsoSpec.from_dict({"file_id": "1"})
        with self.assertRaises(ValueError):
            TransferProgress.from_dict({})
        with self.assertRaises(ValueError):
            PostConfigStepState.from_dict({"step_id": "s1"})
        with self.assertRaises(ValueError):
            Command.from_dict({"cmd_id": "c1", "seq": 1})
        with self.assertRaises(ValueError):
            CommandAck.from_dict({"cmd_id": "c1"})
        with self.assertRaises(ValueError):
            LogEntry.from_dict({"ts": "t", "level": "INFO", "stage": "PREP"})
        with self.assertRaises(ValueError):
            UpdateManifest.from_dict({"version": "0.2.1"})

    def test_enum_and_choice_validation(self) -> None:
        for bad_stage in ("BOGUS", "prep", 123, None):
            with self.assertRaises(ValueError):
                SessionState(stage=bad_stage, status=StageStatus.IDLE)  # type: ignore[arg-type]
        for bad_status in ("IDLE", "unknown", 0):
            with self.assertRaises(ValueError):
                SessionState(stage=Stage.PREP, status=bad_status)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Command(cmd_id="c1", seq=1, cmd_type="invalid")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SystemStatus("H", "Win", True, 1, smart_app_control_state="ON")
        with self.assertRaises(ValueError):
            TransferProgress(phase="uploading")
        with self.assertRaises(ValueError):
            PostConfigStepState("s1", "N", "unknown_cat")
        with self.assertRaises(ValueError):
            PostConfigStepState("s1", "N", "theme", status="broken")
        with self.assertRaises(ValueError):
            CommandAck("c1", status="ok")
        with self.assertRaises(ValueError):
            LogEntry("2026-01-01T00:00:00Z", "TRACE", "PREP", "msg")

    def test_bool_int_and_float_quirks(self) -> None:
        # Bools rejected where int/float expected
        with self.assertRaises(ValueError):
            SystemStatus("H", "Win", True, True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SystemStatus("H", "Win", True, 1, free_disk_bytes=False)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            IsoSpec("1", "a.iso", "2025", True, "abc")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TransferProgress(phase="downloading", bytes_done=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TransferProgress(phase="downloading", percent=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TransferProgress(phase="downloading", speed_bps=False)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            TransferProgress(phase="downloading", eta_seconds=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SessionState(install_percent=True)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SessionState(retry_count=False)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Command("c1", True, CommandType.PAUSE)  # type: ignore[arg-type]

        # Int/str rejected where bool expected
        for bad_bool in (0, 1, "true"):
            with self.assertRaises(ValueError):
                SystemStatus("H", "Win", bad_bool, 1)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                SystemStatus("H", "Win", True, 1, defender_rtp_enabled=bad_bool)  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                SessionState(iso_skipped_download=bad_bool)  # type: ignore[arg-type]

        # Bounds and non-finite floats
        for bad_pct in (-0.1, 100.1, float("nan"), float("inf"), float("-inf")):
            with self.assertRaises(ValueError):
                TransferProgress(phase="downloading", percent=bad_pct)
        for bad_spd in (-1.0, float("inf"), float("nan")):
            with self.assertRaises(ValueError):
                TransferProgress(phase="downloading", speed_bps=bad_spd)
        for bad_eta in (-0.5, float("nan"), float("inf")):
            with self.assertRaises(ValueError):
                TransferProgress(phase="downloading", eta_seconds=bad_eta)
        for bad_inst in (-1, 101):
            with self.assertRaises(ValueError):
                SessionState(install_percent=bad_inst)

    def test_nested_and_collection_validation(self) -> None:
        with self.assertRaises(ValueError):
            SystemStatus("H", "Win", True, 1, defender_exclusions=[123])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            SessionState(iso_spec="bad")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SessionState(transfer=123)  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            SessionState(post_config_steps=["bad"])  # type: ignore[list-item]
        with self.assertRaises(ValueError):
            SessionState(summary="not-a-dict")  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            Command("c1", 1, CommandType.PAUSE, payload=[])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            CommandAck("c1", "accepted", result="not-a-dict")  # type: ignore[arg-type]


class TestAuthFunctions(unittest.TestCase):
    def test_generate_session_id(self) -> None:
        ids = {generate_session_id() for _ in range(50)}
        self.assertGreater(len(ids), 40)
        for sid in ids:
            self.assertRegex(sid, r"^S-[A-Z0-9]{4}$")
        self.assertRegex(generate_session_id(8), r"^S-[A-Z0-9]{8}$")
        for bad_len in (0, 2, 3, True, "4"):
            with self.assertRaises(ValueError):
                generate_session_id(bad_len)  # type: ignore[arg-type]

    def test_generate_token(self) -> None:
        tokens = {generate_token() for _ in range(50)}
        self.assertEqual(len(tokens), 50)
        for tok in tokens:
            self.assertGreaterEqual(len(tok), 32)
            self.assertRegex(tok, r"^[A-Za-z0-9_-]+$")
        for bad_n in (0, 8, 15, True, "32"):
            with self.assertRaises(ValueError):
                generate_token(bad_n)  # type: ignore[arg-type]

    def test_verify_token(self) -> None:
        tok = generate_token()
        self.assertTrue(verify_token(tok, tok))
        self.assertFalse(verify_token(tok, tok[:-1] + ("A" if tok[-1] != "A" else "B")))
        self.assertFalse(verify_token(tok, tok + "x"))
        self.assertFalse(verify_token("AbcDef", "abcdef"))
        self.assertFalse(verify_token("", ""))
        self.assertFalse(verify_token("   ", "   "))
        for bad in (None, "", "   ", 123, b"tok", []):
            self.assertFalse(verify_token(tok, bad))  # type: ignore[arg-type]
            self.assertFalse(verify_token(bad, tok))  # type: ignore[arg-type]
        # Non-ASCII safe (no TypeError)
        self.assertTrue(verify_token("секрет-токен", "секрет-токен"))
        self.assertFalse(verify_token("секрет-токен", "інший-токен"))

    def test_extract_bearer_token(self) -> None:
        self.assertEqual(extract_bearer_token("Bearer my_token_123"), "my_token_123")
        self.assertEqual(extract_bearer_token("bearer my_token_123"), "my_token_123")
        self.assertIsNone(extract_bearer_token(None))
        self.assertIsNone(extract_bearer_token("Basic abc"))
        self.assertIsNone(extract_bearer_token("Bearer "))
        self.assertIsNone(extract_bearer_token("Bearer two words"))


class TestPairingConfig(unittest.TestCase):
    def test_valid_configs_and_round_trips(self) -> None:
        configs = [
            PairingConfig("direct", "http://192.168.1.50:8765/", "S-4K9P", generate_token()),
            PairingConfig("relay", "https://relay.example.com/max/", "S-9Z2M", "tok+with/chars=_-"),
            PairingConfig("direct", "http://[::1]:8765", "S-IPV6", "secret_token_value"),
        ]
        self.assertEqual(configs[0].server_url, "http://192.168.1.50:8765")
        self.assertEqual(configs[1].server_url, "https://relay.example.com/max")

        for cfg in configs:
            raw_json = cfg.to_connect_json()
            self.assertEqual(PairingConfig.from_connect_json(raw_json), cfg)

            uri = cfg.to_pairing_uri()
            self.assertTrue(uri.startswith(f"maxpair://{cfg.mode}?"))
            self.assertEqual(PairingConfig.from_pairing_uri(uri), cfg)

        # Verify special characters are percent-encoded in URI
        uri_special = configs[1].to_pairing_uri()
        self.assertIn("%2B", uri_special)
        self.assertIn("%2F", uri_special)
        self.assertIn("%3D", uri_special)

    def test_invalid_fields_rejected(self) -> None:
        for bad_mode in ("ws", "DIRECT", "", None, 123):
            with self.assertRaises(ValueError):
                PairingConfig(bad_mode, "http://127.0.0.1:8765", "S-1234", "tok")  # type: ignore[arg-type]

        bad_urls = [
            "",
            "ftp://host:8765",
            "ws://host:8765",
            "javascript:alert(1)",
            "http://",
            "http:///path",
            "http://:8765",
            "http://host:99999",
            "http://host:abc",
            "http://user:pass@host:8765",
            "http://host:8765?a=1",
            "http://host:8765#frag",
            " http://host:8765",
            "http://host:8765/a b",
            "http:127.0.0.1:8765",
        ]
        for bad_url in bad_urls:
            with self.subTest(bad_url=bad_url):
                with self.assertRaises(ValueError):
                    PairingConfig("direct", bad_url, "S-1234", "tok")

        for bad_val in ("", "   ", "has space", "has\ttab", None):
            with self.assertRaises(ValueError):
                PairingConfig("direct", "http://127.0.0.1:8765", bad_val, "tok")  # type: ignore[arg-type]
            with self.assertRaises(ValueError):
                PairingConfig("direct", "http://127.0.0.1:8765", "S-1234", bad_val)  # type: ignore[arg-type]

    def test_malformed_connect_json_and_pairing_uri_rejected(self) -> None:
        for bad_json in (
            None,
            "{not json",
            "[]",
            "null",
            "123",
            json.dumps({"mode": "direct"}),
            json.dumps({"mode": "direct", "server_url": 123, "session_id": "S-1", "token": "t"}),
        ):
            with self.subTest(bad_json=bad_json):
                with self.assertRaises(ValueError):
                    PairingConfig.from_connect_json(bad_json)  # type: ignore[arg-type]

        bad_uris = [
            None,
            "",
            " maxpair://direct?server_url=http%3A%2F%2F127.0.0.1&session_id=S-1&token=t",
            "maxpair://direct?server_url=http%3A%2F%2F127.0.0.1&session_id=S-1&token=t ",
            "http://direct?server_url=http%3A%2F%2F127.0.0.1&session_id=S-1&token=t",
            "maxpair:direct?server_url=http%3A%2F%2F127.0.0.1&session_id=S-1&token=t",
            "maxpair://unknown?server_url=http%3A%2F%2F127.0.0.1&session_id=S-1&token=t",
            "maxpair://direct/extra?server_url=http%3A%2F%2F127.0.0.1&session_id=S-1&token=t",
            "maxpair://direct?server_url=http%3A%2F%2F127.0.0.1&session_id=S-1&token=t#frag",
            "maxpair://direct",
            "maxpair://direct?server_url=http%3A%2F%2F127.0.0.1&server_url=http%3A%2F%2F127.0.0.2&session_id=S-1&token=t",
            "maxpair://direct?server_url=http%3A%2F%2F127.0.0.1&session_id=S-1&token=t&extra=1",
            "maxpair://direct?malformed_no_equals",
            "maxpair://direct?server_url=http%3A%2F%2F127.0.0.1&session_id=S-1",
            "maxpair://direct?server_url=http%3A%2F%2F127.0.0.1&session_id=S-1&token=",
        ]
        for bad_uri in bad_uris:
            with self.subTest(bad_uri=bad_uri):
                with self.assertRaises(ValueError):
                    PairingConfig.from_pairing_uri(bad_uri)  # type: ignore[arg-type]


class TestDriverProtocolsAndSimulators(unittest.TestCase):
    def test_protocols_runtime_checkable_and_bundle_types(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = create_simulated_bundle(work_dir=tmp)
            self.assertIsInstance(bundle, DriverBundle)
            self.assertIsInstance(bundle.system, SystemDriver)
            self.assertIsInstance(bundle.installer, InstallerDriver)
            self.assertIsInstance(bundle.first_run, FirstRunDriver)
            self.assertIsInstance(bundle.post_config, PostConfigDriver)

            # Standalone instantiation without bundle
            self.assertIsInstance(SimulatedSystemDriver(), SystemDriver)
            self.assertIsInstance(SimulatedInstallerDriver(), InstallerDriver)
            self.assertIsInstance(SimulatedFirstRunDriver(), FirstRunDriver)
            self.assertIsInstance(SimulatedPostConfigDriver(), PostConfigDriver)

    def test_invalid_simulated_bundle_flags_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                create_simulated_bundle(work_dir=tmp, smart_app_control_state="bogus")
            with self.assertRaises(ValueError):
                create_simulated_bundle(work_dir=tmp, first_run_outcome="bogus")
            with self.assertRaises(ValueError):
                create_simulated_bundle(work_dir=tmp, step_delay_sec=-0.1)

    def test_system_collect_status_and_elevation_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = create_simulated_bundle(
                work_dir=tmp,
                initial_admin=False,
                initial_exclusions=[r"C:\PreExisting"],
            )
            st1 = bundle.system.collect_status("0.2.0", "1.0.0")
            self.assertFalse(st1.is_admin)
            self.assertEqual(st1.agent_version, "0.2.0")
            self.assertEqual(st1.runtime_version, "1.0.0")

            # Mutating returned lists does not affect internal state
            st1.defender_exclusions.append(r"C:\Mutated")
            self.assertEqual(
                bundle.system.collect_status("0.2.0", "1.0.0").defender_exclusions,
                [r"C:\PreExisting"],
            )

            elev1 = bundle.system.request_elevation("python.exe", ["-m", "remote"], tmp)
            self.assertTrue(elev1["ok"])
            self.assertTrue(elev1["elevated"])
            self.assertFalse(elev1["already_admin"])
            self.assertTrue(bundle.system.collect_status("0.2.0", "1.0.0").is_admin)

            elev2 = bundle.system.request_elevation("python.exe", [], tmp)
            self.assertTrue(elev2["already_admin"])

    def test_smart_app_control_guided_and_scripted_modes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # evaluation + guided -> off, reboot_required=False
            b1 = create_simulated_bundle(work_dir=tmp, smart_app_control_state="evaluation")
            res1 = b1.system.disable_smart_app_control("guided")
            self.assertTrue(res1["ok"])
            self.assertTrue(res1["changed"])
            self.assertEqual(res1["state"], "off")
            self.assertFalse(res1["reboot_required"])
            self.assertEqual(b1.system.get_smart_app_control_status()["state"], "off")
            self.assertEqual(
                b1.system.collect_status("0.2.0", "1.0.0").smart_app_control_state, "off"
            )
            # already off -> changed=False
            res_off = b1.system.disable_smart_app_control("guided")
            self.assertTrue(res_off["ok"])
            self.assertFalse(res_off["changed"])

            # evaluation + scripted (admin) -> off, reboot_required=True
            b2 = create_simulated_bundle(
                work_dir=tmp, initial_admin=True, smart_app_control_state="evaluation"
            )
            res2 = b2.system.disable_smart_app_control("scripted")
            self.assertTrue(res2["ok"])
            self.assertTrue(res2["changed"])
            self.assertEqual(res2["state"], "off")
            self.assertTrue(res2["reboot_required"])

            # enforced + scripted (admin) -> ok=False, stays enforced, reboot_required=True
            b3 = create_simulated_bundle(
                work_dir=tmp, initial_admin=True, smart_app_control_state="enforced"
            )
            res3 = b3.system.disable_smart_app_control("scripted")
            self.assertFalse(res3["ok"])
            self.assertEqual(res3["state"], "enforced")
            self.assertTrue(res3["reboot_required"])
            self.assertEqual(res3["guided_uri"], "windowsdefender://smartapp/")
            # enforced + guided -> ok=True, off
            res3g = b3.system.disable_smart_app_control("guided")
            self.assertTrue(res3g["ok"])
            self.assertEqual(res3g["state"], "off")

            # scripted without admin -> ok=False
            b4 = create_simulated_bundle(
                work_dir=tmp, initial_admin=False, smart_app_control_state="evaluation"
            )
            res4 = b4.system.disable_smart_app_control("scripted")
            self.assertFalse(res4["ok"])
            self.assertEqual(res4["state"], "evaluation")

            # unsupported mode -> ok=False
            res_bad = b4.system.disable_smart_app_control("invalid")
            self.assertFalse(res_bad["ok"])

    def test_defender_normal_lifecycle_and_exclusion_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = create_simulated_bundle(
                work_dir=tmp,
                initial_admin=True,
                defender_tamper_protected=False,
                defender_rtp_enabled=True,
                initial_exclusions=[r"C:\Existing"],
            )
            add_res = bundle.system.configure_defender(
                "add_exclusions", [r"C:\Install", r"C:\Install", r"C:\Existing", "  "]
            )
            self.assertTrue(add_res["ok"])
            self.assertEqual(add_res["added"], [r"C:\Install"])
            self.assertEqual(add_res["exclusions"], [r"C:\Existing", r"C:\Install"])

            dis_res = bundle.system.configure_defender("disable")
            self.assertTrue(dis_res["ok"])
            self.assertFalse(dis_res["rtp_enabled"])
            self.assertFalse(bundle.system.collect_status("0.2.0", "1.0.0").defender_rtp_enabled)

            open_res = bundle.system.configure_defender("open_settings")
            self.assertTrue(open_res["ok"])
            self.assertEqual(open_res["guided_uri"], "windowsdefender://threatsettings/")

            rst_res = bundle.system.configure_defender("restore")
            self.assertTrue(rst_res["ok"])
            self.assertTrue(rst_res["rtp_enabled"])
            self.assertEqual(rst_res["removed_exclusions"], [r"C:\Install"])
            self.assertEqual(rst_res["exclusions"], [r"C:\Existing"])

    def test_defender_tamper_protected_enforces_guided_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = create_simulated_bundle(
                work_dir=tmp,
                initial_admin=True,
                defender_tamper_protected=True,
                defender_rtp_enabled=True,
            )
            res = bundle.system.configure_defender(
                "disable", exclusion_paths=[r"C:\Install"]
            )
            self.assertFalse(res["ok"])
            self.assertTrue(res["tamper_protected"])
            self.assertTrue(res["rtp_enabled"])
            self.assertTrue(res["guided_fallback"])
            self.assertEqual(res["guided_uri"], "windowsdefender://threatsettings/")
            self.assertEqual(res["added_exclusions"], [r"C:\Install"])
            st = bundle.system.collect_status("0.2.0", "1.0.0")
            self.assertTrue(st.defender_rtp_enabled)
            self.assertIn(r"C:\Install", st.defender_exclusions)

    def test_capture_screenshot_creates_valid_minimal_png(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = create_simulated_bundle(work_dir=tmp)
            dest = Path(tmp) / "nested" / "shots" / "cap.png"
            out = bundle.system.capture_screenshot(dest)
            self.assertEqual(out, dest)
            self.assertTrue(dest.is_file())
            raw = dest.read_bytes()
            self.assertTrue(raw.startswith(b"\x89PNG\r\n\x1a\n"))
            self.assertIn(b"IHDR", raw)
            self.assertIn(b"IDAT", raw)
            self.assertIn(b"IEND", raw)
            try:
                from PIL import Image
            except ImportError:
                Image = None  # type: ignore[assignment]
            if Image is not None:
                with Image.open(dest) as img:
                    img.verify()

    def test_installer_success_shared_state_and_already_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = create_simulated_bundle(work_dir=tmp, step_delay_sec=0.0)
            iso = Path(tmp) / "3dsMax2025.iso"
            iso.write_bytes(b"ISO-DATA")

            progress_events: list[tuple[int, str]] = []
            logs: list[tuple[str, str]] = []
            res = bundle.installer.install_3dsmax(
                "2025",
                iso,
                progress_cb=lambda p, m: progress_events.append((p, m)),
                log_cb=lambda lvl, m: logs.append((lvl, m)),
                cancel_event=threading.Event(),
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "completed")
            self.assertEqual(res["exit_code"], 0)
            self.assertFalse(res["already_installed"])
            self.assertTrue(bundle.installer.is_max_installed("2025"))
            self.assertFalse(bundle.installer.is_installer_running())
            self.assertIn(
                "2025", bundle.system.collect_status("0.2.0", "1.0.0").installed_max_versions
            )
            percents = [p for p, _ in progress_events]
            self.assertEqual(percents, sorted(percents))
            self.assertEqual(percents[-1], 100)
            self.assertTrue(len(logs) >= 5)

            # Second call short-circuits as already_installed
            res2 = bundle.installer.install_3dsmax(
                "2025",
                iso,
                progress_cb=lambda p, m: None,
                log_cb=lambda lvl, m: None,
                cancel_event=threading.Event(),
            )
            self.assertTrue(res2["ok"])
            self.assertTrue(res2["already_installed"])

    def test_installer_missing_iso_and_fail_install_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = create_simulated_bundle(work_dir=tmp, step_delay_sec=0.0)
            missing = Path(tmp) / "does_not_exist.iso"
            res = bundle.installer.install_3dsmax(
                "2025",
                missing,
                progress_cb=lambda p, m: None,
                log_cb=lambda lvl, m: None,
                cancel_event=threading.Event(),
            )
            self.assertFalse(res["ok"])
            self.assertEqual(res["status"], "failed")
            self.assertFalse(bundle.installer.is_max_installed("2025"))

            iso = Path(tmp) / "3dsMax2025.iso"
            iso.write_bytes(b"ISO-DATA")
            b_fail = create_simulated_bundle(
                work_dir=tmp, fail_install=True, step_delay_sec=0.0
            )
            res_f = b_fail.installer.install_3dsmax(
                "2025",
                iso,
                progress_cb=lambda p, m: None,
                log_cb=lambda lvl, m: None,
                cancel_event=threading.Event(),
            )
            self.assertFalse(res_f["ok"])
            self.assertEqual(res_f["status"], "failed")
            self.assertEqual(res_f["exit_code"], 1)
            self.assertFalse(b_fail.installer.is_max_installed("2025"))
            self.assertFalse(b_fail.installer.is_installer_running())

    def test_installer_cancel_event_aborts_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            iso = Path(tmp) / "3dsMax2025.iso"
            iso.write_bytes(b"ISO-DATA")
            bundle = create_simulated_bundle(work_dir=tmp, step_delay_sec=2.0)

            pre_cancel = threading.Event()
            pre_cancel.set()
            res_pre = bundle.installer.install_3dsmax(
                "2025",
                iso,
                progress_cb=lambda p, m: None,
                log_cb=lambda lvl, m: None,
                cancel_event=pre_cancel,
            )
            self.assertEqual(res_pre["status"], "aborted")

            mid_cancel = threading.Event()
            timer = threading.Timer(0.05, mid_cancel.set)
            t0 = time.monotonic()
            timer.start()
            try:
                res_mid = bundle.installer.install_3dsmax(
                    "2025",
                    iso,
                    progress_cb=lambda p, m: None,
                    log_cb=lambda lvl, m: None,
                    cancel_event=mid_cancel,
                )
            finally:
                timer.cancel()
            elapsed = time.monotonic() - t0
            self.assertEqual(res_mid["status"], "aborted")
            self.assertLess(elapsed, 0.5)
            self.assertFalse(bundle.installer.is_max_installed("2025"))
            self.assertFalse(bundle.installer.is_installer_running())

    def test_first_run_all_four_outcomes_and_screenshots(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dump_dir = Path(tmp) / "dumps"
            bundle = create_simulated_bundle(
                work_dir=tmp, first_run_outcome="ready", step_delay_sec=0.0
            )
            res25 = bundle.first_run.run_first_run(
                "2025",
                log_cb=lambda lvl, m: None,
                cancel_event=threading.Event(),
                dump_dir=dump_dir,
            )
            self.assertEqual(
                set(res25.keys()), {"status", "detail", "matched_rules", "screenshots"}
            )
            self.assertEqual(res25["status"], "ready")
            self.assertIn("Privacy / Data Collection Consent", res25["matched_rules"])
            self.assertEqual(res25["screenshots"], [])

            res26 = bundle.first_run.run_first_run(
                "2026",
                log_cb=lambda lvl, m: None,
                cancel_event=threading.Event(),
                dump_dir=dump_dir,
            )
            self.assertIn("Privacy Settings (3ds Max 2026+ WebView)", res26["matched_rules"])

            for outcome in ("licensing_required", "timeout", "failed"):
                with self.subTest(outcome=outcome):
                    assert isinstance(bundle.first_run, SimulatedFirstRunDriver)
                    bundle.first_run.first_run_outcome = outcome
                    res = bundle.first_run.run_first_run(
                        "2025",
                        log_cb=lambda lvl, m: None,
                        cancel_event=threading.Event(),
                        dump_dir=dump_dir,
                    )
                    self.assertEqual(res["status"], outcome)
                    self.assertEqual(len(res["screenshots"]), 1)
                    shot = Path(res["screenshots"][0])
                    self.assertTrue(shot.is_file())
                    self.assertTrue(shot.read_bytes().startswith(b"\x89PNG\r\n\x1a\n"))

            # Switch back to ready to simulate post-licensing retry
            bundle.first_run.first_run_outcome = "ready"
            res_retry = bundle.first_run.run_first_run(
                "2025",
                log_cb=lambda lvl, m: None,
                cancel_event=threading.Event(),
            )
            self.assertEqual(res_retry["status"], "ready")

    def test_first_run_cancel_event_aborts_promptly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = create_simulated_bundle(work_dir=tmp, step_delay_sec=2.0)
            cancel = threading.Event()
            timer = threading.Timer(0.05, cancel.set)
            t0 = time.monotonic()
            timer.start()
            try:
                res = bundle.first_run.run_first_run(
                    "2025",
                    log_cb=lambda lvl, m: None,
                    cancel_event=cancel,
                )
            finally:
                timer.cancel()
            elapsed = time.monotonic() - t0
            self.assertEqual(res["status"], "aborted")
            self.assertLess(elapsed, 0.5)

    def test_post_config_full_pipeline_version_aware_enu_and_verify_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            bundle = create_simulated_bundle(work_dir=work, step_delay_sec=0.0)
            iso = Path(tmp) / "3dsMax2025.iso"
            iso.write_bytes(b"ISO")
            bundle.installer.install_3dsmax(
                "2025",
                iso,
                progress_cb=lambda p, m: None,
                log_cb=lambda lvl, m: None,
                cancel_event=threading.Event(),
            )

            steps = bundle.post_config.load_steps(version="2025")
            categories = {s.category for s in steps}
            self.assertEqual(
                categories, {"renderer", "theme", "quad_menu", "hotkey", "scripts", "verify"}
            )
            step_ids = [s.step_id for s in steps]
            self.assertIn("renderer_corona", step_ids)
            self.assertIn("renderer_vray", step_ids)

            for step in steps:
                res = bundle.post_config.run_step(
                    step.step_id,
                    "2025",
                    assets_dir=None,
                    log_cb=lambda lvl, m: None,
                    cancel_event=threading.Event(),
                )
                self.assertTrue(res["ok"], f"Step {step.step_id} failed: {res}")
                self.assertEqual(res["status"], "done")

            enu25 = work / "Autodesk" / "3dsMax" / "2025 - 64bit" / "ENU"
            backup25 = work / "Autodesk" / "3dsMax" / "2025 - 64bit" / "ENU_backup"
            plugins25 = work / "ProgramFiles" / "Autodesk" / "3ds Max 2025" / "plugins"
            self.assertTrue(enu25.is_dir())
            self.assertTrue(backup25.is_dir())
            self.assertTrue((enu25 / "en-US" / "UI" / "custom_theme.clrx").is_file())
            self.assertTrue((enu25 / "en-US" / "UI" / "custom_toolbar.cuix").is_file())
            self.assertTrue(
                (enu25 / "en-US" / "UI" / "UserSettings" / "custom_quads.mnx").is_file()
            )
            self.assertFalse((enu25 / "en-US" / "UI" / "custom_quads.mnux").exists())
            self.assertTrue(
                (enu25 / "en-US" / "UI" / "UserSettings" / "custom_hotkeys.hsx").is_file()
            )
            self.assertTrue((enu25 / "scripts" / "startup" / "verify_install.ms").is_file())
            self.assertTrue((enu25 / "usermacros" / "custom_tools.mcr").is_file())
            self.assertTrue((plugins25 / "CoronaMaxUtils.dlu").is_file())
            self.assertTrue((plugins25 / "vrender.dlr").is_file())

            verify_json = work / "logs" / "max_verify.json"
            self.assertTrue(verify_json.is_file())
            report = json.loads(verify_json.read_text("utf-8"))
            self.assertTrue(report["ok"])
            self.assertEqual(report["version"], "2025")
            self.assertIn("Chaos Corona", report["renderers"])
            self.assertIn("V-Ray", report["renderers"])
            self.assertTrue(all(report["checks"].values()))

            # 2024 uses legacy .mnux under ENU/en-US/UI/
            res24 = bundle.post_config.run_step(
                "ui_quad_menu",
                "2024",
                assets_dir=None,
                log_cb=lambda lvl, m: None,
                cancel_event=threading.Event(),
            )
            self.assertTrue(res24["ok"])
            enu24 = work / "Autodesk" / "3dsMax" / "2024 - 64bit" / "ENU"
            self.assertTrue((enu24 / "en-US" / "UI" / "custom_quads.mnux").is_file())

    def test_post_config_assets_dir_zip_slip_failure_and_cancel(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp) / "work"
            assets = Path(tmp) / "assets"
            assets.mkdir()
            (assets / "studio.clrx").write_text("<ColorScheme/>", encoding="utf-8")
            (assets / "studio.mnx").write_text("<Quad/>", encoding="utf-8")
            (assets / "tool.ms").write_text("-- script", encoding="utf-8")
            (assets / "tool.mcr").write_text("-- macro", encoding="utf-8")

            good_mzp = assets / "good_pkg.mzp"
            with zipfile.ZipFile(good_mzp, "w") as zf:
                zf.writestr("mzp.run", "name \"GoodPkg\"")
                zf.writestr("sub/helper.ms", "-- helper")

            bundle = create_simulated_bundle(
                work_dir=work,
                fail_post_config_steps={"renderer_vray"},
                step_delay_sec=0.0,
            )

            # Custom assets copied & safe .mzp extracted
            res_theme = bundle.post_config.run_step(
                "ui_theme", "2025", assets_dir=assets, log_cb=lambda l, m: None, cancel_event=threading.Event()
            )
            self.assertTrue(res_theme["ok"])
            enu25 = work / "Autodesk" / "3dsMax" / "2025 - 64bit" / "ENU"
            self.assertTrue((enu25 / "en-US" / "UI" / "studio.clrx").is_file())

            res_scripts = bundle.post_config.run_step(
                "scripts_deploy", "2025", assets_dir=assets, log_cb=lambda l, m: None, cancel_event=threading.Event()
            )
            self.assertTrue(res_scripts["ok"])
            self.assertTrue((enu25 / "scripts" / "startup" / "tool.ms").is_file())
            self.assertTrue((enu25 / "usermacros" / "tool.mcr").is_file())
            self.assertTrue(
                (enu25 / "scripts" / "startup" / "good_pkg" / "sub" / "helper.ms").is_file()
            )

            # Malicious Zip-Slip .mzp rejected without writing outside ENU
            bad_assets = Path(tmp) / "bad_assets"
            bad_assets.mkdir()
            evil_mzp = bad_assets / "evil.mzp"
            with zipfile.ZipFile(evil_mzp, "w") as zf:
                zf.writestr("../../../../escape.ms", "pwned")
            res_evil = bundle.post_config.run_step(
                "scripts_deploy", "2025", assets_dir=bad_assets, log_cb=lambda l, m: None, cancel_event=threading.Event()
            )
            self.assertFalse(res_evil["ok"])
            self.assertEqual(res_evil["status"], "failed")
            self.assertFalse((work / "escape.ms").exists())
            self.assertFalse((Path(tmp) / "escape.ms").exists())

            # Simulated step failure & unknown step
            res_fail = bundle.post_config.run_step(
                "renderer_vray", "2025", assets_dir=None, log_cb=lambda l, m: None, cancel_event=threading.Event()
            )
            self.assertFalse(res_fail["ok"])
            self.assertEqual(res_fail["status"], "failed")

            res_unk = bundle.post_config.run_step(
                "nonexistent_step", "2025", assets_dir=None, log_cb=lambda l, m: None, cancel_event=threading.Event()
            )
            self.assertFalse(res_unk["ok"])
            self.assertEqual(res_unk["status"], "failed")

            # Prompt cancellation
            slow_bundle = create_simulated_bundle(work_dir=work, step_delay_sec=2.0)
            cancel = threading.Event()
            timer = threading.Timer(0.05, cancel.set)
            t0 = time.monotonic()
            timer.start()
            try:
                res_abort = slow_bundle.post_config.run_step(
                    "ui_theme", "2025", assets_dir=None, log_cb=lambda l, m: None, cancel_event=cancel
                )
            finally:
                timer.cancel()
            self.assertEqual(res_abort["status"], "aborted")
            self.assertLess(time.monotonic() - t0, 0.5)


if __name__ == "__main__":
    unittest.main()
