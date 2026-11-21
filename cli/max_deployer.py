"""Deploy MAXScript / MacroScript / MZP packages and launch 3ds Max (2024-2028)."""

from __future__ import annotations

import ctypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

DEFAULT_3DSMAX_EXE = Path(r"C:\Program Files\Autodesk\3ds Max 2025\3dsmax.exe")
SUPPORTED_3DSMAX_VERSIONS = ("2028", "2027", "2026", "2025", "2024")
VERSION_TO_INTERNAL = {
    "2024": "26.0",
    "2025": "27.0",
    "2026": "28.0",
    "2027": "29.0",
    "2028": "30.0",
}
ICON_EXTENSIONS = {".bmp", ".png", ".ico"}


def get_default_3dsmax_exe(version: str = "2025") -> Path:
    """Return the default 3dsmax.exe path for the given version year."""
    return Path(rf"C:\Program Files\Autodesk\3ds Max {version}\3dsmax.exe")


def _discover_3dsmax_from_registry(version: str) -> Path | None:
    """Discover 3dsmax.exe via ADSK_3DSMAX_X64_<ver> env var or Windows Registry."""
    env_dir = os.environ.get(f"ADSK_3DSMAX_X64_{version}")
    if env_dir:
        candidate = Path(env_dir) / "3dsmax.exe"
        if candidate.is_file():
            return candidate

    if sys.platform != "win32":
        return None

    import winreg  # type: ignore[import-not-found]

    internal_ver = VERSION_TO_INTERNAL.get(str(version))
    if internal_ver:
        for subkey in (
            rf"SOFTWARE\Autodesk\3dsMax\{internal_ver}",
            rf"SOFTWARE\WOW6432Node\Autodesk\3dsMax\{internal_ver}",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, subkey) as key:
                    install_dir, _ = winreg.QueryValueEx(key, "Installdir")
                    if install_dir:
                        candidate = Path(str(install_dir)) / "3dsmax.exe"
                        if candidate.is_file():
                            return candidate
            except OSError:
                pass

    uninstall_roots = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    for root_path in uninstall_roots:
        try:
            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, root_path) as root_key:
                idx = 0
                while True:
                    try:
                        sub_name = winreg.EnumKey(root_key, idx)
                        idx += 1
                    except OSError:
                        break
                    try:
                        with winreg.OpenKey(root_key, sub_name) as sub_key:
                            disp_name, _ = winreg.QueryValueEx(sub_key, "DisplayName")
                            if f"3ds Max {version}" in str(disp_name):
                                install_loc, _ = winreg.QueryValueEx(sub_key, "InstallLocation")
                                if install_loc:
                                    candidate = Path(str(install_loc)) / "3dsmax.exe"
                                    if candidate.is_file():
                                        return candidate
                    except OSError:
                        continue
        except OSError:
            continue
    return None


def get_installed_3dsmax_exe(version: str = "2025") -> Path | None:
    """Return installed 3dsmax.exe path via registry/env discovery or default path."""
    discovered = _discover_3dsmax_from_registry(str(version))
    if discovered is not None:
        return discovered
    default_exe = get_default_3dsmax_exe(str(version))
    if default_exe.exists():
        return default_exe
    return None


def find_all_installed_3dsmax(
    versions: tuple[str, ...] = ("2024", "2025", "2026", "2027", "2028"),
) -> list[tuple[str, Path]]:
    """Return list of (version, exe_path) for all installed 3ds Max versions."""
    installed: list[tuple[str, Path]] = []
    for version in versions:
        candidate = get_installed_3dsmax_exe(version)
        if candidate is not None and candidate.exists():
            installed.append((version, candidate))
    return installed


def find_installed_3dsmax_exe(
    versions: tuple[str, ...] = SUPPORTED_3DSMAX_VERSIONS,
) -> tuple[str, Path] | None:
    """Return (version, exe_path) for the first installed 3ds Max executable."""
    for version in versions:
        candidate = get_installed_3dsmax_exe(version)
        if candidate is not None and candidate.exists():
            return version, candidate
    return None


def get_max_user_enu_dir(version: str = "2025") -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / "AppData" / "Local"
    return base / "Autodesk" / "3dsMax" / f"{version} - 64bit" / "ENU"


def _validate_zip_member_path(member_name: str, target_root: Path) -> Path:
    """Validate a zip member name against Zip-Slip path traversal."""
    normalized = member_name.replace("\\", "/")
    if not normalized or normalized.startswith("/"):
        raise ValueError(f"Unsafe zip entry path (absolute): {member_name!r}")
    if re.match(r"^[A-Za-z]:", normalized):
        raise ValueError(f"Unsafe zip entry path (drive letter): {member_name!r}")

    parts = PurePosixPath(normalized).parts
    if ".." in parts:
        raise ValueError(f"Unsafe zip entry path (traversal): {member_name!r}")

    resolved_root = target_root.resolve()
    dest_path = (resolved_root / Path(*parts)).resolve()
    try:
        dest_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"Unsafe zip entry escapes target root: {member_name!r}") from exc
    return dest_path


def _safe_extract_mzp(mzp_path: Path, target_dir: Path) -> list[Path]:
    """Safely extract an .mzp archive after validating every entry against Zip-Slip."""
    extracted: list[Path] = []
    target_dir.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(mzp_path, "r") as zf:
        # Validate all entries before extracting anything.
        validated: list[tuple[zipfile.ZipInfo, Path]] = []
        for info in zf.infolist():
            dest_path = _validate_zip_member_path(info.filename, target_dir)
            validated.append((info, dest_path))

        for info, dest_path in validated:
            if info.is_dir():
                dest_path.mkdir(parents=True, exist_ok=True)
                continue
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src_f, open(dest_path, "wb") as dst_f:
                shutil.copyfileobj(src_f, dst_f)
            extracted.append(dest_path)
    return extracted


def _apply_mzp_run_directives(
    extract_dir: Path,
    *,
    enu_dir: Path,
    scripts_dir: Path,
    macros_dir: Path,
    icons_dir: Path,
) -> list[Path]:
    """Parse mzp.run copy directives and place referenced files into target ENU dirs."""
    mzp_run = None
    for cand in extract_dir.rglob("*"):
        if cand.is_file() and cand.name.lower() == "mzp.run":
            mzp_run = cand
            break
    if mzp_run is None:
        return []

    var_map = {
        "$userscripts": scripts_dir,
        "$scripts": enu_dir / "scripts",
        "$startupscripts": enu_dir / "scripts" / "startup",
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
            dst_token = tokens[3].rstrip("\\/").lower()
            dst_base = var_map.get(dst_token)
            if dst_base is None:
                continue
            try:
                src_path = _validate_zip_member_path(src_rel, extract_dir)
            except ValueError:
                continue
            if not src_path.exists():
                continue
            dst_base.mkdir(parents=True, exist_ok=True)
            if src_path.is_file():
                dst_path = dst_base / src_path.name
                shutil.copy2(src_path, dst_path)
                copied.append(dst_path)
            elif src_path.is_dir():
                for sub in sorted(src_path.rglob("*")):
                    if sub.is_file():
                        rel = sub.relative_to(src_path)
                        dst_sub = dst_base / rel
                        dst_sub.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(sub, dst_sub)
                        copied.append(dst_sub)
    return copied


def deploy_scripts(
    source_dir: Path,
    version: str = "2025",
    startup: bool = True,
    dry_run: bool = False,
    *,
    max_version: str | None = None,
) -> list[Path]:
    """Recursively copy .ms/.mse/.mcr/icons and safely unpack .mzp archives into 3ds Max user folders."""
    if not source_dir.exists():
        raise FileNotFoundError(f"Source scripts directory not found: {source_dir}")

    resolved_version = str(max_version or version)
    enu_dir = get_max_user_enu_dir(resolved_version)
    scripts_dir = enu_dir / "scripts" / ("startup" if startup else "")
    macros_dir = enu_dir / "usermacros"
    icons_dir = enu_dir / "usericons"
    deployed: list[Path] = []

    if not dry_run:
        scripts_dir.mkdir(parents=True, exist_ok=True)
        macros_dir.mkdir(parents=True, exist_ok=True)
        icons_dir.mkdir(parents=True, exist_ok=True)

    for item in sorted(source_dir.rglob("*")):
        if not item.is_file():
            continue
        ext = item.suffix.lower()
        if ext in {".ms", ".mse"}:
            target = scripts_dir / item.name
            if not dry_run:
                shutil.copy2(item, target)
            deployed.append(target)
        elif ext == ".mcr":
            target = macros_dir / item.name
            if not dry_run:
                shutil.copy2(item, target)
            deployed.append(target)
        elif ext in ICON_EXTENSIONS:
            target = icons_dir / item.name
            if not dry_run:
                shutil.copy2(item, target)
            deployed.append(target)
        elif ext == ".mzp":
            target_dir = scripts_dir / item.stem
            if not dry_run:
                extracted = _safe_extract_mzp(item, target_dir)
                deployed.extend(extracted)
                copied = _apply_mzp_run_directives(
                    target_dir,
                    enu_dir=enu_dir,
                    scripts_dir=scripts_dir,
                    macros_dir=macros_dir,
                    icons_dir=icons_dir,
                )
                deployed.extend(copied)
            if target_dir not in deployed:
                deployed.append(target_dir)

    return deployed


class DeElevatedProcess:
    """Lightweight Popen-compatible wrapper for a Win32 process launched via CreateProcessWithTokenW."""

    def __init__(self, pid: int, process_handle: int) -> None:
        self.pid = pid
        self._handle = process_handle
        self._returncode: int | None = None

    @property
    def returncode(self) -> int | None:
        return self.poll()

    def poll(self) -> int | None:
        if self._returncode is not None:
            return self._returncode
        if sys.platform != "win32" or not self._handle:
            return None
        k32 = ctypes.windll.kernel32
        wait_res = k32.WaitForSingleObject(self._handle, 0)
        if wait_res == 0:  # WAIT_OBJECT_0
            code = ctypes.c_ulong(0)
            k32.GetExitCodeProcess(self._handle, ctypes.byref(code))
            self._returncode = int(code.value)
            k32.CloseHandle(self._handle)
            self._handle = 0
            return self._returncode
        return None

    def wait(self, timeout: float | None = None) -> int:
        if self._returncode is not None:
            return self._returncode
        if sys.platform != "win32" or not self._handle:
            return 0
        k32 = ctypes.windll.kernel32
        ms = 0xFFFFFFFF if timeout is None else max(0, int(timeout * 1000))
        res = k32.WaitForSingleObject(self._handle, ms)
        if res != 0:
            raise subprocess.TimeoutExpired(cmd="3dsmax.exe", timeout=timeout or 0)
        code = ctypes.c_ulong(0)
        k32.GetExitCodeProcess(self._handle, ctypes.byref(code))
        self._returncode = int(code.value)
        k32.CloseHandle(self._handle)
        self._handle = 0
        return self._returncode

    def terminate(self) -> None:
        if sys.platform == "win32" and self._handle and self._returncode is None:
            ctypes.windll.kernel32.TerminateProcess(self._handle, 1)

    def kill(self) -> None:
        self.terminate()


def _launch_de_elevated_win32(cmd: list[str], cwd: str) -> Any | None:
    """Launch a Medium-integrity process on Windows by duplicating explorer.exe's token."""
    if sys.platform != "win32":
        return None

    try:
        import psutil
    except ImportError:
        psutil = None  # type: ignore[assignment]

    explorer_pid: int | None = None
    if psutil is not None:
        for proc in psutil.process_iter(["pid", "name"]):
            try:
                if (proc.info.get("name") or "").lower() == "explorer.exe":
                    explorer_pid = int(proc.info["pid"])
                    break
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue

    if explorer_pid is None:
        return None

    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    advapi = ctypes.windll.advapi32

    PROCESS_QUERY_INFORMATION = 0x0400
    TOKEN_DUPLICATE = 0x0002
    TOKEN_QUERY = 0x0008
    TOKEN_ASSIGN_PRIMARY = 0x0001
    TOKEN_ADJUST_DEFAULT = 0x0080
    TOKEN_ADJUST_SESSIONID = 0x0100
    MAXIMUM_ALLOWED = 0x02000000
    SecurityImpersonation = 2
    TokenPrimary = 1

    class STARTUPINFOW(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(ctypes.c_byte)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    h_proc = k32.OpenProcess(PROCESS_QUERY_INFORMATION, False, explorer_pid)
    if not h_proc:
        return None

    h_token = wintypes.HANDLE()
    h_dup = wintypes.HANDLE()
    try:
        if not advapi.OpenProcessToken(
            h_proc,
            TOKEN_DUPLICATE | TOKEN_QUERY | TOKEN_ASSIGN_PRIMARY | TOKEN_ADJUST_DEFAULT | TOKEN_ADJUST_SESSIONID,
            ctypes.byref(h_token),
        ):
            return None
        if not advapi.DuplicateTokenEx(
            h_token,
            MAXIMUM_ALLOWED,
            None,
            SecurityImpersonation,
            TokenPrimary,
            ctypes.byref(h_dup),
        ):
            return None

        si = STARTUPINFOW()
        si.cb = ctypes.sizeof(STARTUPINFOW)
        si.lpDesktop = r"winsta0\default"
        si.dwFlags = 0x00000001  # STARTF_USESHOWWINDOW
        si.wShowWindow = 1
        pi = PROCESS_INFORMATION()

        cmdline = subprocess.list2cmdline(cmd)
        ok = advapi.CreateProcessWithTokenW(
            h_dup,
            0,
            None,
            cmdline,
            0,
            None,
            cwd,
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not ok:
            return None
        if pi.hThread:
            k32.CloseHandle(pi.hThread)
        return DeElevatedProcess(pid=int(pi.dwProcessId), process_handle=int(pi.hProcess))
    finally:
        if h_dup:
            k32.CloseHandle(h_dup)
        if h_token:
            k32.CloseHandle(h_token)
        k32.CloseHandle(h_proc)


def launch_3dsmax(
    max_exe: Path = DEFAULT_3DSMAX_EXE,
    run_script: Path | None = None,
    extra_args: list[str] | None = None,
    dry_run: bool = False,
    de_elevate: bool = False,
) -> subprocess.Popen[bytes] | Any | None:
    """Launch 3ds Max in background and return the process handle."""
    cmd = [str(max_exe)]
    if run_script:
        cmd.extend(["-U", "MAXScript", str(run_script)])
    if extra_args:
        cmd.extend(extra_args)

    if dry_run:
        mode = " [de-elevated]" if de_elevate else ""
        print(f"[DRY-RUN LAUNCH{mode}] {' '.join(cmd)}")
        return None

    if not max_exe.exists():
        raise FileNotFoundError(f"3ds Max executable not found: {max_exe}")

    if sys.platform == "win32" and de_elevate:
        try:
            is_admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except Exception:
            is_admin = False
        if is_admin:
            proc = _launch_de_elevated_win32(cmd, cwd=str(max_exe.parent))
            if proc is not None:
                return proc
            runas_exe = shutil.which("runas.exe")
            if runas_exe:
                try:
                    return subprocess.Popen(
                        [runas_exe, "/trustlevel:0x20000", subprocess.list2cmdline(cmd)],
                        cwd=str(max_exe.parent),
                    )
                except OSError:
                    pass

    startupinfo = None
    if sys.platform == "win32":
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.lpDesktop = r"winsta0\default"
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startupinfo.wShowWindow = 1  # SW_SHOWNORMAL

    return subprocess.Popen(
        cmd,
        cwd=str(max_exe.parent),
        startupinfo=startupinfo,
    )
