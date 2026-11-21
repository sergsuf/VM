"""Unit and integration tests for M4: updater, bootstrap supervisor, packaging, CI, and docs."""

from __future__ import annotations

import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import zipfile

from remote.protocol import Stage, StageStatus, UpdateManifest
from remote.state_machine import DeploymentStateMachine
from remote.updater import (
    UPDATE_RESTART_EXIT_CODE,
    AppUpdater,
    UpdateBlockedError,
    UpdateCompatibilityError,
    UpdateError,
    UpdateVerificationError,
    can_accept_update,
    compare_semver,
    mark_healthy_boot,
    parse_semver,
    prune_releases,
)
from remote.client import bootstrap

REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_zip(zip_path: Path, files: dict[str, bytes]) -> tuple[Path, str, int]:
    zip_path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for name, content in files.items():
            zf.writestr(name, content)
    data = zip_path.read_bytes()
    return zip_path, hashlib.sha256(data).hexdigest(), len(data)


class TestUpdater(unittest.TestCase):
    """Tests for remote/updater.py (F-18)."""

    def test_semver_parse_and_compare(self) -> None:
        self.assertEqual(parse_semver("1.2.3"), (1, 2, 3))
        self.assertEqual(parse_semver("v2.0"), (2, 0, 0))
        self.assertEqual(compare_semver("1.2.3", "1.2.3"), 0)
        self.assertEqual(compare_semver("1.2.4", "1.2.3"), 1)
        self.assertEqual(compare_semver("1.2.3", "2.0.0"), -1)

    def test_apply_update_local_archive_and_healthy_boot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            (app_dir / "releases" / "1.0.0").mkdir(parents=True)
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

            zip_path, digest, size = _make_zip(
                root / "staging" / "upd-1.2.0.zip",
                {"VERSION.txt": b"1.2.0\n", "remote/mod.py": b"X = 1\n"},
            )
            manifest = UpdateManifest(
                version="1.2.0",
                min_runtime_version="1.0.0",
                file_id="upd-120",
                filename=zip_path.name,
                size_bytes=size,
                sha256=digest.upper(),  # verify case-insensitive SHA-256
            )

            updater = AppUpdater(app_dir=app_dir, runtime_version="1.0.0")
            state = updater.apply_update(manifest, archive_path=zip_path)
            self.assertEqual(state["version"], "1.2.0")
            self.assertEqual(state["previous"], "1.0.0")
            self.assertTrue(state["pending_verify"])
            self.assertEqual(state["attempts"], 0)
            self.assertTrue((app_dir / "releases" / "1.2.0" / "VERSION.txt").is_file())

            cleared = mark_healthy_boot(app_dir)
            self.assertFalse(cleared["pending_verify"])
            self.assertEqual(cleared["attempts"], 0)
            persisted = json.loads(active_path.read_text(encoding="utf-8"))
            self.assertFalse(persisted["pending_verify"])

    def test_apply_update_http_download_with_headers(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            (app_dir / "releases" / "1.0.0").mkdir(parents=True)
            zip_path, digest, size = _make_zip(
                root / "srv" / "upd-1.1.0.zip",
                {"VERSION.txt": b"1.1.0\n"},
            )
            payload = zip_path.read_bytes()
            seen_auth: list[str | None] = []

            class Handler(BaseHTTPRequestHandler):
                def do_GET(self) -> None:  # noqa: N802
                    seen_auth.append(self.headers.get("Authorization"))
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

                def log_message(self, format: str, *args: object) -> None:
                    return

            srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
            t = threading.Thread(target=srv.serve_forever, daemon=True)
            t.start()
            try:
                url = f"http://127.0.0.1:{srv.server_port}/upd-1.1.0.zip"
                manifest = UpdateManifest(
                    version="1.1.0",
                    min_runtime_version="1.0.0",
                    file_id="upd-110",
                    filename="upd-1.1.0.zip",
                    size_bytes=size,
                    sha256=digest,
                    download_url=url,
                )
                updater = AppUpdater(app_dir=app_dir, current_version="1.0.0")
                state = updater.apply_update(
                    manifest,
                    headers={"Authorization": "Bearer secret-tok"},
                )
                self.assertEqual(state["version"], "1.1.0")
                self.assertEqual(seen_auth, ["Bearer secret-tok"])
            finally:
                srv.shutdown()
                srv.server_close()

    def test_gates_semver_blocked_and_min_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            (app_dir / "releases" / "1.5.0").mkdir(parents=True)
            active_path = app_dir / "active.json"
            orig = {
                "version": "1.5.0",
                "previous": "1.4.0",
                "pending_verify": False,
                "attempts": 0,
                "blocked_versions": ["1.6.0"],
            }
            active_path.write_text(json.dumps(orig), encoding="utf-8")

            zip_path, digest, size = _make_zip(root / "u.zip", {"VERSION.txt": b"x\n"})
            updater = AppUpdater(app_dir=app_dir, runtime_version="1.0.0")

            # Blocked version
            m_blocked = UpdateManifest(
                version="1.6.0",
                min_runtime_version="1.0.0",
                file_id="b",
                filename="u.zip",
                size_bytes=size,
                sha256=digest,
            )
            with self.assertRaises(UpdateBlockedError):
                updater.apply_update(m_blocked, archive_path=zip_path)

            # Same / older SemVer
            for ver in ("1.5.0", "1.4.9"):
                m_old = UpdateManifest(
                    version=ver,
                    min_runtime_version="1.0.0",
                    file_id="o",
                    filename="u.zip",
                    size_bytes=size,
                    sha256=digest,
                )
                with self.assertRaises(UpdateCompatibilityError):
                    updater.apply_update(m_old, archive_path=zip_path)

            # Incompatible min_runtime_version
            m_rt = UpdateManifest(
                version="2.0.0",
                min_runtime_version="2.0.0",
                file_id="r",
                filename="u.zip",
                size_bytes=size,
                sha256=digest,
            )
            with self.assertRaises(UpdateCompatibilityError):
                updater.apply_update(m_rt, archive_path=zip_path)

            self.assertEqual(json.loads(active_path.read_text(encoding="utf-8")), orig)

    def test_sha256_mismatch_leaves_active_untouched(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app_dir = root / "app"
            active_path = app_dir / "active.json"
            app_dir.mkdir(parents=True)
            orig_text = json.dumps(
                {
                    "version": "1.0.0",
                    "previous": None,
                    "pending_verify": False,
                    "attempts": 0,
                    "blocked_versions": [],
                }
            )
            active_path.write_text(orig_text, encoding="utf-8")
            zip_path, _, size = _make_zip(root / "u.zip", {"VERSION.txt": b"2.0.0\n"})
            manifest = UpdateManifest(
                version="2.0.0",
                min_runtime_version="1.0.0",
                file_id="bad",
                filename="u.zip",
                size_bytes=size,
                sha256="0" * 64,
            )
            updater = AppUpdater(app_dir=app_dir)
            with self.assertRaises(UpdateVerificationError):
                updater.apply_update(manifest, archive_path=zip_path)
            self.assertEqual(active_path.read_text(encoding="utf-8"), orig_text)
            self.assertFalse((app_dir / "releases" / "2.0.0").exists())

    def test_zip_slip_traversal_and_absolute_paths_rejected(self) -> None:
        bad_entries = [
            "../escape.txt",
            "..\\win_escape.txt",
            "/tmp/abs_escape.txt",
            "C:\\win_abs.txt",
        ]
        for entry_name in bad_entries:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                app_dir = root / "app"
                app_dir.mkdir(parents=True)
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
                zip_path, digest, size = _make_zip(
                    root / "evil.zip",
                    {"VERSION.txt": b"2.0.0\n", entry_name: b"evil"},
                )
                manifest = UpdateManifest(
                    version="2.0.0",
                    min_runtime_version="1.0.0",
                    file_id="evil",
                    filename="evil.zip",
                    size_bytes=size,
                    sha256=digest,
                )
                updater = AppUpdater(app_dir=app_dir)
                with self.assertRaises(UpdateVerificationError):
                    updater.apply_update(manifest, archive_path=zip_path)
                self.assertEqual(
                    json.loads(active_path.read_text(encoding="utf-8"))["version"], "1.0.0"
                )
                self.assertFalse((app_dir / "releases" / "2.0.0").exists())

    def test_prune_releases_preserves_active_and_previous(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_dir = Path(tmp) / "app"
            for ver in ("0.8.0", "0.9.0", "1.0.0", "1.1.0", "1.2.0"):
                (app_dir / "releases" / ver).mkdir(parents=True)
            (app_dir / "active.json").write_text(
                json.dumps(
                    {
                        "version": "1.2.0",
                        "previous": "0.9.0",
                        "pending_verify": False,
                        "attempts": 0,
                        "blocked_versions": [],
                    }
                ),
                encoding="utf-8",
            )
            removed = prune_releases(app_dir, keep=2)
            remaining = {p.name for p in (app_dir / "releases").iterdir()}
            self.assertIn("1.2.0", remaining)
            self.assertIn("0.9.0", remaining)
            self.assertTrue(len(removed) >= 1)

    def test_can_accept_update_with_state_machine(self) -> None:
        sm = DeploymentStateMachine(session_id="S-1", client_id="c1")
        self.assertTrue(can_accept_update(sm))
        sm.start_install("2025")
        self.assertFalse(can_accept_update(sm))
        sm.complete_iso_download("/tmp/max.iso")
        self.assertFalse(can_accept_update(sm))
        sm.complete_install()
        self.assertEqual(sm.state.stage, Stage.WAIT_LICENSE)
        self.assertEqual(sm.state.status, StageStatus.WAITING_OPERATOR)
        self.assertTrue(can_accept_update(sm))


class TestBootstrapSupervisor(unittest.TestCase):
    """Tests for remote/client/bootstrap.py (F-12, F-19)."""

    def test_constants_and_help_cli(self) -> None:
        self.assertEqual(UPDATE_RESTART_EXIT_CODE, 42)
        self.assertEqual(bootstrap.UPDATE_RESTART_EXIT_CODE, 42)
        self.assertEqual(bootstrap.MAX_VERIFY_ATTEMPTS, 2)
        res = subprocess.run(
            [sys.executable, str(REPO_ROOT / "remote" / "client" / "bootstrap.py"), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0)
        self.assertIn("--root", res.stdout)
        self.assertIn("--no-elevate", res.stdout)

    def test_exit_42_hotswap_and_extra_arg_forwarding(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            log_file = app_root / "boot.log"
            rel1 = app_root / "app" / "releases" / "1.0.0" / "remote" / "client"
            rel2 = app_root / "app" / "releases" / "1.1.0" / "remote" / "client"
            for r in (rel1, rel2):
                r.mkdir(parents=True)
                (r.parent / "__init__.py").write_text("", encoding="utf-8")
                (r / "__init__.py").write_text("", encoding="utf-8")

            active_path = app_root / "app" / "active.json"
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
                f"with open({str(log_file)!r}, 'a', encoding='utf-8') as f: f.write('1.0.0:' + ' '.join(sys.argv[1:]) + '\\n')\n"
                f"pathlib.Path({str(active_path)!r}).write_text(json.dumps({{'version':'1.1.0','previous':'1.0.0','pending_verify':True,'attempts':0,'blocked_versions':[]}}), encoding='utf-8')\n"
                f"sys.exit(42)\n",
                encoding="utf-8",
            )
            (rel2 / "main.py").write_text(
                f"import json, pathlib, sys\n"
                f"with open({str(log_file)!r}, 'a', encoding='utf-8') as f: f.write('1.1.0:' + ' '.join(sys.argv[1:]) + '\\n')\n"
                f"p = pathlib.Path({str(active_path)!r})\n"
                f"d = json.loads(p.read_text(encoding='utf-8'))\n"
                f"d['pending_verify'] = False\n"
                f"d['attempts'] = 0\n"
                f"p.write_text(json.dumps(d), encoding='utf-8')\n"
                f"sys.exit(0)\n",
                encoding="utf-8",
            )

            res = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "remote" / "client" / "bootstrap.py"),
                    "--root",
                    str(app_root),
                    "--no-elevate",
                    "--headless",
                    "--driver",
                    "simulated",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            lines = log_file.read_text(encoding="utf-8").strip().splitlines()
            self.assertEqual(
                lines,
                ["1.0.0:--headless --driver simulated", "1.1.0:--headless --driver simulated"],
            )

    def test_two_attempt_crash_rollback_and_blacklist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            log_file = app_root / "rb.log"
            rel_good = app_root / "app" / "releases" / "1.0.0" / "remote" / "client"
            rel_bad = app_root / "app" / "releases" / "2.0.0" / "remote" / "client"
            for r in (rel_good, rel_bad):
                r.mkdir(parents=True)
                (r.parent / "__init__.py").write_text("", encoding="utf-8")
                (r / "__init__.py").write_text("", encoding="utf-8")

            active_path = app_root / "app" / "active.json"
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
                f"open({str(log_file)!r}, 'a').write('crash\\n')\n"
                f"sys.exit(1)\n",
                encoding="utf-8",
            )
            (rel_good / "main.py").write_text(
                f"import sys\n"
                f"open({str(log_file)!r}, 'a').write('ok\\n')\n"
                f"sys.exit(0)\n",
                encoding="utf-8",
            )

            rc = bootstrap.run_supervisor(app_root, no_elevate=True)
            self.assertEqual(rc, 0)
            self.assertEqual(log_file.read_text(encoding="utf-8").splitlines(), ["crash", "crash", "ok"])
            active = json.loads(active_path.read_text(encoding="utf-8"))
            self.assertEqual(active["version"], "1.0.0")
            self.assertIsNone(active["previous"])
            self.assertFalse(active["pending_verify"])
            self.assertIn("2.0.0", active["blocked_versions"])

    def test_missing_previous_exits_nonzero_cleanly(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            app_root = Path(tmp)
            rel_bad = app_root / "app" / "releases" / "1.0.0" / "remote" / "client"
            rel_bad.mkdir(parents=True)
            (rel_bad.parent / "__init__.py").write_text("", encoding="utf-8")
            (rel_bad / "__init__.py").write_text("", encoding="utf-8")
            (rel_bad / "main.py").write_text("import sys\nsys.exit(3)\n", encoding="utf-8")
            (app_root / "app" / "active.json").write_text(
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
            rc = bootstrap.run_supervisor(app_root, no_elevate=True)
            self.assertEqual(rc, 3)

    def test_windows_session0_guard_and_uac_elevation_mocked(self) -> None:
        with mock.patch.object(bootstrap.os, "name", "nt"):
            with mock.patch.object(bootstrap, "get_session_id", return_value=0):
                with self.assertRaises(RuntimeError):
                    bootstrap.ensure_elevated_interactive(no_elevate=False)

            fake_shell32 = mock.Mock()
            fake_shell32.ShellExecuteW.return_value = 42
            fake_windll = mock.Mock()
            fake_windll.shell32 = fake_shell32
            with (
                mock.patch.object(bootstrap, "get_session_id", return_value=1),
                mock.patch.object(bootstrap, "is_user_admin", return_value=False),
                mock.patch.object(bootstrap, "show_pre_uac_notice") as mock_notice,
                mock.patch.object(bootstrap.ctypes, "windll", fake_windll, create=True),
            ):
                cont = bootstrap.ensure_elevated_interactive(no_elevate=False, argv=["--headless"])
                self.assertFalse(cont)
                mock_notice.assert_called_once()
                fake_shell32.ShellExecuteW.assert_called_once()


class TestPackagingCiAndDocs(unittest.TestCase):
    """Tests for packaging/, .github/workflows/ci.yml, and docs/REMOTE_DEPLOY.md (F-19, F-20)."""

    def test_build_app_update_valid_and_invalid_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            res = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "packaging" / "build_app_update.py"),
                    "--version",
                    "1.2.3",
                    "--out-dir",
                    str(out),
                    "--min-runtime-version",
                    "1.0.0",
                    "--notes",
                    "Unit test build",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            zips = list(out.glob("*.zip"))
            self.assertEqual(len(zips), 1)
            manifest = UpdateManifest.from_dict(
                json.loads((out / "manifest.json").read_text(encoding="utf-8"))
            )
            self.assertEqual(manifest.version, "1.2.3")
            self.assertEqual(manifest.size_bytes, zips[0].stat().st_size)

            with zipfile.ZipFile(zips[0], "r") as zf:
                names = zf.namelist()
                self.assertIn("VERSION.txt", names)
                self.assertTrue(any(n.startswith("remote/") for n in names))
                self.assertTrue(any(n.startswith("cli/") for n in names))
                self.assertTrue(any(n.startswith("config/") for n in names))
                for n in names:
                    self.assertFalse(n.startswith("/") or ".." in n)
                    self.assertFalse(n.startswith(".git/"))
                    self.assertFalse(n.startswith(".agents/"))
                    self.assertFalse(n.startswith("tests/"))

            for bad_ver in ("", "../evil", "1.0/../../bad", "1.0\\bad"):
                bad_res = subprocess.run(
                    [
                        sys.executable,
                        str(REPO_ROOT / "packaging" / "build_app_update.py"),
                        "--version",
                        bad_ver,
                        "--out-dir",
                        str(out),
                    ],
                    capture_output=True,
                    text=True,
                    timeout=15,
                )
                self.assertNotEqual(bad_res.returncode, 0, f"Accepted invalid version {bad_ver!r}")

    def test_build_portable_client_offline_layout(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "portable"
            res = subprocess.run(
                [
                    sys.executable,
                    str(REPO_ROOT / "packaging" / "build_portable_client.py"),
                    "--out-dir",
                    str(out),
                    "--version",
                    "1.0.0",
                    "--offline",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            self.assertEqual(res.returncode, 0, res.stderr)
            self.assertTrue((out / "Start-MaxAgent.bat").is_file())
            self.assertTrue((out / "runtime" / "bootstrap.py").is_file())
            self.assertTrue((out / "runtime" / "version.json").is_file())
            self.assertTrue((out / "runtime" / "Lib" / "site-packages").is_dir())
            self.assertTrue((out / "tools" / "ahk" / "AutoHotkey64.exe").exists())
            self.assertTrue((out / "app" / "releases" / "1.0.0" / "VERSION.txt").is_file())

            pth = (out / "runtime" / "python312._pth").read_text(encoding="utf-8")
            self.assertRegex(pth, r"(?m)^import site\s*$")
            self.assertRegex(pth, r"Lib[/\\]site-packages")

            active = json.loads((out / "app" / "active.json").read_text(encoding="utf-8"))
            self.assertEqual(active["version"], "1.0.0")
            self.assertFalse(active["pending_verify"])

    def test_start_bat_ps1_ci_and_docs(self) -> None:
        bat = (REPO_ROOT / "packaging" / "Start-MaxAgent.bat").read_text(encoding="utf-8")
        self.assertIn("%~dp0", bat)
        self.assertIn("python.exe", bat.lower())
        self.assertIn("bootstrap.py", bat.lower())
        self.assertIn("%*", bat)
        for bad in (".vbs", "wscript", "cscript"):
            self.assertNotIn(bad, bat.lower())

        ps1 = (REPO_ROOT / "packaging" / "build_portable_client.ps1").read_text(encoding="utf-8")
        self.assertIn("python312._pth", ps1)
        self.assertIn("import site", ps1)
        self.assertIn("AutoHotkey64.exe", ps1)

        ci = (REPO_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        self.assertNotIn("\t", ci)
        self.assertIn("jobs:", ci)
        self.assertRegex(ci, r"(?m)^on:")
        self.assertIn("unittest", ci)
        self.assertIn("pytest", ci)
        self.assertTrue((REPO_ROOT / ".github" / "workflows" / "vm-orchestrator.yml").is_file())

        doc = (REPO_ROOT / "docs" / "REMOTE_DEPLOY.md").read_text(encoding="utf-8")
        self.assertGreater(len(doc), 800)
        for topic in ("direct", "relay", "smart app control", "defender", "wait_license", "post_config"):
            self.assertIn(topic, doc.lower())

        # M5 completed: remote/client/__init__.py is present as part of the client package
        self.assertTrue((REPO_ROOT / "remote" / "client" / "__init__.py").is_file())


if __name__ == "__main__":
    unittest.main()
