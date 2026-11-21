"""Tests for single-file SFX packaging assets."""

from __future__ import annotations

import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


class SfxPackagingTests(unittest.TestCase):
    def test_sfx_stub_and_builder_exist_with_expected_behavior(self) -> None:
        stub = (REPO_ROOT / "packaging" / "MaxAgentSfxStub.cs").read_text(encoding="utf-8")
        builder = (REPO_ROOT / "packaging" / "build_sfx_exe.ps1").read_text(encoding="utf-8")

        self.assertIn('SubfolderName = "MaxAgent"', stub)
        self.assertIn("Start-MaxAgent.bat", stub)
        self.assertIn("connect.json", stub)
        self.assertIn("ZipFile.OpenRead", stub)
        self.assertIn("Blocked unsafe zip entry", stub)

        self.assertIn("build_portable_client.ps1", builder)
        self.assertIn("MaxAgentSfxStub.cs", builder)
        self.assertIn("csc.exe", builder)
        self.assertIn('/resource:"$ZipPath,MaxAgent.zip"', builder)


if __name__ == "__main__":
    unittest.main()
