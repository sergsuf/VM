"""Unit tests for M3 1-click POST_CONFIG pipeline, configs, MaxScript, and deploy_scripts."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import yaml

from cli.max_deployer import deploy_scripts, get_max_user_enu_dir
from remote.drivers.windows import WindowsPostConfigDriver

REPO_ROOT = Path(__file__).resolve().parent.parent


class TestPostConfigYAMLAndMaxScript(unittest.TestCase):
    def test_post_config_and_agent_example_yaml_valid_and_tab_free(self) -> None:
        for rel in ("config/post_config.yaml", "config/agent.example.yaml"):
            path = REPO_ROOT / rel
            self.assertTrue(path.is_file(), f"Missing {rel}")
            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("\t", raw, f"Tab character found in {rel}")
            parsed = yaml.safe_load(raw)
            self.assertIsInstance(parsed, dict)

        post_raw = (REPO_ROOT / "config" / "post_config.yaml").read_text(encoding="utf-8")
        lower = post_raw.lower()
        for kw in ("corona", "vray", "theme", "quad", "hotkey", "script", "verify"):
            self.assertIn(kw, lower)

        drv = WindowsPostConfigDriver(repo_root=REPO_ROOT)
        steps_2025 = drv.load_steps(config_path=REPO_ROOT / "config" / "post_config.yaml", version="2025")
        categories = {s.category for s in steps_2025}
        self.assertEqual(categories, {"renderer", "theme", "quad_menu", "hotkey", "scripts", "verify"})

    def test_verify_install_ms_is_version_dynamic_and_writes_json(self) -> None:
        ms_text = (REPO_ROOT / "max_scripts" / "verify_install.ms").read_text(encoding="utf-8")
        self.assertIn("max_verify.json", ms_text)
        self.assertIn("maxVersion", ms_text)
        self.assertRegex(ms_text, r"rendererClass|renderers")
        self.assertNotIn("max_2025_ready.txt", ms_text)


class TestDeployScriptsAndZipSlip(unittest.TestCase):
    def test_recursive_deploy_scripts_with_max_version_and_mzp_run(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lad = root / "LocalAppData"
            src = root / "assets"
            (src / "nested").mkdir(parents=True, exist_ok=True)
            (src / "icons").mkdir(parents=True, exist_ok=True)
            (src / "startup_tool.ms").write_text("print 1", encoding="utf-8")
            (src / "nested" / "helper.mse").write_bytes(b"MSE")
            (src / "nested" / "MyMacro.mcr").write_text("macroScript M ()", encoding="utf-8")
            (src / "icons" / "tool_24i.bmp").write_bytes(b"BM")

            mzp_path = src / "pack.mzp"
            with zipfile.ZipFile(mzp_path, "w") as zf:
                zf.writestr("mzp.run", 'copy "scripts/pack_init.ms" to "$userScripts"\n')
                zf.writestr("scripts/pack_init.ms", "print \"pack_init\"\n")

            with patch.dict(os.environ, {"LOCALAPPDATA": str(lad)}):
                deployed = deploy_scripts(src, max_version="2025")
                enu = get_max_user_enu_dir("2025")

            self.assertTrue((enu / "scripts" / "startup" / "startup_tool.ms").is_file())
            self.assertTrue((enu / "scripts" / "startup" / "helper.mse").is_file())
            self.assertTrue((enu / "scripts" / "startup" / "pack_init.ms").is_file())
            self.assertTrue((enu / "usermacros" / "MyMacro.mcr").is_file())
            self.assertTrue((enu / "usericons" / "tool_24i.bmp").is_file())
            self.assertGreaterEqual(len(deployed), 5)

    def test_deploy_scripts_rejects_mzp_zip_slip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lad = root / "LocalAppData"
            src = root / "evil_assets"
            src.mkdir(parents=True, exist_ok=True)
            evil_mzp = src / "evil.mzp"
            with zipfile.ZipFile(evil_mzp, "w") as zf:
                zf.writestr("../../escaped_from_mzp.ms", "evil()")

            with patch.dict(os.environ, {"LOCALAPPDATA": str(lad)}):
                with self.assertRaises(ValueError):
                    deploy_scripts(src, max_version="2025")
            self.assertFalse((root / "escaped_from_mzp.ms").exists())


class TestWindowsPostConfigDriver(unittest.TestCase):
    def test_version_routing_2024_mnux_vs_2025_mnx_hsx_and_enu_backup(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            lad = root / "LocalAppData"
            pf = root / "ProgramFiles"
            assets = root / "assets"
            assets.mkdir(parents=True, exist_ok=True)

            # Create mixed assets
            (assets / "StudioTheme.clrx").write_text("<ColorTheme/>", encoding="utf-8")
            (assets / "StudioUI.cuix").write_text("<CUIX/>", encoding="utf-8")
            (assets / "LegacyMenu.mnux").write_text("<MNUX/>", encoding="utf-8")
            (assets / "ModernMenu.mnx").write_text("<MNX/>", encoding="utf-8")
            (assets / "ModernKeys.hsx").write_text("<HSX/>", encoding="utf-8")
            (assets / "StudioTool.ms").write_text("print 42", encoding="utf-8")

            # Pre-seed ENU/3dsmax.ini to verify backup & non-destructive merge
            enu_2025 = lad / "Autodesk" / "3dsMax" / "2025 - 64bit" / "ENU"
            enu_2025.mkdir(parents=True, exist_ok=True)
            (enu_2025 / "3dsmax.ini").write_text("[Performance]\nThreads=16\n", encoding="utf-8")

            drv = WindowsPostConfigDriver(
                repo_root=REPO_ROOT,
                program_files=pf,
                local_app_data=lad,
                data_dir=root / "data",
            )
            cancel = threading.Event()

            # Missing renderer assets should skip without corrupting 3dsmax.ini
            res_corona = drv.run_step(
                "renderer_corona",
                "2025",
                assets_dir=assets,
                log_cb=lambda *_a: None,
                cancel_event=cancel,
            )
            self.assertEqual(res_corona["status"], "skipped")
            self.assertIn("Threads=16", (enu_2025 / "3dsmax.ini").read_text(encoding="utf-8"))

            # Backup created and preserves original 3dsmax.ini
            backup_2025 = enu_2025.parent / "ENU_backup"
            self.assertTrue(backup_2025.is_dir())
            self.assertIn("Threads=16", (backup_2025 / "3dsmax.ini").read_text(encoding="utf-8"))

            # Theme, Quad Menu, Hotkeys, Scripts, Verify for 2025
            for sid in ("ui_theme", "ui_quad_menu", "ui_hotkeys", "scripts_deploy", "verify_install"):
                res = drv.run_step(
                    sid,
                    "2025",
                    assets_dir=assets,
                    log_cb=lambda *_a: None,
                    cancel_event=cancel,
                )
                self.assertTrue(res["ok"], f"{sid} failed: {res}")

            self.assertTrue((enu_2025 / "en-US" / "UI" / "StudioTheme.clrx").is_file())
            self.assertTrue((enu_2025 / "en-US" / "UI" / "UserSettings" / "ModernMenu.mnx").is_file())
            self.assertTrue((enu_2025 / "en-US" / "UI" / "UserSettings" / "ModernKeys.hsx").is_file())
            self.assertFalse((enu_2025 / "en-US" / "UI" / "LegacyMenu.mnux").exists())
            self.assertIn("ColorScheme = StudioTheme", (enu_2025 / "3dsmax.ini").read_text(encoding="utf-8"))
            self.assertTrue((enu_2025 / "scripts" / "startup" / "verify_install.ms").is_file())

            # Verify 2024 routes ONLY .mnux into en-US/UI/ (no .mnx)
            res_2024 = drv.run_step(
                "ui_quad_menu",
                "2024",
                assets_dir=assets,
                log_cb=lambda *_a: None,
                cancel_event=cancel,
            )
            self.assertTrue(res_2024["ok"])
            enu_2024 = lad / "Autodesk" / "3dsMax" / "2024 - 64bit" / "ENU"
            self.assertTrue((enu_2024 / "en-US" / "UI" / "LegacyMenu.mnux").is_file())
            self.assertFalse((enu_2024 / "en-US" / "UI" / "UserSettings" / "ModernMenu.mnx").exists())

            # Verify max_verify.json generated
            verify_json = enu_2025.parent / "max_verify.json"
            self.assertTrue(verify_json.is_file())
            report = json.loads(verify_json.read_text(encoding="utf-8"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["version"], "2025")

    def test_skip_if_renderer_plugin_already_installed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plugins = root / "ProgramFiles" / "Autodesk" / "3ds Max 2025" / "plugins"
            plugins.mkdir(parents=True, exist_ok=True)
            (plugins / "CoronaMaxUtils.dlu").write_bytes(b"DLU")

            drv = WindowsPostConfigDriver(
                repo_root=REPO_ROOT,
                program_files=root / "ProgramFiles",
                local_app_data=root / "LocalAppData",
                data_dir=root / "data",
            )
            res = drv.run_step(
                "renderer_corona",
                "2025",
                assets_dir=None,
                log_cb=lambda *_a: None,
                cancel_event=threading.Event(),
            )
            self.assertTrue(res["ok"])
            self.assertEqual(res["status"], "skipped")
            self.assertTrue(res.get("skipped"))


if __name__ == "__main__":
    unittest.main()
