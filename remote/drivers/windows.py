"""Pure-Python stdlib-first Windows platform drivers for remote 3ds Max deployment.

100% importable and unit-testable on Linux: all ``ctypes.windll`` and ``winreg``
access is guarded at runtime behind ``sys.platform == "win32"`` and injectable
OS/subprocess/registry adapters.
"""

from __future__ import annotations

import configparser
import ctypes
import json
import os
import platform
import re
import shlex
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
import zlib
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Protocol

from remote.drivers.base import DriverBundle
from remote.protocol import PostConfigStepState, SystemStatus

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

SAC_POLICY_SUBKEY = r"SYSTEM\CurrentControlSet\Control\CI\Policy"
SAC_POLICY_VALUE = "VerifiedAndReputablePolicyState"

SAC_STATE_TO_REGISTRY: dict[str, int] = {
    "off": 0,
    "enforced": 1,
    "evaluation": 2,
}
REGISTRY_TO_SAC_STATE: dict[int, str] = {
    0: "off",
    1: "enforced",
    2: "evaluation",
}

SAC_GUIDED_URI = "windowsdefender://smartapp/"
DEFENDER_THREAT_URI = "windowsdefender://threatsettings/"
DEFENDER_EXCLUSIONS_URI = "windowsdefender://exclusions/"

SUPPORTED_MAX_VERSIONS = ("2024", "2025", "2026", "2027", "2028")
VERSION_TO_INTERNAL = {
    "2024": "26.0",
    "2025": "27.0",
    "2026": "28.0",
    "2027": "29.0",
    "2028": "30.0",
}

DEFAULT_POST_CONFIG_STEPS: list[tuple[str, str, str]] = [
    ("renderer_corona", "Install Chaos Corona Renderer", "renderer"),
    ("renderer_vray", "Install Chaos V-Ray Renderer", "renderer"),
    ("ui_theme", "Apply 3ds Max UI Theme", "theme"),
    ("ui_quad_menu", "Install Custom Quad Menus", "quad_menu"),
    ("ui_hotkeys", "Apply Custom Hotkeys", "hotkey"),
    ("scripts_deploy", "Deploy MAXScripts, Macros, Icons & MZP", "scripts"),
    ("verify_install", "Verify 3ds Max Installation & Renderers", "verify"),
]

_MB_OKCANCEL = 0x00000001
_MB_ICONWARNING = 0x00000030
_MB_TOPMOST = 0x00040000
_IDOK = 1
_IDCANCEL = 2

_ICON_EXTS = {".bmp", ".png", ".ico"}


# ---------------------------------------------------------------------------
# Injectable Platform Adapter Protocols & Stdlib Defaults
# ---------------------------------------------------------------------------


class RegistryOps(Protocol):
    def read_dword(self, hive: str, subkey: str, name: str) -> int | None: ...
    def write_dword(self, hive: str, subkey: str, name: str, value: int) -> None: ...
    def read_str(self, hive: str, subkey: str, name: str) -> str | None: ...
    def subkey_exists(self, hive: str, subkey: str) -> bool: ...


class Win32Ops(Protocol):
    def is_user_an_admin(self) -> bool: ...
    def get_session_id(self) -> int: ...
    def message_box(self, title: str, text: str, flags: int = ...) -> int: ...
    def shell_execute(
        self, verb: str, file: str, params: str = "", cwd: str = "", show_cmd: int = 1
    ) -> int: ...
    def capture_screen_png(self, dest_path: Path) -> Path | None: ...


class _DefaultWinRegistry:
    """Stdlib winreg adapter (raises/returns None cleanly on non-Windows)."""

    @staticmethod
    def _hive_const(hive: str) -> int:
        import winreg  # type: ignore[import-not-found]

        mapping = {
            "HKLM": winreg.HKEY_LOCAL_MACHINE,
            "HKEY_LOCAL_MACHINE": winreg.HKEY_LOCAL_MACHINE,
            "HKCU": winreg.HKEY_CURRENT_USER,
            "HKEY_CURRENT_USER": winreg.HKEY_CURRENT_USER,
        }
        if hive.upper() not in mapping:
            raise ValueError(f"Unsupported registry hive: {hive}")
        return mapping[hive.upper()]

    def read_dword(self, hive: str, subkey: str, name: str) -> int | None:
        if sys.platform != "win32":
            return None
        import winreg  # type: ignore[import-not-found]

        try:
            with winreg.OpenKey(self._hive_const(hive), subkey) as key:
                val, _ = winreg.QueryValueEx(key, name)
                return int(val)
        except OSError:
            return None

    def write_dword(self, hive: str, subkey: str, name: str, value: int) -> None:
        if sys.platform != "win32":
            raise PermissionError("Windows Registry is unavailable on non-Windows platforms.")
        import winreg  # type: ignore[import-not-found]

        with winreg.CreateKeyEx(
            self._hive_const(hive),
            subkey,
            0,
            winreg.KEY_SET_VALUE | winreg.KEY_WOW64_64KEY,
        ) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_DWORD, int(value))

    def read_str(self, hive: str, subkey: str, name: str) -> str | None:
        if sys.platform != "win32":
            return None
        import winreg  # type: ignore[import-not-found]

        try:
            with winreg.OpenKey(self._hive_const(hive), subkey) as key:
                val, _ = winreg.QueryValueEx(key, name)
                return str(val)
        except OSError:
            return None

    def subkey_exists(self, hive: str, subkey: str) -> bool:
        if sys.platform != "win32":
            return False
        import winreg  # type: ignore[import-not-found]

        try:
            with winreg.OpenKey(self._hive_const(hive), subkey):
                return True
        except OSError:
            return False


def _encode_png_rgb(width: int, height: int, rgb_rows: bytes) -> bytes:
    """Encode raw top-down 24-bit RGB scanlines into a PNG byte stream using stdlib zlib."""
    stride = width * 3
    raw = bytearray()
    for y in range(height):
        raw.append(0)  # filter type 0 (None)
        start = y * stride
        raw.extend(rgb_rows[start : start + stride])

    def _chunk(tag: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(tag + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    idat = zlib.compress(bytes(raw), level=6)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")


class _DefaultWin32Ops:
    """Stdlib ctypes.windll adapter guarded for non-Windows import safety."""

    def is_user_an_admin(self) -> bool:
        if sys.platform != "win32":
            return hasattr(os, "geteuid") and os.geteuid() == 0
        try:
            return bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            return False

    def get_session_id(self) -> int:
        if sys.platform != "win32":
            return 1
        try:
            pid = ctypes.windll.kernel32.GetCurrentProcessId()
            sid = ctypes.c_ulong(0)
            ok = ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(sid))
            return int(sid.value) if ok else 0
        except Exception:
            return 0

    def message_box(
        self,
        title: str,
        text: str,
        flags: int = _MB_OKCANCEL | _MB_ICONWARNING | _MB_TOPMOST,
    ) -> int:
        if sys.platform != "win32":
            return _IDOK
        try:
            return int(ctypes.windll.user32.MessageBoxW(0, text, title, flags))
        except Exception:
            return _IDCANCEL

    def shell_execute(
        self,
        verb: str,
        file: str,
        params: str = "",
        cwd: str = "",
        show_cmd: int = 1,
    ) -> int:
        if sys.platform != "win32":
            return 0
        try:
            return int(
                ctypes.windll.shell32.ShellExecuteW(
                    None,
                    verb,
                    file,
                    params or None,
                    cwd or None,
                    show_cmd,
                )
            )
        except Exception:
            return 0

    def capture_screen_png(self, dest_path: Path) -> Path | None:
        if sys.platform != "win32":
            return None
        from ctypes import wintypes

        u32 = ctypes.windll.user32
        g32 = ctypes.windll.gdi32
        try:
            x = int(u32.GetSystemMetrics(76))  # SM_XVIRTUALSCREEN
            y = int(u32.GetSystemMetrics(77))  # SM_YVIRTUALSCREEN
            w = int(u32.GetSystemMetrics(78))  # SM_CXVIRTUALSCREEN
            h = int(u32.GetSystemMetrics(79))  # SM_CYVIRTUALSCREEN
            if w <= 0 or h <= 0:
                w = int(u32.GetSystemMetrics(0))
                h = int(u32.GetSystemMetrics(1))
                x, y = 0, 0
            if w <= 0 or h <= 0:
                return None

            hdc_screen = u32.GetDC(0)
            if not hdc_screen:
                return None
            hdc_mem = g32.CreateCompatibleDC(hdc_screen)
            hbmp = g32.CreateCompatibleBitmap(hdc_screen, w, h)
            old = g32.SelectObject(hdc_mem, hbmp)
            try:
                g32.BitBlt(hdc_mem, 0, 0, w, h, hdc_screen, x, y, 0x00CC0020)  # SRCCOPY

                class _BITMAPINFOHEADER(ctypes.Structure):
                    _fields_ = [
                        ("biSize", wintypes.DWORD),
                        ("biWidth", wintypes.LONG),
                        ("biHeight", wintypes.LONG),
                        ("biPlanes", wintypes.WORD),
                        ("biBitCount", wintypes.WORD),
                        ("biCompression", wintypes.DWORD),
                        ("biSizeImage", wintypes.DWORD),
                        ("biXPelsPerMeter", wintypes.LONG),
                        ("biYPelsPerMeter", wintypes.LONG),
                        ("biClrUsed", wintypes.DWORD),
                        ("biClrImportant", wintypes.DWORD),
                    ]

                bmi = _BITMAPINFOHEADER()
                bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
                bmi.biWidth = w
                bmi.biHeight = -h  # top-down
                bmi.biPlanes = 1
                bmi.biBitCount = 24
                bmi.biCompression = 0  # BI_RGB

                row_bytes = ((w * 3 + 3) // 4) * 4
                buf = ctypes.create_string_buffer(row_bytes * h)
                rows = g32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bmi), 0)
                if rows <= 0:
                    return None

                raw = buf.raw
                rgb = bytearray(w * h * 3)
                for row in range(h):
                    src_off = row * row_bytes
                    dst_off = row * w * 3
                    for col in range(w):
                        b = raw[src_off + col * 3]
                        g = raw[src_off + col * 3 + 1]
                        r = raw[src_off + col * 3 + 2]
                        rgb[dst_off + col * 3] = r
                        rgb[dst_off + col * 3 + 1] = g
                        rgb[dst_off + col * 3 + 2] = b

                png_bytes = _encode_png_rgb(w, h, bytes(rgb))
                dest_path = Path(dest_path)
                dest_path.parent.mkdir(parents=True, exist_ok=True)
                dest_path.write_bytes(png_bytes)
                return dest_path
            finally:
                g32.SelectObject(hdc_mem, old)
                g32.DeleteObject(hbmp)
                g32.DeleteDC(hdc_mem)
                u32.ReleaseDC(0, hdc_screen)
        except Exception:
            return None


def _default_run_cmd(
    args: list[str],
    *,
    timeout: float | None = 30.0,
    cwd: str | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=cwd,
        check=False,
    )


def _atomic_write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp")
    payload = json.dumps(data, indent=2, ensure_ascii=False)
    with open(tmp_path, "w", encoding="utf-8") as fh:
        fh.write(payload)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp_path, path)


def _ps_quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _terminate_process_tree(proc: Any) -> None:
    pid = getattr(proc, "pid", None)
    if sys.platform == "win32" and pid:
        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=10,
                check=False,
            )
            return
        except Exception:
            pass
    try:
        proc.terminate()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# WindowsSystemDriver (F-12, F-13, F-14, Screenshot)
# ---------------------------------------------------------------------------


class WindowsSystemDriver:
    """Windows OS inspection, UAC elevation, Smart App Control, Defender, and screenshots."""

    def __init__(
        self,
        *,
        data_dir: Path | None = None,
        repo_root: Path | None = None,
        program_files: Path | None = None,
        disk_probe_path: Path | None = None,
        win32_ops: Win32Ops | None = None,
        reg_ops: RegistryOps | None = None,
        run_cmd: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        prompt_before_uac: bool = True,
    ) -> None:
        self._repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent.parent
        default_data = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MaxAgent" / "data"
        self._data_dir = Path(data_dir) if data_dir else default_data
        self._program_files = Path(program_files) if program_files else Path(
            os.environ.get("ProgramFiles", r"C:\Program Files")
        )
        self._disk_probe_path = Path(disk_probe_path) if disk_probe_path else self._data_dir
        self._win32: Win32Ops = win32_ops or _DefaultWin32Ops()
        self._reg: RegistryOps = reg_ops or _DefaultWinRegistry()
        self._run_cmd = run_cmd or _default_run_cmd
        self._prompt_before_uac = prompt_before_uac

    def _ledger_path(self) -> Path:
        return self._data_dir / "defender_exclusions_ledger.json"

    def _read_exclusion_ledger(self) -> list[str]:
        path = self._ledger_path()
        if not path.is_file():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, list):
                return [str(x) for x in raw if str(x).strip()]
        except Exception:
            pass
        return []

    def _write_exclusion_ledger(self, paths: list[str]) -> None:
        deduped: list[str] = []
        seen: set[str] = set()
        for p in paths:
            norm = os.path.normpath(p)
            if norm.lower() not in seen:
                seen.add(norm.lower())
                deduped.append(norm)
        _atomic_write_json(self._ledger_path(), deduped)

    def _detect_installed_max_versions(self) -> list[str]:
        found: list[str] = []
        for ver in SUPPORTED_MAX_VERSIONS:
            exe = self._program_files / "Autodesk" / f"3ds Max {ver}" / "3dsmax.exe"
            if exe.is_file():
                found.append(ver)
                continue
            internal = VERSION_TO_INTERNAL.get(ver)
            if internal:
                inst_dir = self._reg.read_str("HKLM", rf"SOFTWARE\Autodesk\3dsMax\{internal}", "Installdir")
                if inst_dir and (Path(inst_dir) / "3dsmax.exe").is_file():
                    found.append(ver)
        return found

    def _is_ahk_available(self) -> bool:
        for env_key in ("MAX_AUTO_AHK_EXE", "MAX_AGENT_AHK_EXE", "AHK_EXE"):
            env_ahk = os.environ.get(env_key)
            if env_ahk and Path(env_ahk).is_file():
                return True
        exe_dir = Path(sys.executable).resolve().parent
        candidates = [
            self._repo_root / "ahk" / "AutoHotkey64.exe",
            self._repo_root / "tools" / "AutoHotkey64.exe",
            self._repo_root / "AutoHotkey64.exe",
            exe_dir / "AutoHotkey64.exe",
            exe_dir / "ahk" / "AutoHotkey64.exe",
            exe_dir / "tools" / "AutoHotkey64.exe",
            exe_dir.parent / "ahk" / "AutoHotkey64.exe",
            exe_dir.parent / "tools" / "AutoHotkey64.exe",
            Path(r"C:\Program Files\AutoHotkey\v2\AutoHotkey64.exe"),
        ]
        if any(c.is_file() for c in candidates):
            return True
        return bool(shutil.which("AutoHotkey64.exe") or shutil.which("AutoHotkey.exe"))

    def collect_status(self, agent_version: str, runtime_version: str) -> SystemStatus:
        sac = self.get_smart_app_control_status()
        defender = self.get_defender_status()
        probe = self._disk_probe_path
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            free_bytes = int(shutil.disk_usage(probe).free)
        except OSError:
            free_bytes = 0

        return SystemStatus(
            hostname=socket.gethostname() or "WINDOWS-CLIENT",
            os_version=platform.platform(),
            is_admin=bool(self._win32.is_user_an_admin()),
            session_id=max(0, int(self._win32.get_session_id())),
            smart_app_control_state=str(sac.get("state", "unknown")),
            smart_app_control_reboot_required=bool(sac.get("reboot_required", False)),
            defender_rtp_enabled=defender.get("rtp_enabled"),
            defender_tamper_protected=defender.get("tamper_protected"),
            defender_exclusions=list(defender.get("exclusions", [])),
            third_party_av=list(defender.get("third_party_av", [])),
            free_disk_bytes=free_bytes,
            installed_max_versions=self._detect_installed_max_versions(),
            ahk_available=self._is_ahk_available(),
            agent_version=agent_version,
            runtime_version=runtime_version,
        )

    def request_elevation(
        self, executable: str, args: list[str], cwd: str
    ) -> dict[str, Any]:
        session_id = int(self._win32.get_session_id())
        if session_id <= 0:
            return {
                "ok": False,
                "elevated": False,
                "already_admin": False,
                "is_admin": False,
                "session_id": session_id,
                "detail": "Interactive desktop session (SessionId > 0) required for UAC elevation.",
            }

        if self._win32.is_user_an_admin():
            return {
                "ok": True,
                "elevated": True,
                "already_admin": True,
                "is_admin": True,
                "session_id": session_id,
                "executable": executable,
                "args": list(args),
                "cwd": cwd,
                "method": "already_admin",
                "detail": "Already running with Administrator privileges.",
            }

        if self._prompt_before_uac:
            choice = self._win32.message_box(
                "Administrator Privileges Required",
                (
                    "MaxAgent requires Administrator privileges to install Autodesk 3ds Max "
                    "and configure system components.\n\n"
                    "Click OK to continue to the Windows UAC prompt."
                ),
                _MB_OKCANCEL | _MB_ICONWARNING | _MB_TOPMOST,
            )
            if choice == _IDCANCEL:
                return {
                    "ok": False,
                    "elevated": False,
                    "already_admin": False,
                    "is_admin": False,
                    "cancelled": True,
                    "session_id": session_id,
                    "detail": "Elevation cancelled by user at pre-UAC prompt.",
                }

        params = subprocess.list2cmdline(list(args)) if args else ""
        hinst = int(self._win32.shell_execute("runas", executable, params, cwd, 1))
        if hinst > 32:
            return {
                "ok": True,
                "elevated": True,
                "already_admin": False,
                "is_admin": False,
                "session_id": session_id,
                "executable": executable,
                "args": list(args),
                "cwd": cwd,
                "method": "ShellExecuteW_runas",
                "hinstance": hinst,
                "detail": "Elevated process launched via UAC runas.",
            }
        return {
            "ok": False,
            "elevated": False,
            "already_admin": False,
            "is_admin": False,
            "session_id": session_id,
            "hinstance": hinst,
            "detail": f"ShellExecuteW(runas) failed with code {hinst}.",
        }

    def get_smart_app_control_status(self) -> dict[str, Any]:
        reg_val = self._reg.read_dword("HKLM", SAC_POLICY_SUBKEY, SAC_POLICY_VALUE)
        state = REGISTRY_TO_SAC_STATE.get(reg_val, "unknown") if reg_val is not None else "unknown"

        ci_enforced = state == "enforced"
        ci_active_sac = False
        try:
            res = self._run_cmd(["CiTool.exe", "-lp", "-json"], timeout=5.0)
            if res.returncode == 0 and res.stdout.strip():
                parsed = json.loads(res.stdout)
                policies = parsed.get("Policies", []) if isinstance(parsed, dict) else []
                for pol in policies:
                    if not isinstance(pol, dict):
                        continue
                    fname = str(pol.get("FriendlyName", "")).lower()
                    is_enf = bool(pol.get("IsEnforced", False))
                    is_auth = bool(pol.get("IsAuthorized", False))
                    if ("smart app" in fname or "verified" in fname) and (is_enf or is_auth):
                        ci_active_sac = True
                        if is_enf:
                            ci_enforced = True
        except Exception:
            pass

        reboot_required = (reg_val == 0 and ci_active_sac)
        return {
            "state": state,
            "registry_value": reg_val,
            "reboot_required": reboot_required,
            "ci_enforced": ci_enforced,
            "guided_uri": SAC_GUIDED_URI,
            "source": "windows",
        }

    def disable_smart_app_control(self, mode: str = "guided") -> dict[str, Any]:
        normalized = (mode or "guided").strip().lower()
        if normalized not in ("guided", "scripted"):
            raise ValueError(f"Unsupported Smart App Control mode: {mode!r}")

        current = self.get_smart_app_control_status()
        warning = "Smart App Control cannot be re-enabled without reinstalling Windows."
        if current["state"] == "off" and not current["reboot_required"]:
            return {
                "ok": True,
                "changed": False,
                "mode": normalized,
                "state": "off",
                "registry_value": current["registry_value"],
                "reboot_required": False,
                "guided_uri": SAC_GUIDED_URI,
                "warning": warning,
                "detail": "Smart App Control is already Off.",
            }

        if normalized == "guided":
            opened = False
            if self._win32.get_session_id() > 0:
                hinst = self._win32.shell_execute("open", SAC_GUIDED_URI, "", "", 1)
                opened = hinst > 32
            post = self.get_smart_app_control_status()
            return {
                "ok": True,
                "changed": False,
                "mode": "guided",
                "opened_settings": opened,
                "state": post["state"],
                "registry_value": post["registry_value"],
                "reboot_required": post["reboot_required"],
                "guided_uri": SAC_GUIDED_URI,
                "warning": warning,
                "detail": "Opened Windows Security Smart App Control settings for guided disable.",
            }

        # scripted mode
        if not self._win32.is_user_an_admin():
            return {
                "ok": False,
                "changed": False,
                "mode": "scripted",
                "state": current["state"],
                "registry_value": current["registry_value"],
                "reboot_required": False,
                "guided_uri": SAC_GUIDED_URI,
                "detail": "Administrator privileges required for scripted Smart App Control change.",
            }

        try:
            self._reg.write_dword("HKLM", SAC_POLICY_SUBKEY, SAC_POLICY_VALUE, 0)
        except (PermissionError, OSError) as exc:
            return {
                "ok": False,
                "changed": False,
                "mode": "scripted",
                "state": current["state"],
                "registry_value": current["registry_value"],
                "reboot_required": current["state"] == "enforced",
                "guided_uri": SAC_GUIDED_URI,
                "guided_fallback": True,
                "detail": f"Registry write blocked by OS/CI protection ({exc}); use guided UI.",
            }

        try:
            self._run_cmd(["CiTool.exe", "-r"], timeout=10.0)
        except Exception:
            pass

        post = self.get_smart_app_control_status()
        return {
            "ok": True,
            "changed": True,
            "mode": "scripted",
            "state": post["state"],
            "registry_value": post["registry_value"],
            "reboot_required": True,
            "guided_uri": SAC_GUIDED_URI,
            "warning": warning,
            "detail": "Smart App Control policy set to Off (0); reboot required to complete enforcement change.",
        }

    def _run_ps_json(self, ps_script: str, timeout: float = 15.0) -> Any:
        try:
            res = self._run_cmd(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    ps_script,
                ],
                timeout=timeout,
            )
            if res.returncode != 0 or not res.stdout.strip():
                return None
            return json.loads(res.stdout)
        except Exception:
            return None

    def get_defender_status(self) -> dict[str, Any]:
        comp = self._run_ps_json(
            "Get-MpComputerStatus | Select-Object AntivirusEnabled,AMServiceEnabled,"
            "RealTimeProtectionEnabled,IsTamperProtected | ConvertTo-Json -Compress"
        )
        antivirus_enabled = None
        am_service_enabled = None
        rtp_enabled = None
        tamper_protected = None
        if isinstance(comp, dict):
            antivirus_enabled = comp.get("AntivirusEnabled")
            am_service_enabled = comp.get("AMServiceEnabled")
            rtp_enabled = comp.get("RealTimeProtectionEnabled")
            tamper_protected = comp.get("IsTamperProtected")

        raw_excl = self._run_ps_json("(Get-MpPreference).ExclusionPath | ConvertTo-Json -Compress")
        exclusions: list[str] = []
        if isinstance(raw_excl, str) and raw_excl.strip():
            exclusions = [raw_excl.strip()]
        elif isinstance(raw_excl, list):
            exclusions = [str(x).strip() for x in raw_excl if str(x).strip()]

        raw_av = self._run_ps_json(
            r"Get-CimInstance -Namespace root\SecurityCenter2 -ClassName AntiVirusProduct "
            r"| Select-Object displayName,productState | ConvertTo-Json -Compress"
        )
        third_party_av: list[str] = []
        av_items = [raw_av] if isinstance(raw_av, dict) else (raw_av if isinstance(raw_av, list) else [])
        for item in av_items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("displayName", "")).strip()
            if name and "windows defender" not in name.lower() and name not in third_party_av:
                third_party_av.append(name)

        return {
            "antivirus_enabled": antivirus_enabled,
            "am_service_enabled": am_service_enabled,
            "rtp_enabled": rtp_enabled,
            "tamper_protected": tamper_protected,
            "exclusions": exclusions,
            "third_party_av": third_party_av,
            "guided_uri": DEFENDER_THREAT_URI,
            "exclusions_uri": DEFENDER_EXCLUSIONS_URI,
            "source": "windows",
        }

    def configure_defender(
        self, action: str, exclusion_paths: list[str] | None = None
    ) -> dict[str, Any]:
        act = (action or "").strip().lower()
        if act not in ("open_settings", "add_exclusions", "disable", "restore"):
            raise ValueError(f"Unsupported Defender action: {action!r}")

        if act == "open_settings":
            opened = False
            if self._win32.get_session_id() > 0:
                opened = self._win32.shell_execute("open", DEFENDER_THREAT_URI, "", "", 1) > 32
            status = self.get_defender_status()
            return {
                "ok": True,
                "action": act,
                "opened_settings": opened,
                "guided_uri": DEFENDER_THREAT_URI,
                **status,
            }

        if not self._win32.is_user_an_admin():
            status = self.get_defender_status()
            return {
                "ok": False,
                "action": act,
                "guided_fallback": True,
                "detail": "Administrator privileges required to modify Windows Defender.",
                **status,
            }

        expanded_targets = [
            os.path.normpath(os.path.expandvars(p))
            for p in (exclusion_paths or [])
            if str(p).strip()
        ]

        if act == "add_exclusions":
            if expanded_targets:
                ps_arr = "@(" + ", ".join(_ps_quote(p) for p in expanded_targets) + ")"
                res = self._run_cmd(
                    [
                        "powershell.exe",
                        "-NoProfile",
                        "-NonInteractive",
                        "-ExecutionPolicy",
                        "Bypass",
                        "-Command",
                        f"Add-MpPreference -ExclusionPath {ps_arr}",
                    ],
                    timeout=20.0,
                )
                if res.returncode != 0:
                    status = self.get_defender_status()
                    return {
                        "ok": False,
                        "action": act,
                        "detail": (res.stderr or res.stdout or "Add-MpPreference failed").strip(),
                        **status,
                    }
                ledger = self._read_exclusion_ledger()
                ledger.extend(expanded_targets)
                self._write_exclusion_ledger(ledger)
            status = self.get_defender_status()
            return {
                "ok": True,
                "action": act,
                "added_exclusions": expanded_targets,
                "detail": f"Added {len(expanded_targets)} Defender exclusion(s).",
                **status,
            }

        if act == "disable":
            if expanded_targets:
                add_res = self.configure_defender("add_exclusions", expanded_targets)
                if not add_res.get("ok"):
                    return add_res

            pre = self.get_defender_status()
            if pre.get("tamper_protected") is True:
                return {
                    "ok": False,
                    "action": act,
                    "guided_fallback": True,
                    "detail": (
                        "Tamper Protection is enabled; Real-Time Protection cannot be disabled "
                        "programmatically. Open Windows Security to turn it off manually."
                    ),
                    **pre,
                }

            self._run_cmd(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    "Set-MpPreference -DisableRealtimeMonitoring $true",
                ],
                timeout=20.0,
            )
            post = self.get_defender_status()
            if post.get("rtp_enabled") is not False:
                return {
                    "ok": False,
                    "action": act,
                    "guided_fallback": True,
                    "detail": (
                        "Real-Time Protection remained active after Set-MpPreference "
                        "(likely blocked by Tamper Protection or policy)."
                    ),
                    **post,
                }
            return {
                "ok": True,
                "action": act,
                "guided_fallback": False,
                "detail": "Windows Defender Real-Time Protection disabled.",
                **post,
            }

        # act == "restore"
        ledger = self._read_exclusion_ledger()
        targets = expanded_targets if expanded_targets else ledger
        failed_cmds: list[str] = []
        removed_exclusions: list[str] = []
        if targets:
            ps_arr = "@(" + ", ".join(_ps_quote(p) for p in targets) + ")"
            rem_res = self._run_cmd(
                [
                    "powershell.exe",
                    "-NoProfile",
                    "-NonInteractive",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-Command",
                    f"Remove-MpPreference -ExclusionPath {ps_arr}",
                ],
                timeout=20.0,
            )
            if rem_res.returncode == 0:
                target_lowers = {t.lower() for t in targets}
                remaining = [p for p in ledger if p.lower() not in target_lowers]
                self._write_exclusion_ledger(remaining)
                removed_exclusions = list(targets)
            else:
                failed_cmds.append(f"Remove-MpPreference rc={rem_res.returncode}")

        rtp_res = self._run_cmd(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                "Set-MpPreference -DisableRealtimeMonitoring $false",
            ],
            timeout=20.0,
        )
        if rtp_res.returncode != 0:
            failed_cmds.append(f"Set-MpPreference rc={rtp_res.returncode}")

        post = self.get_defender_status()
        rtp_ok = post.get("rtp_enabled") is True
        ok = (not failed_cmds) and rtp_ok
        detail = (
            "Restored Windows Defender Real-Time Protection and removed recorded exclusions."
            if ok
            else f"Failed to restore Windows Defender ({'; '.join(failed_cmds) or 'RTP still disabled'})."
        )
        return {
            "ok": ok,
            "action": act,
            "removed_exclusions": removed_exclusions,
            "detail": detail,
            **post,
        }

    def capture_screenshot(self, dest_path: Path) -> Path | None:
        return self._win32.capture_screen_png(Path(dest_path))


# ---------------------------------------------------------------------------
# WindowsInstallerDriver (F-15)
# ---------------------------------------------------------------------------


class WindowsInstallerDriver:
    """Invokes vm/Install-3dsMax.ps1 -QuietConsole and tails status file."""

    def __init__(
        self,
        *,
        script_path: Path | None = None,
        program_files: Path | None = None,
        reg_ops: RegistryOps | None = None,
        popen_fn: Callable[..., Any] | None = None,
        run_cmd: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        poll_interval_sec: float = 0.5,
    ) -> None:
        repo_root = Path(__file__).resolve().parent.parent.parent
        self._script_path = Path(script_path) if script_path else repo_root / "vm" / "Install-3dsMax.ps1"
        self._program_files = Path(program_files) if program_files else Path(
            os.environ.get("ProgramFiles", r"C:\Program Files")
        )
        self._reg: RegistryOps = reg_ops or _DefaultWinRegistry()
        self._popen_fn = popen_fn or subprocess.Popen
        self._run_cmd = run_cmd or _default_run_cmd
        self._poll_interval_sec = poll_interval_sec

    def is_max_installed(self, version: str) -> bool:
        ver = str(version)
        exe = self._program_files / "Autodesk" / f"3ds Max {ver}" / "3dsmax.exe"
        if exe.is_file():
            return True
        internal = VERSION_TO_INTERNAL.get(ver)
        if internal:
            inst_dir = self._reg.read_str("HKLM", rf"SOFTWARE\Autodesk\3dsMax\{internal}", "Installdir")
            if inst_dir and (Path(inst_dir) / "3dsmax.exe").is_file():
                return True
        return False

    def is_installer_running(self) -> bool:
        if sys.platform == "win32":
            try:
                res = self._run_cmd(["tasklist", "/FO", "CSV", "/NH"], timeout=5.0)
                for line in res.stdout.splitlines():
                    name = line.split(",", 1)[0].strip().strip('"').lower()
                    if name in ("setup.exe", "adskinstaller.exe"):
                        return True
            except Exception:
                pass
        return False

    @staticmethod
    def _parse_status_line(text: str) -> tuple[int, str] | None:
        for line in reversed(text.splitlines()):
            stripped = line.strip().lstrip("\ufeff")
            if "|" not in stripped:
                continue
            pct_raw, _, msg = stripped.partition("|")
            pct_str = pct_raw.strip().rstrip("%")
            try:
                pct = max(0, min(100, int(pct_str)))
                return pct, msg.strip()
            except ValueError:
                continue
        return None

    def install_3dsmax(
        self,
        version: str,
        iso_path: Path,
        *,
        progress_cb: Callable[[int, str], None],
        log_cb: Callable[[str, str], None],
        cancel_event: threading.Event,
        timeout_sec: int = 2400,
    ) -> dict[str, Any]:
        ver = str(version)
        iso = Path(iso_path)

        if cancel_event.is_set():
            log_cb("WARN", f"3ds Max {ver} installation cancelled before start.")
            return {"ok": False, "status": "aborted", "detail": "Cancelled before start."}

        if not iso.is_file():
            msg = f"ISO file not found: {iso}"
            log_cb("ERROR", msg)
            return {"ok": False, "status": "failed", "detail": msg}

        if self.is_max_installed(ver):
            msg = f"Autodesk 3ds Max {ver} is already installed."
            progress_cb(100, msg)
            log_cb("INFO", msg)
            return {"ok": True, "status": "completed", "already_installed": True, "detail": msg}

        status_file = iso.parent / "logs" / f"3dsMax-{ver}-status.txt"
        cmd = [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(self._script_path),
            "-Version",
            ver,
            "-IsoPath",
            str(iso),
            "-InstallerTimeoutSeconds",
            str(timeout_sec),
            "-StatusRefreshSeconds",
            "2",
            "-QuietConsole",
        ]
        log_cb("INFO", f"Launching installer: {' '.join(cmd)}")
        proc = self._popen_fn(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def _drain_output() -> None:
            stream = getattr(proc, "stdout", None)
            if stream is None:
                return
            try:
                for line in stream:
                    cleaned = line.rstrip("\r\n")
                    if cleaned:
                        log_cb("INFO", cleaned)
            except Exception:
                pass

        reader = threading.Thread(target=_drain_output, daemon=True)
        reader.start()

        last_progress: tuple[int, str] | None = None
        deadline = time.monotonic() + max(1, int(timeout_sec))

        while True:
            if status_file.is_file():
                try:
                    parsed = self._parse_status_line(status_file.read_text(encoding="utf-8", errors="ignore"))
                    if parsed is not None and parsed != last_progress:
                        last_progress = parsed
                        progress_cb(parsed[0], parsed[1])
                except OSError:
                    pass

            if cancel_event.is_set():
                _terminate_process_tree(proc)
                reader.join(timeout=2.0)
                log_cb("WARN", f"3ds Max {ver} installation aborted by cancel_event.")
                return {"ok": False, "status": "aborted", "detail": "Installation cancelled."}

            rc = proc.poll()
            if rc is not None:
                reader.join(timeout=2.0)
                if status_file.is_file():
                    try:
                        parsed = self._parse_status_line(
                            status_file.read_text(encoding="utf-8", errors="ignore")
                        )
                        if parsed is not None and parsed != last_progress:
                            last_progress = parsed
                            progress_cb(parsed[0], parsed[1])
                    except OSError:
                        pass

                if rc == 0:
                    if last_progress is None or last_progress[0] < 100:
                        progress_cb(100, f"Autodesk 3ds Max {ver} installed.")
                    return {
                        "ok": True,
                        "status": "completed",
                        "exit_code": 0,
                        "detail": (last_progress[1] if last_progress else f"3ds Max {ver} installed."),
                    }
                detail = (
                    last_progress[1]
                    if last_progress
                    else f"Install-3dsMax.ps1 exited with code {rc}."
                )
                log_cb("ERROR", detail)
                return {
                    "ok": False,
                    "status": "failed",
                    "exit_code": rc,
                    "detail": detail,
                }

            if time.monotonic() >= deadline:
                _terminate_process_tree(proc)
                reader.join(timeout=2.0)
                detail = f"Installer timed out after {timeout_sec}s."
                log_cb("ERROR", detail)
                return {"ok": False, "status": "timeout", "detail": detail}

            time.sleep(self._poll_interval_sec)


# ---------------------------------------------------------------------------
# WindowsFirstRunDriver (F-16)
# ---------------------------------------------------------------------------


class WindowsFirstRunDriver:
    """Invokes `python -u -m cli.main run --profile client` and parses WatchResult JSON."""

    def __init__(
        self,
        *,
        python_exe: str | Path | None = None,
        ahk_exe: str | Path | None = None,
        rules_config: Path | None = None,
        repo_root: Path | None = None,
        popen_fn: Callable[..., Any] | None = None,
        poll_interval_sec: float = 0.25,
    ) -> None:
        self._repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent.parent
        self._python_exe = str(python_exe or sys.executable)
        self._ahk_exe = str(ahk_exe) if ahk_exe else None
        self._rules_config = Path(rules_config) if rules_config else None
        self._popen_fn = popen_fn or subprocess.Popen
        self._poll_interval_sec = poll_interval_sec

    def run_first_run(
        self,
        version: str,
        *,
        log_cb: Callable[[str, str], None],
        cancel_event: threading.Event,
        timeout_sec: int = 300,
        dump_dir: Path | None = None,
    ) -> dict[str, Any]:
        ver = str(version)
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", ver) or ".." in ver:
            raise ValueError(f"Unsafe 3ds Max version: {version!r}")
        if cancel_event.is_set():
            log_cb("WARN", f"First-run automation for 3ds Max {ver} cancelled before start.")
            return {
                "status": "aborted",
                "detail": "First-run cancelled.",
                "matched_rules": [],
                "screenshots": [],
            }

        target_dump_dir = Path(dump_dir) if dump_dir else Path(tempfile.mkdtemp(prefix=f"max_firstrun_{ver}_"))
        target_dump_dir.mkdir(parents=True, exist_ok=True)
        json_result_path = target_dump_dir / f"first_run_{ver}_result.json"

        cmd = [
            self._python_exe,
            "-u",
            "-m",
            "cli.main",
            "run",
            "--version",
            ver,
            "--profile",
            "client",
            "--max-timeout",
            str(timeout_sec),
            "--dump-dir",
            str(target_dump_dir),
            "--json-result",
            str(json_result_path),
        ]
        if self._ahk_exe:
            cmd.extend(["--ahk-exe", self._ahk_exe])
        if self._rules_config:
            cmd.extend(["--config", str(self._rules_config)])

        log_cb("INFO", f"Starting first-run watcher: {' '.join(cmd)}")
        proc = self._popen_fn(
            cmd,
            cwd=str(self._repo_root),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        def _drain() -> None:
            stream = getattr(proc, "stdout", None)
            if stream is None:
                return
            try:
                for line in stream:
                    cleaned = line.rstrip("\r\n")
                    if cleaned:
                        log_cb("INFO", cleaned)
            except Exception:
                pass

        reader = threading.Thread(target=_drain, daemon=True)
        reader.start()

        deadline = time.monotonic() + max(5, int(timeout_sec) + 30)
        while True:
            if cancel_event.is_set():
                _terminate_process_tree(proc)
                reader.join(timeout=2.0)
                return {
                    "status": "aborted",
                    "detail": "First-run cancelled.",
                    "matched_rules": [],
                    "screenshots": [],
                }

            rc = proc.poll()
            if rc is not None:
                reader.join(timeout=2.0)
                if json_result_path.is_file():
                    try:
                        parsed = json.loads(json_result_path.read_text(encoding="utf-8"))
                        if isinstance(parsed, dict) and "status" in parsed:
                            return {
                                "status": str(parsed.get("status", "failed")),
                                "detail": str(parsed.get("detail", "")),
                                "matched_rules": list(parsed.get("matched_rules", [])),
                                "screenshots": list(parsed.get("screenshots", [])),
                            }
                    except Exception:
                        pass

                if rc == 0:
                    return {
                        "status": "ready",
                        "detail": f"Autodesk 3ds Max {ver} main workspace ready.",
                        "matched_rules": [],
                        "screenshots": [],
                    }
                if rc == 10:
                    return {
                        "status": "licensing_required",
                        "detail": f"Autodesk 3ds Max {ver} requires manual licensing/sign-in.",
                        "matched_rules": [],
                        "screenshots": [],
                    }
                return {
                    "status": "failed",
                    "detail": f"First-run watcher exited with code {rc}.",
                    "matched_rules": [],
                    "screenshots": [],
                }

            if time.monotonic() >= deadline:
                _terminate_process_tree(proc)
                reader.join(timeout=2.0)
                return {
                    "status": "timeout",
                    "detail": f"First-run watcher timed out after {timeout_sec}s.",
                    "matched_rules": [],
                    "screenshots": [],
                }

            time.sleep(self._poll_interval_sec)


# ---------------------------------------------------------------------------
# WindowsPostConfigDriver (F-17)
# ---------------------------------------------------------------------------


class WindowsPostConfigDriver:
    """1-click declarative POST_CONFIG driver for renderers, UI, scripts, and verification."""

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        program_files: Path | None = None,
        local_app_data: Path | None = None,
        data_dir: Path | None = None,
        reg_ops: RegistryOps | None = None,
        run_cmd: Callable[..., subprocess.CompletedProcess[str]] | None = None,
        popen_fn: Callable[..., Any] | None = None,
    ) -> None:
        self._repo_root = Path(repo_root) if repo_root else Path(__file__).resolve().parent.parent.parent
        self._program_files = Path(program_files) if program_files else Path(
            os.environ.get("ProgramFiles", r"C:\Program Files")
        )
        self._local_app_data_override = Path(local_app_data) if local_app_data else None
        default_data = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "MaxAgent" / "data"
        self._data_dir = Path(data_dir) if data_dir else default_data
        self._reg: RegistryOps = reg_ops or _DefaultWinRegistry()
        self._run_cmd = run_cmd or _default_run_cmd
        self._popen_fn = popen_fn or subprocess.Popen

    def _local_app_data(self) -> Path:
        if self._local_app_data_override is not None:
            return self._local_app_data_override
        env_lad = os.environ.get("LOCALAPPDATA")
        if env_lad:
            return Path(env_lad)
        return Path.home() / "AppData" / "Local"

    def _enu_dir(self, version: str) -> Path:
        ver = str(version)
        if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$", ver) or ".." in ver:
            raise ValueError(f"Unsafe 3ds Max version: {version!r}")
        return self._local_app_data() / "Autodesk" / "3dsMax" / f"{ver} - 64bit" / "ENU"

    def _backup_dir(self, version: str) -> Path:
        return self._enu_dir(version).parent / "ENU_backup"

    def _plugins_dir(self, version: str) -> Path:
        return self._program_files / "Autodesk" / f"3ds Max {version}" / "plugins"

    def _max_exe(self, version: str) -> Path:
        default = self._program_files / "Autodesk" / f"3ds Max {version}" / "3dsmax.exe"
        if default.is_file():
            return default
        internal = VERSION_TO_INTERNAL.get(str(version))
        if internal:
            inst_dir = self._reg.read_str("HKLM", rf"SOFTWARE\Autodesk\3dsMax\{internal}", "Installdir")
            if inst_dir:
                cand = Path(inst_dir) / "3dsmax.exe"
                if cand.is_file():
                    return cand
        return default

    def _verify_json_path(self, version: str) -> Path:
        return self._enu_dir(version).parent / "max_verify.json"

    @staticmethod
    def _parse_steps_yaml_stdlib(text: str) -> list[dict[str, Any]]:
        """Parse simple YAML `steps:` entries using Python stdlib only."""
        steps: list[dict[str, Any]] = []
        in_steps = False
        current: dict[str, Any] | None = None
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            indent = len(raw_line) - len(raw_line.lstrip(" "))
            if indent == 0 and stripped.endswith(":"):
                in_steps = stripped == "steps:"
                current = None
                continue
            if not in_steps:
                continue
            if stripped.startswith("- "):
                current = {}
                steps.append(current)
                stripped = stripped[2:].strip()
                if not stripped:
                    continue
            if current is None or ":" not in stripped:
                continue
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip()
            if not val:
                continue
            if val.startswith("[") and val.endswith("]"):
                current[key] = [
                    item.strip().strip("'\"")
                    for item in val[1:-1].split(",")
                    if item.strip()
                ]
            elif val.lower() in ("true", "false"):
                current[key] = val.lower() == "true"
            else:
                current[key] = val.strip("'\"")
        return steps

    def _load_raw_step_configs(
        self, config_path: Path | None = None, version: str | None = None
    ) -> list[dict[str, Any]]:
        cfg = Path(config_path) if config_path is not None else (self._repo_root / "config" / "post_config.yaml")
        raw_entries: list[dict[str, Any]] = []
        if cfg.is_file():
            text = cfg.read_text(encoding="utf-8")
            if cfg.suffix.lower() == ".json":
                parsed = json.loads(text)
                if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                    raw_entries = [e for e in parsed["steps"] if isinstance(e, dict)]
            else:
                try:
                    import yaml  # type: ignore[import-not-found]

                    parsed = yaml.safe_load(text)
                    if isinstance(parsed, dict) and isinstance(parsed.get("steps"), list):
                        raw_entries = [e for e in parsed["steps"] if isinstance(e, dict)]
                except ImportError:
                    raw_entries = self._parse_steps_yaml_stdlib(text)
                except Exception:
                    raw_entries = self._parse_steps_yaml_stdlib(text)

        if not raw_entries:
            return [
                {"step_id": sid, "name": name, "category": cat}
                for sid, name, cat in DEFAULT_POST_CONFIG_STEPS
            ]

        filtered: list[dict[str, Any]] = []
        for entry in raw_entries:
            if entry.get("enabled") is False:
                continue
            allowed = entry.get("versions")
            if (
                version is not None
                and isinstance(allowed, list)
                and str(version) not in {str(v) for v in allowed}
            ):
                continue
            filtered.append(entry)
        return filtered

    def load_steps(
        self, config_path: Path | None = None, version: str | None = None
    ) -> list[PostConfigStepState]:
        entries = self._load_raw_step_configs(config_path=config_path, version=version)
        steps: list[PostConfigStepState] = []
        for entry in entries:
            step_id = entry.get("step_id") or entry.get("id")
            name = entry.get("name")
            category = entry.get("category")
            if step_id and name and category:
                steps.append(
                    PostConfigStepState(
                        step_id=str(step_id),
                        name=str(name),
                        category=str(category),
                        status="pending",
                        detail="",
                    )
                )
        return steps

    def _close_3dsmax_if_running(self, log_cb: Callable[[str, str], None]) -> None:
        if sys.platform != "win32":
            return
        try:
            res = self._run_cmd(["tasklist", "/FI", "IMAGENAME eq 3dsmax.exe", "/NH"], timeout=5.0)
            if "3dsmax.exe" in res.stdout.lower():
                log_cb("INFO", "Closing running 3dsmax.exe before post-config...")
                self._run_cmd(["taskkill", "/IM", "3dsmax.exe", "/T", "/F"], timeout=10.0)
        except Exception:
            pass

    def _ensure_enu_and_backup(self, version: str) -> tuple[Path, Path]:
        enu_dir = self._enu_dir(version)
        backup_dir = self._backup_dir(version)
        (enu_dir / "en-US" / "UI" / "UserSettings").mkdir(parents=True, exist_ok=True)
        (enu_dir / "scripts" / "startup").mkdir(parents=True, exist_ok=True)
        (enu_dir / "usermacros").mkdir(parents=True, exist_ok=True)
        (enu_dir / "usericons").mkdir(parents=True, exist_ok=True)
        ini_path = enu_dir / "3dsmax.ini"
        if not ini_path.exists():
            ini_path.write_text("[Directories]\n[UI]\n", encoding="utf-8")
        if not backup_dir.exists():
            shutil.copytree(enu_dir, backup_dir)
        return enu_dir, backup_dir

    @staticmethod
    def _validate_zip_member(member_name: str, target_root: Path) -> Path:
        normalized = member_name.replace("\\", "/")
        if not normalized or normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
            raise ValueError(f"Unsafe zip entry path: {member_name!r}")
        parts = PurePosixPath(normalized).parts
        if ".." in parts:
            raise ValueError(f"Unsafe zip traversal path: {member_name!r}")
        resolved_root = target_root.resolve()
        dest = (resolved_root / Path(*parts)).resolve()
        dest.relative_to(resolved_root)
        return dest

    def _safe_extract_mzp(self, mzp_path: Path, target_dir: Path) -> list[Path]:
        target_dir.mkdir(parents=True, exist_ok=True)
        extracted: list[Path] = []
        with zipfile.ZipFile(mzp_path, "r") as zf:
            validated = [
                (info, self._validate_zip_member(info.filename, target_dir))
                for info in zf.infolist()
            ]
            for info, dest in validated:
                if info.is_dir():
                    dest.mkdir(parents=True, exist_ok=True)
                    continue
                dest.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(info, "r") as src_f, open(dest, "wb") as dst_f:
                    shutil.copyfileobj(src_f, dst_f)
                extracted.append(dest)
        return extracted

    def _apply_mzp_run(
        self,
        extract_dir: Path,
        *,
        enu_dir: Path,
        startup_dir: Path,
        macros_dir: Path,
        icons_dir: Path,
    ) -> list[Path]:
        mzp_run = next(
            (p for p in extract_dir.rglob("*") if p.is_file() and p.name.lower() == "mzp.run"),
            None,
        )
        if mzp_run is None:
            return []
        var_map = {
            "$userscripts": startup_dir,
            "$scripts": enu_dir / "scripts",
            "$startupscripts": startup_dir,
            "$usermacros": macros_dir,
            "$macros": macros_dir,
            "$usericons": icons_dir,
            "$icons": icons_dir,
        }
        copied: list[Path] = []
        for raw_line in mzp_run.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("--"):
                continue
            try:
                tokens = shlex.split(line)
            except ValueError:
                continue
            if len(tokens) >= 4 and tokens[0].lower() == "copy" and tokens[2].lower() == "to":
                src_rel = tokens[1].replace("\\", "/").lstrip("/")
                dst_base = var_map.get(tokens[3].rstrip("\\/").lower())
                if dst_base is None:
                    continue
                try:
                    src_path = self._validate_zip_member(src_rel, extract_dir)
                except ValueError:
                    continue
                if src_path.is_file():
                    dst_base.mkdir(parents=True, exist_ok=True)
                    dst = dst_base / src_path.name
                    shutil.copy2(src_path, dst)
                    copied.append(dst)
                elif src_path.is_dir():
                    for item in sorted(src_path.rglob("*")):
                        if item.is_file():
                            rel = item.relative_to(src_path)
                            dst = dst_base / rel
                            dst.parent.mkdir(parents=True, exist_ok=True)
                            shutil.copy2(item, dst)
                            copied.append(dst)
        return copied

    @staticmethod
    def _merge_ini(target_ini: Path, source_ini: Path | None = None, color_scheme: str | None = None) -> None:
        cp = configparser.ConfigParser(interpolation=None)
        cp.optionxform = str  # preserve case
        if target_ini.is_file():
            cp.read(target_ini, encoding="utf-8")
        if source_ini is not None and source_ini.is_file():
            src_cp = configparser.ConfigParser(interpolation=None)
            src_cp.optionxform = str
            src_cp.read(source_ini, encoding="utf-8")
            for section in src_cp.sections():
                if not cp.has_section(section):
                    cp.add_section(section)
                for k, v in src_cp.items(section):
                    cp.set(section, k, v)
        if color_scheme:
            if not cp.has_section("UI"):
                cp.add_section("UI")
            cp.set("UI", "ColorScheme", color_scheme)
        with open(target_ini, "w", encoding="utf-8") as fh:
            cp.write(fh)

    def run_step(
        self,
        step_id: str,
        version: str,
        *,
        assets_dir: Path | None,
        log_cb: Callable[[str, str], None],
        cancel_event: threading.Event,
        config_path: Path | None = None,
    ) -> dict[str, Any]:
        ver = str(version)
        if cancel_event.is_set():
            return {
                "ok": False,
                "status": "aborted",
                "step_id": step_id,
                "version": ver,
                "files": [],
                "detail": f"Step '{step_id}' cancelled.",
            }

        raw_steps = self._load_raw_step_configs(config_path=config_path, version=ver)
        raw_step = next((s for s in raw_steps if (s.get("step_id") or s.get("id")) == step_id), None)
        if raw_step is None:
            return {
                "ok": False,
                "status": "failed",
                "step_id": step_id,
                "version": ver,
                "files": [],
                "detail": f"Unknown post-config step: {step_id}",
            }

        category = str(raw_step.get("category", ""))
        step_name = str(raw_step.get("name", step_id))
        assets = Path(assets_dir) if assets_dir is not None else None
        log_cb("INFO", f"Running post-config step '{step_id}' ({step_name}) for 3ds Max {ver}...")

        self._close_3dsmax_if_running(log_cb)
        enu_dir, backup_dir = self._ensure_enu_and_backup(ver)
        files: list[Path] = []

        if category == "renderer":
            plugins_dir = self._plugins_dir(ver)
            skip_plugin = str(raw_step.get("skip_if_plugin", "")).strip()
            if skip_plugin and (plugins_dir / skip_plugin).is_file():
                msg = f"{step_name} already installed ({skip_plugin} present); skipped."
                log_cb("INFO", msg)
                return {
                    "ok": True,
                    "status": "skipped",
                    "skipped": True,
                    "step_id": step_id,
                    "category": category,
                    "version": ver,
                    "files": [],
                    "backup_dir": str(backup_dir),
                    "detail": msg,
                }

            installer_glob = str(raw_step.get("installer_glob", "")).strip()
            if not installer_glob:
                installer_glob = "*corona*.exe" if "corona" in step_id.lower() else "*vray*.exe"

            installer_path: Path | None = None
            if assets is not None and assets.is_dir():
                matches = sorted(p for p in assets.rglob(installer_glob) if p.is_file())
                if matches:
                    installer_path = matches[0]

            if installer_path is None:
                msg = f"Installer matching '{installer_glob}' not found in assets_dir; skipped."
                log_cb("WARN", msg)
                return {
                    "ok": True,
                    "status": "skipped",
                    "skipped": True,
                    "step_id": step_id,
                    "category": category,
                    "version": ver,
                    "files": [],
                    "backup_dir": str(backup_dir),
                    "detail": msg,
                }

            silent_args = [str(a) for a in (raw_step.get("silent_args") or ["/quiet"])]
            cmd = [str(installer_path), *silent_args]
            log_cb("INFO", f"Running silent renderer installer: {' '.join(cmd)}")
            step_timeout = float(raw_step.get("timeout_sec") or 1800.0)
            proc = self._popen_fn(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

            def _drain_renderer() -> None:
                stream = getattr(proc, "stdout", None)
                if stream is None:
                    return
                try:
                    for line in stream:
                        cleaned = line.rstrip("\r\n")
                        if cleaned:
                            log_cb("INFO", cleaned)
                except Exception:
                    pass

            reader = threading.Thread(target=_drain_renderer, daemon=True)
            reader.start()
            deadline = time.monotonic() + max(0.05, step_timeout)
            while True:
                if cancel_event.is_set():
                    _terminate_process_tree(proc)
                    reader.join(timeout=2.0)
                    return {
                        "ok": False,
                        "status": "aborted",
                        "step_id": step_id,
                        "category": category,
                        "version": ver,
                        "files": [],
                        "detail": f"Renderer install '{step_id}' cancelled.",
                    }
                rc = proc.poll()
                if rc is not None:
                    reader.join(timeout=2.0)
                    if rc != 0:
                        return {
                            "ok": False,
                            "status": "failed",
                            "step_id": step_id,
                            "category": category,
                            "version": ver,
                            "files": [],
                            "detail": f"Renderer installer {installer_path.name} failed with exit code {rc}.",
                        }
                    break
                if time.monotonic() >= deadline:
                    _terminate_process_tree(proc)
                    reader.join(timeout=2.0)
                    return {
                        "ok": False,
                        "status": "timeout",
                        "step_id": step_id,
                        "category": category,
                        "version": ver,
                        "files": [],
                        "detail": f"Renderer installer {installer_path.name} timed out after {step_timeout}s.",
                    }
                time.sleep(min(0.25, max(0.01, step_timeout / 4.0)))
            return {
                "ok": True,
                "status": "done",
                "step_id": step_id,
                "category": category,
                "version": ver,
                "files": [str(installer_path)],
                "backup_dir": str(backup_dir),
                "detail": f"Installed {step_name} via {installer_path.name}.",
            }

        if category == "theme":
            ui_dir = enu_dir / "en-US" / "UI"
            ui_dir.mkdir(parents=True, exist_ok=True)
            clrx_name: str | None = None
            source_ini: Path | None = None
            if assets is not None and assets.is_dir():
                for src in sorted(assets.rglob("*")):
                    if not src.is_file():
                        continue
                    ext = src.suffix.lower()
                    if ext in (".clrx", ".cuix"):
                        dst = ui_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
                        if ext == ".clrx":
                            clrx_name = src.stem
                    elif src.name.lower() == "3dsmax.ini":
                        source_ini = src
            if clrx_name or source_ini is not None:
                ini_path = enu_dir / "3dsmax.ini"
                self._merge_ini(ini_path, source_ini=source_ini, color_scheme=clrx_name)
                files.append(ini_path)
            return {
                "ok": True,
                "status": "done",
                "step_id": step_id,
                "category": category,
                "version": ver,
                "files": [str(p) for p in files],
                "backup_dir": str(backup_dir),
                "detail": f"Applied theme assets ({len(files)} file(s)).",
            }

        if category == "quad_menu":
            ver_num = int(ver) if ver.isdigit() else 2025
            if ver_num <= 2024:
                target_dir = enu_dir / "en-US" / "UI"
                allowed_ext = ".mnux"
            else:
                target_dir = enu_dir / "en-US" / "UI" / "UserSettings"
                allowed_ext = ".mnx"
            target_dir.mkdir(parents=True, exist_ok=True)
            if assets is not None and assets.is_dir():
                for src in sorted(assets.rglob("*")):
                    if src.is_file() and src.suffix.lower() == allowed_ext:
                        dst = target_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
            return {
                "ok": True,
                "status": "done",
                "step_id": step_id,
                "category": category,
                "version": ver,
                "files": [str(p) for p in files],
                "backup_dir": str(backup_dir),
                "detail": f"Deployed {len(files)} {allowed_ext} quad-menu file(s) for 3ds Max {ver}.",
            }

        if category == "hotkey":
            ver_num = int(ver) if ver.isdigit() else 2025
            if ver_num <= 2024:
                target_dir = enu_dir / "en-US" / "UI"
                allowed_exts = {".kbdx", ".hsx"}
            else:
                target_dir = enu_dir / "en-US" / "UI" / "UserSettings"
                allowed_exts = {".hsx"}
            target_dir.mkdir(parents=True, exist_ok=True)
            if assets is not None and assets.is_dir():
                for src in sorted(assets.rglob("*")):
                    if src.is_file() and src.suffix.lower() in allowed_exts:
                        dst = target_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
            return {
                "ok": True,
                "status": "done",
                "step_id": step_id,
                "category": category,
                "version": ver,
                "files": [str(p) for p in files],
                "backup_dir": str(backup_dir),
                "detail": f"Deployed {len(files)} hotkey file(s) for 3ds Max {ver}.",
            }

        if category == "scripts":
            startup_dir = enu_dir / "scripts" / "startup"
            macros_dir = enu_dir / "usermacros"
            icons_dir = enu_dir / "usericons"
            startup_dir.mkdir(parents=True, exist_ok=True)
            macros_dir.mkdir(parents=True, exist_ok=True)
            icons_dir.mkdir(parents=True, exist_ok=True)

            if assets is not None and assets.is_dir():
                for src in sorted(assets.rglob("*")):
                    if not src.is_file():
                        continue
                    ext = src.suffix.lower()
                    if ext in (".ms", ".mse"):
                        dst = startup_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
                    elif ext == ".mcr":
                        dst = macros_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
                    elif ext in _ICON_EXTS:
                        dst = icons_dir / src.name
                        shutil.copy2(src, dst)
                        files.append(dst)
                    elif ext == ".mzp":
                        extract_dir = startup_dir / src.stem
                        try:
                            extracted = self._safe_extract_mzp(src, extract_dir)
                            files.extend(extracted)
                            copied = self._apply_mzp_run(
                                extract_dir,
                                enu_dir=enu_dir,
                                startup_dir=startup_dir,
                                macros_dir=macros_dir,
                                icons_dir=icons_dir,
                            )
                            files.extend(copied)
                        except (ValueError, zipfile.BadZipFile) as exc:
                            log_cb("ERROR", f"Rejected unsafe archive {src.name}: {exc}")
                            return {
                                "ok": False,
                                "status": "failed",
                                "step_id": step_id,
                                "category": category,
                                "version": ver,
                                "files": [str(p) for p in files],
                                "detail": f"Failed to extract {src.name}: {exc}",
                            }

            repo_verify = self._repo_root / "max_scripts" / "verify_install.ms"
            if repo_verify.is_file():
                dst_verify = startup_dir / "verify_install.ms"
                shutil.copy2(repo_verify, dst_verify)
                files.append(dst_verify)

            return {
                "ok": True,
                "status": "done",
                "step_id": step_id,
                "category": category,
                "version": ver,
                "files": [str(p) for p in files],
                "backup_dir": str(backup_dir),
                "detail": f"Deployed {len(files)} script/macro/icon artifact(s).",
            }

        if category == "verify":
            verification = self.verify_installation(ver)
            detail = (
                f"Verification {verification['status']}: "
                f"{len(verification['renderers'])} renderer(s), "
                f"{len(verification['scripts'])} script(s)."
            )
            log_cb("INFO", detail)
            return {
                "ok": bool(verification["ok"]),
                "status": "done" if verification["ok"] else "failed",
                "step_id": step_id,
                "category": "verify",
                "version": ver,
                "files": [verification["verify_file"]],
                "backup_dir": str(backup_dir),
                "verification": verification,
                "detail": detail,
            }

        return {
            "ok": False,
            "status": "failed",
            "step_id": step_id,
            "category": category,
            "version": ver,
            "files": [],
            "detail": f"Unsupported post-config category: {category}",
        }

    def verify_installation(self, version: str) -> dict[str, Any]:
        ver = str(version)
        max_exe = self._max_exe(ver)
        max_installed = max_exe.is_file()
        enu_dir = self._enu_dir(ver)
        backup_dir = self._backup_dir(ver)
        plugins_dir = self._plugins_dir(ver)
        verify_path = self._verify_json_path(ver)

        plugins: list[str] = []
        if plugins_dir.is_dir():
            plugins = sorted(
                p.name
                for p in plugins_dir.iterdir()
                if p.is_file() and p.suffix.lower() in (".dlu", ".dlr", ".gup")
            )

        renderers: list[str] = []
        for plugin in plugins:
            lower = plugin.lower()
            if lower == "coronamaxutils.dlu" and "Chaos Corona" not in renderers:
                renderers.append("Chaos Corona")
            elif lower == "vrender.dlr" and "V-Ray" not in renderers:
                renderers.append("V-Ray")
            elif lower.endswith(".dlr"):
                stem = Path(plugin).stem
                if stem not in renderers:
                    renderers.append(stem)

        scripts: list[str] = []
        scripts_root = enu_dir / "scripts"
        if scripts_root.is_dir():
            scripts = sorted(
                str(p.relative_to(scripts_root).as_posix())
                for p in scripts_root.rglob("*")
                if p.is_file() and p.suffix.lower() in (".ms", ".mse")
            )

        macros: list[str] = []
        macros_root = enu_dir / "usermacros"
        if macros_root.is_dir():
            macros = sorted(
                p.name
                for p in macros_root.iterdir()
                if p.is_file() and p.suffix.lower() == ".mcr"
            )

        ui_files: list[str] = []
        ui_root = enu_dir / "en-US" / "UI"
        if ui_root.is_dir():
            ui_files = sorted(
                str(p.relative_to(ui_root).as_posix())
                for p in ui_root.rglob("*")
                if p.is_file() and p.suffix.lower() in (".clrx", ".cuix", ".mnx", ".mnux", ".hsx", ".kbdx")
            )

        checks = {
            "max_exe_exists": max_installed,
            "enu_exists": enu_dir.is_dir(),
            "backup_exists": backup_dir.is_dir(),
            "has_scripts_or_ui": bool(scripts or macros or ui_files),
        }
        overall_ok = checks["enu_exists"] and checks["backup_exists"]
        report = {
            "ok": overall_ok,
            "status": "ok" if overall_ok else "failed",
            "version": ver,
            "max_exe": str(max_exe),
            "enu_dir": str(enu_dir),
            "backup_dir": str(backup_dir),
            "plugins": plugins,
            "renderers": renderers,
            "scripts": scripts,
            "macros": macros,
            "ui_files": ui_files,
            "checks": checks,
            "verify_file": str(verify_path),
        }
        _atomic_write_json(verify_path, report)
        return report


def create_windows_bundle(
    *,
    data_dir: Path | None = None,
    repo_root: Path | None = None,
    prompt_before_uac: bool = True,
) -> DriverBundle:
    """Create a DriverBundle wired with all Windows platform drivers."""
    return DriverBundle(
        system=WindowsSystemDriver(
            data_dir=data_dir,
            repo_root=repo_root,
            prompt_before_uac=prompt_before_uac,
        ),
        installer=WindowsInstallerDriver(),
        first_run=WindowsFirstRunDriver(repo_root=repo_root),
        post_config=WindowsPostConfigDriver(repo_root=repo_root, data_dir=data_dir),
    )


__all__ = [
    "SAC_POLICY_SUBKEY",
    "SAC_POLICY_VALUE",
    "SAC_STATE_TO_REGISTRY",
    "REGISTRY_TO_SAC_STATE",
    "SAC_GUIDED_URI",
    "DEFENDER_THREAT_URI",
    "DEFENDER_EXCLUSIONS_URI",
    "DEFAULT_POST_CONFIG_STEPS",
    "WindowsSystemDriver",
    "WindowsInstallerDriver",
    "WindowsFirstRunDriver",
    "WindowsPostConfigDriver",
    "create_windows_bundle",
]
