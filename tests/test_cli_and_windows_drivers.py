"""Unit tests for M3 CLI upgrades and Windows platform drivers (Linux-safe)."""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from cli.ahk_bridge import AHKBridge
from cli.main import cli
from cli.max_deployer import (
    find_all_installed_3dsmax,
    get_installed_3dsmax_exe,
    get_max_user_enu_dir,
    launch_3dsmax,
)
from cli.ui_inspector import WindowSnapshot
from cli.watcher import WatchResult, WatcherConfig, watch_loop
from remote.drivers.base import (
    DriverBundle,
    FirstRunDriver,
    InstallerDriver,
    PostConfigDriver,
    SystemDriver,
)
from remote.drivers.windows import (
    DEFENDER_THREAT_URI,
    SAC_GUIDED_URI,
    WindowsFirstRunDriver,
    WindowsInstallerDriver,
    WindowsPostConfigDriver,
    WindowsSystemDriver,
    create_windows_bundle,
)

REPO_ROOT = Path(__file__).resolve().parent.parent


class _FakeRegOps:
    def __init__(self, dwords=None, strs=None, deny_write=False):
        self.dwords = dict(dwords or {})
        self.strs = dict(strs or {})
        self.deny_write = deny_write

    def read_dword(self, hive, subkey, name):
        return self.dwords.get((hive, subkey, name))

    def write_dword(self, hive, subkey, name, value):
        if self.deny_write:
            raise PermissionError("Kernel CI protection")
        self.dwords[(hive, subkey, name)] = int(value)

    def read_str(self, hive, subkey, name):
        return self.strs.get((hive, subkey, name))

    def subkey_exists(self, hive, subkey):
        return any(k[0] == hive and k[1] == subkey for k in self.dwords)


class _FakeWin32Ops:
    def __init__(
        self,
        *,
        is_admin: bool = False,
        session_id: int = 1,
        msgbox_result: int = 1,
        shell_result: int = 42,
    ) -> None:
        self.is_admin = is_admin
        self.session_id = session_id
        self.msgbox_result = msgbox_result
        self.shell_result = shell_result
        self.shell_calls: list[tuple[str, str, str, str, int]] = []
        self.msgbox_calls: list[tuple[str, str, int]] = []

    def is_user_an_admin(self) -> bool:
        return self.is_admin

    def get_session_id(self) -> int:
        return self.session_id

    def message_box(self, title: str, text: str, flags: int = 0) -> int:
        self.msgbox_calls.append((title, text, flags))
        return self.msgbox_result

    def shell_execute(self, verb: str, file: str, params: str = "", cwd: str = "", show_cmd: int = 1) -> int:
        self.shell_calls.append((verb, file, params, cwd, show_cmd))
        return self.shell_result

    def capture_screen_png(self, dest_path: Path) -> Path | None:
        dest_path.parent.mkdir(parents=True, exist_ok=True)
        dest_path.write_bytes(b"\x89PNG\r\n\x1a\nFAKE")
        return dest_path


class TestWindowsDriverProtocols(unittest.TestCase):
    def test_bundle_conforms_to_protocols(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            bundle = create_windows_bundle(data_dir=Path(tmp))
            self.assertIsInstance(bundle, DriverBundle)
            self.assertIsInstance(bundle.system, SystemDriver)
            self.assertIsInstance(bundle.installer, InstallerDriver)
            self.assertIsInstance(bundle.first_run, FirstRunDriver)
            self.assertIsInstance(bundle.post_config, PostConfigDriver)


class TestWindowsSystemDriver(unittest.TestCase):
    def test_elevation_guards_and_runas(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            # Session 0 rejected
            drv0 = WindowsSystemDriver(
                data_dir=Path(tmp),
                win32_ops=_FakeWin32Ops(is_admin=False, session_id=0),
            )
            res0 = drv0.request_elevation("python.exe", ["-m", "agent"], tmp)
            self.assertFalse(res0["ok"])
            self.assertEqual(res0["session_id"], 0)

            # Already admin
            drv_admin = WindowsSystemDriver(
                data_dir=Path(tmp),
                win32_ops=_FakeWin32Ops(is_admin=True, session_id=1),
            )
            res_admin = drv_admin.request_elevation("python.exe", ["-m", "agent"], tmp)
            self.assertTrue(res_admin["ok"])
            self.assertTrue(res_admin["already_admin"])

            # Cancelled at pre-UAC prompt
            drv_cancel = WindowsSystemDriver(
                data_dir=Path(tmp),
                win32_ops=_FakeWin32Ops(is_admin=False, session_id=1, msgbox_result=2),
            )
            res_cancel = drv_cancel.request_elevation("python.exe", ["-m", "agent"], tmp)
            self.assertFalse(res_cancel["ok"])
            self.assertTrue(res_cancel.get("cancelled"))

            # Successful ShellExecuteW(runas)
            w32 = _FakeWin32Ops(is_admin=False, session_id=1, msgbox_result=1, shell_result=42)
            drv_ok = WindowsSystemDriver(data_dir=Path(tmp), win32_ops=w32)
            res_ok = drv_ok.request_elevation("python.exe", ["-m", "agent"], tmp)
            self.assertTrue(res_ok["ok"])
            self.assertEqual(res_ok["method"], "ShellExecuteW_runas")
            self.assertEqual(w32.shell_calls[0][0], "runas")

    def test_smart_app_control_status_and_disable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reg = _FakeRegOps(
                dwords={
                    ("HKLM", r"SYSTEM\CurrentControlSet\Control\CI\Policy", "VerifiedAndReputablePolicyState"): 1
                }
            )
            w32 = _FakeWin32Ops(is_admin=True, session_id=1, shell_result=42)

            def fake_run(cmd, **_kwargs):
                if cmd[0] == "CiTool.exe" and "-lp" in cmd:
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps({"Policies": [{"FriendlyName": "Smart App Control", "IsEnforced": True}]}),
                        stderr="",
                    )
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            drv = WindowsSystemDriver(data_dir=Path(tmp), win32_ops=w32, reg_ops=reg, run_cmd=fake_run)
            st = drv.get_smart_app_control_status()
            self.assertEqual(st["state"], "enforced")
            self.assertTrue(st["ci_enforced"])

            guided = drv.disable_smart_app_control(mode="guided")
            self.assertTrue(guided["ok"])
            self.assertEqual(guided["guided_uri"], SAC_GUIDED_URI)
            self.assertEqual(w32.shell_calls[-1][1], SAC_GUIDED_URI)

            scripted = drv.disable_smart_app_control(mode="scripted")
            self.assertTrue(scripted["ok"])
            self.assertTrue(scripted["changed"])
            self.assertTrue(scripted["reboot_required"])
            self.assertEqual(scripted["state"], "off")

    def test_defender_tamper_protection_and_exclusion_ledger(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tamper_state = {"tamper": True, "rtp": True, "exclusions": []}

            def fake_run(cmd, **_kwargs):
                ps_cmd = cmd[-1] if len(cmd) >= 2 else ""
                if "Get-MpComputerStatus" in ps_cmd:
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps({
                            "AntivirusEnabled": True,
                            "AMServiceEnabled": True,
                            "RealTimeProtectionEnabled": tamper_state["rtp"],
                            "IsTamperProtected": tamper_state["tamper"],
                        }),
                        stderr="",
                    )
                if "ExclusionPath" in ps_cmd and "Get-MpPreference" in ps_cmd:
                    return subprocess.CompletedProcess(
                        cmd, 0, stdout=json.dumps(tamper_state["exclusions"]), stderr=""
                    )
                if "AntiVirusProduct" in ps_cmd:
                    return subprocess.CompletedProcess(
                        cmd,
                        0,
                        stdout=json.dumps([
                            {"displayName": "Windows Defender", "productState": 397568},
                            {"displayName": "ESET Security", "productState": 266240},
                        ]),
                        stderr="",
                    )
                if "Add-MpPreference" in ps_cmd:
                    tamper_state["exclusions"].append(tmp)
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                if "Remove-MpPreference" in ps_cmd:
                    tamper_state["exclusions"].clear()
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                if "DisableRealtimeMonitoring $false" in ps_cmd:
                    tamper_state["rtp"] = True
                    return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

            drv = WindowsSystemDriver(
                data_dir=Path(tmp),
                win32_ops=_FakeWin32Ops(is_admin=True, session_id=1),
                run_cmd=fake_run,
            )
            status = drv.get_defender_status()
            self.assertTrue(status["tamper_protected"])
            self.assertEqual(status["third_party_av"], ["ESET Security"])

            dis = drv.configure_defender("disable", exclusion_paths=[tmp])
            self.assertFalse(dis["ok"])
            self.assertTrue(dis["guided_fallback"])
            self.assertEqual(dis["guided_uri"], DEFENDER_THREAT_URI)

            ledger = Path(tmp) / "defender_exclusions_ledger.json"
            self.assertTrue(ledger.is_file())
            self.assertIn(os.path.normpath(tmp), json.loads(ledger.read_text(encoding="utf-8")))

            restored = drv.configure_defender("restore")
            self.assertTrue(restored["ok"])
            self.assertEqual(json.loads(ledger.read_text(encoding="utf-8")), [])


class TestWindowsInstallerDriverAndPS1(unittest.TestCase):
    def test_install_3dsmax_tails_status_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            iso = root / "Autodesk_3ds_Max_2025.iso"
            iso.write_bytes(b"ISO")
            status_file = root / "logs" / "3dsMax-2025-status.txt"
            status_file.parent.mkdir(parents=True, exist_ok=True)
            status_file.write_text("45%|Installing Autodesk 3ds Max 2025... (8500 files)\n", encoding="utf-8")

            class FakeProc:
                pid = 1234
                stdout = iter(["2026-11-20 [INFO] Setup started\n"])

                def __init__(self):
                    self._calls = 0

                def poll(self):
                    self._calls += 1
                    if self._calls == 1:
                        status_file.write_text("100%|Installation completed successfully.\n", encoding="utf-8")
                        return None
                    return 0

                def terminate(self):
                    pass

            progress_events = []
            logs = []
            drv = WindowsInstallerDriver(
                program_files=root / "PF",
                popen_fn=lambda *a, **kw: FakeProc(),
                poll_interval_sec=0.01,
            )
            res = drv.install_3dsmax(
                "2025",
                iso,
                progress_cb=lambda p, m: progress_events.append((p, m)),
                log_cb=lambda lvl, m: logs.append((lvl, m)),
                cancel_event=threading.Event(),
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "completed")
            self.assertIn((45, "Installing Autodesk 3ds Max 2025... (8500 files)"), progress_events)
            self.assertEqual(progress_events[-1][0], 100)

    def test_ps1_static_hardening_checks(self) -> None:
        ps1_text = (REPO_ROOT / "vm" / "Install-3dsMax.ps1").read_text(encoding="utf-8")
        self.assertIn("[switch]$QuietConsole", ps1_text)
        self.assertIn("if ($QuietConsole)", ps1_text)
        self.assertIn("Unblock-File", ps1_text)
        self.assertIn("Save-OdisLogs", ps1_text)
        self.assertIn("20 * 1GB", ps1_text)
        self.assertIn("StatusRefreshSeconds", ps1_text)
        self.assertIn("$refreshInterval", ps1_text)


class TestCLIBundledAHKWatcherAndMain(unittest.TestCase):
    def test_bundled_ahk_resolution(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            ahk_file = Path(tmp) / "tools" / "AutoHotkey64.exe"
            ahk_file.parent.mkdir(parents=True, exist_ok=True)
            ahk_file.write_text("MZ", encoding="utf-8")
            with patch.dict(os.environ, {"MAX_AGENT_AHK_EXE": str(ahk_file)}):
                bridge = AHKBridge(dry_run=True)
                self.assertEqual(bridge.ahk_exe, str(ahk_file))
            with patch.dict(os.environ, {"MAX_AUTO_AHK_EXE": str(ahk_file)}):
                bridge_auto = AHKBridge(dry_run=True)
                self.assertEqual(bridge_auto.ahk_exe, str(ahk_file))

    def test_max_deployer_env_discovery_and_dry_run_de_elevate(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            max_dir = Path(tmp) / "Max2026"
            max_dir.mkdir(parents=True, exist_ok=True)
            exe = max_dir / "3dsmax.exe"
            exe.write_text("MZ", encoding="utf-8")
            with patch.dict(os.environ, {"ADSK_3DSMAX_X64_2026": str(max_dir)}):
                self.assertEqual(get_installed_3dsmax_exe("2026"), exe)
                found = dict(find_all_installed_3dsmax(("2026",)))
                self.assertEqual(found["2026"], exe)
            self.assertIsNone(launch_3dsmax(max_exe=exe, dry_run=True, de_elevate=True))

    def test_watcher_client_profile_licensing_exit_10_and_max_timeout(self) -> None:
        cfg = WatcherConfig.from_yaml(REPO_ROOT / "config" / "rules.yaml")
        ahk = AHKBridge(dry_run=True)
        lic_snap = WindowSnapshot(
            hwnd=101,
            title="WebView2 WebBrowser",
            class_name="HwndWrapper",
            process_name="AdskLicensingAgent.exe",
            pid=999,
            rect=(0, 0, 800, 600),
            texts=["Specify a license server"],
        )
        with tempfile.TemporaryDirectory() as tmp:
            dump_dir = Path(tmp) / "dumps"
            with (
                patch("cli.watcher.list_visible_windows", return_value=[lic_snap]),
                patch("cli.watcher.populate_snapshot_controls", side_effect=lambda s: s),
                patch("cli.watcher.is_process_running", return_value=True),
            ):
                res = watch_loop(
                    config=cfg,
                    ahk=ahk,
                    poll_interval=0.01,
                    timeout_sec=5.0,
                    dump_dir=dump_dir,
                    stop_on_main_ready=True,
                    profile="client",
                )
                self.assertIsInstance(res, WatchResult)
                self.assertEqual(res.status, "licensing_required")
                self.assertEqual(res.exit_code, 10)
                self.assertTrue(any(dump_dir.glob("popup_*.json")))

                # Hard max_timeout caps even when 3dsmax.exe is still running
                res_to = watch_loop(
                    config=cfg,
                    ahk=ahk,
                    poll_interval=0.01,
                    timeout_sec=0.02,
                    max_timeout_sec=0.05,
                    dump_dir=dump_dir,
                    stop_on_main_ready=True,
                    profile="vm-test",
                )
                self.assertEqual(res_to.status, "timeout")
                self.assertEqual(res_to.exit_code, 1)

    def test_cli_run_client_profile_json_result_and_exit_10(self) -> None:
        runner = CliRunner()
        with tempfile.TemporaryDirectory() as tmp:
            json_out = Path(tmp) / "result.json"
            fake_result = WatchResult(
                status="licensing_required",
                detail="Licensing dialog detected",
                matched_rules=["Autodesk Sign-In / Identity Prompt (Dismiss if offline/testing)"],
                screenshots=[str(Path(tmp) / "lic.png")],
                elapsed_sec=1.2,
                exit_code=10,
            )
            with (
                patch("cli.main.is_process_running", return_value=False),
                patch("cli.main.watch_loop", return_value=fake_result),
            ):
                res = runner.invoke(
                    cli,
                    [
                        "run",
                        "--no-launch",
                        "--profile",
                        "client",
                        "--json-result",
                        str(json_out),
                        "--dry-run",
                    ],
                )
            self.assertEqual(res.exit_code, 10)
            self.assertTrue(json_out.is_file())
            data = json.loads(json_out.read_text(encoding="utf-8"))
            self.assertEqual(data["status"], "licensing_required")
            self.assertEqual(data["exit_code"], 10)

    def test_windows_first_run_driver_parses_json_result(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            dump_dir = Path(tmp) / "dumps"
            dump_dir.mkdir(parents=True, exist_ok=True)
            json_result = dump_dir / "first_run_2025_result.json"

            class FakeProc:
                pid = 5678
                stdout = iter(["[WATCH] [CLIENT] Licensing required\n"])

                def poll(self):
                    json_result.write_text(
                        json.dumps({
                            "status": "licensing_required",
                            "detail": "Sign-in required",
                            "matched_rules": ["Autodesk Sign-In"],
                            "screenshots": [str(dump_dir / "lic.png")],
                        }),
                        encoding="utf-8",
                    )
                    return 10

                def terminate(self):
                    pass

            drv = WindowsFirstRunDriver(
                repo_root=REPO_ROOT,
                popen_fn=lambda *a, **kw: FakeProc(),
                poll_interval_sec=0.01,
            )
            res = drv.run_first_run(
                "2025",
                log_cb=lambda *_a: None,
                cancel_event=threading.Event(),
                dump_dir=dump_dir,
            )
            self.assertEqual(res["status"], "licensing_required")
            self.assertEqual(res["matched_rules"], ["Autodesk Sign-In"])


if __name__ == "__main__":
    unittest.main()
