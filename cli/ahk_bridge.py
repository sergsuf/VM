"""Bridge for invoking AutoHotkey v2 actions from Python."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

DEFAULT_AHK_PATHS = [
    r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe",
    r"C:\Program Files\AutoHotkey\v2\AutoHotkey32.exe",
    r"C:\Program Files\AutoHotkey\AutoHotkey.exe",
]


class AHKBridge:
    def __init__(
        self,
        ahk_exe: str | Path | None = None,
        script_path: str | Path | None = None,
        dry_run: bool = False,
    ) -> None:
        self.dry_run = dry_run
        self.script_path = (
            Path(script_path)
            if script_path
            else Path(__file__).resolve().parent.parent / "ahk" / "actions.ahk"
        )
        self.ahk_exe = self._resolve_ahk_exe(ahk_exe)

    @staticmethod
    def _resolve_ahk_exe(explicit: str | Path | None) -> str:
        if explicit:
            return str(explicit)
        for name in ("AutoHotkey64.exe", "AutoHotkey.exe", "autohotkey"):
            found = shutil.which(name)
            if found:
                return found
        for candidate in DEFAULT_AHK_PATHS:
            if Path(candidate).exists():
                return candidate
        return DEFAULT_AHK_PATHS[0]

    def _run(self, *args: str, timeout: int = 10) -> int:
        cmd = [self.ahk_exe, str(self.script_path), *args]
        if self.dry_run:
            print(f"[DRY-RUN AHK] {' '.join(cmd)}")
            return 0
        if not Path(self.ahk_exe).exists() and shutil.which(self.ahk_exe) is None:
            raise FileNotFoundError(
                f"AutoHotkey v2 executable not found: {self.ahk_exe}. "
                "Install AutoHotkey v2 or pass --ahk-exe."
            )
        startupinfo = None
        if sys.platform == "win32":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.lpDesktop = r"winsta0\default"
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            startupinfo=startupinfo,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(
                f"AHK command failed (code {result.returncode}): {result.stderr.strip()}"
            )
        return result.returncode

    def click(self, x: int, y: int, hwnd: int = 0) -> None:
        self._run("click", str(x), str(y), str(hwnd))

    def ctrl_click(self, hwnd: int, target_or_x: str | int, y: int | None = None) -> None:
        if y is not None:
            self._run("ctrlclick", str(hwnd), str(target_or_x), str(y))
        else:
            self._run("ctrlclick", str(hwnd), str(target_or_x))

    def send_keys(self, keys: str, hwnd: int = 0) -> None:
        self._run("send", keys, str(hwnd))

    def send_text(self, text: str, hwnd: int = 0) -> None:
        self._run("text", text, str(hwnd))

    def activate(self, hwnd: int) -> None:
        self._run("activate", str(hwnd))

    def close_window(self, hwnd: int) -> None:
        self._run("close", str(hwnd))
