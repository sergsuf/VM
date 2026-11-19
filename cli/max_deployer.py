"""Deploy MAXScript / MacroScript / MZP packages and launch 3ds Max (2025/2027)."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

DEFAULT_3DSMAX_EXE = Path(r"C:\Program Files\Autodesk\3ds Max 2025\3dsmax.exe")


def get_default_3dsmax_exe(version: str = "2025") -> Path:
    """Return the default 3dsmax.exe path for the given version year."""
    return Path(rf"C:\Program Files\Autodesk\3ds Max {version}\3dsmax.exe")


def get_max_user_enu_dir(version: str = "2025") -> Path:
    local_app_data = os.environ.get("LOCALAPPDATA")
    if local_app_data:
        base = Path(local_app_data)
    else:
        base = Path.home() / "AppData" / "Local"
    return base / "Autodesk" / "3dsMax" / f"{version} - 64bit" / "ENU"


def deploy_scripts(
    source_dir: Path,
    version: str = "2025",
    startup: bool = True,
    dry_run: bool = False,
) -> list[Path]:
    """Copy .ms/.mse/.mcr files and unpack .mzp archives into 3ds Max user folders."""
    if not source_dir.exists():
        raise FileNotFoundError(f"Source scripts directory not found: {source_dir}")

    enu_dir = get_max_user_enu_dir(version)
    scripts_dir = enu_dir / "scripts" / ("startup" if startup else "")
    macros_dir = enu_dir / "usermacros"
    deployed: list[Path] = []

    if not dry_run:
        scripts_dir.mkdir(parents=True, exist_ok=True)
        macros_dir.mkdir(parents=True, exist_ok=True)

    for item in sorted(source_dir.iterdir()):
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
        elif ext == ".mzp":
            target_dir = scripts_dir / item.stem
            if not dry_run:
                target_dir.mkdir(parents=True, exist_ok=True)
                with zipfile.ZipFile(item, "r") as zf:
                    zf.extractall(target_dir)
            deployed.append(target_dir)

    return deployed


def launch_3dsmax(
    max_exe: Path = DEFAULT_3DSMAX_EXE,
    run_script: Path | None = None,
    extra_args: list[str] | None = None,
    dry_run: bool = False,
) -> subprocess.Popen[bytes] | None:
    """Launch 3ds Max 2025 in background and return the Popen handle."""
    cmd = [str(max_exe)]
    if run_script:
        cmd.extend(["-U", "MAXScript", str(run_script)])
    if extra_args:
        cmd.extend(extra_args)

    if dry_run:
        print(f"[DRY-RUN LAUNCH] {' '.join(cmd)}")
        return None

    if not max_exe.exists():
        raise FileNotFoundError(f"3ds Max executable not found: {max_exe}")

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
