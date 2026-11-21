"""Tests for single-file SFX packaging assets."""

from __future__ import annotations

import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from remote.client.bootstrap import build_arg_parser, build_child_command, resolve_launch_role

REPO_ROOT = Path(__file__).resolve().parent.parent


class SfxPackagingTests(unittest.TestCase):
    def test_sfx_stub_and_builder_exist_with_expected_behavior(self) -> None:
        stub = (REPO_ROOT / "packaging" / "MaxAgentSfxStub.cs").read_text(encoding="utf-8")
        builder = (REPO_ROOT / "packaging" / "build_sfx_exe.ps1").read_text(encoding="utf-8")

        self.assertIn('SubfolderName = "MaxAgent"', stub)
        self.assertIn("Start-MaxAgent.bat", stub)
        self.assertIn("connect.json", stub)
        self.assertIn("operator.mode", stub)
        self.assertIn("ZipFile.OpenRead", stub)
        self.assertIn("Blocked unsafe zip entry", stub)

        self.assertIn("build_portable_client.ps1", builder)
        self.assertIn("MaxAgentSfxStub.cs", builder)
        self.assertIn("csc.exe", builder)
        self.assertIn('/resource:"$ZipPath,MaxAgent.zip"', builder)

    def test_resolve_launch_role_modes_and_interactive_choice(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual(resolve_launch_role(root, explicit_role="operator"), "operator")
            self.assertEqual(resolve_launch_role(root, explicit_role="client"), "client")

            # Interactive prompt selection
            with mock.patch("builtins.input", return_value="2"):
                self.assertEqual(resolve_launch_role(root, interactive_tty=True), "operator")
            with mock.patch("builtins.input", return_value=""):
                self.assertEqual(resolve_launch_role(root, interactive_tty=True), "client")

            # connect.json skips prompt and picks client
            (root / "connect.json").write_text("{}", encoding="utf-8")
            self.assertEqual(resolve_launch_role(root, interactive_tty=True), "client")

            # operator.mode overrides to operator
            (root / "operator.mode").write_text("", encoding="utf-8")
            self.assertEqual(resolve_launch_role(root, interactive_tty=True), "operator")

    def test_bootstrap_cli_operator_flag_and_command(self) -> None:
        known, _ = build_arg_parser().parse_known_args(["--operator", "--no-elevate"])
        self.assertEqual(known.role, "operator")
        cmd = build_child_command("python.exe", ["--port", "8765"], module_name="remote.operator.main")
        self.assertIn("remote.operator.main", cmd[3])
        self.assertEqual(cmd[-2:], ["--port", "8765"])


if __name__ == "__main__":
    unittest.main()
