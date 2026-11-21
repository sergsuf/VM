"""Build the portable MaxAgent client bundle layout (F-19, F-20)."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DEFAULT))

from packaging.build_app_update import (  # noqa: E402
    BUNDLE_DIRS,
    EXCLUDED_DIR_NAMES,
    EXCLUDED_FILE_SUFFIXES,
    validate_version_string,
)

PYTHON_PTH_CONTENT = "\n".join(
    [
        "python312.zip",
        ".",
        r"Lib\site-packages",
        "import site",
        "",
    ]
)


def _copy_filtered_tree(src_dir: Path, dst_dir: Path) -> None:
    for dirpath, dirnames, filenames in os.walk(src_dir):
        cur = Path(dirpath)
        rel = cur.relative_to(src_dir)
        dirnames[:] = sorted(d for d in dirnames if d not in EXCLUDED_DIR_NAMES)
        target_dir = dst_dir / rel
        target_dir.mkdir(parents=True, exist_ok=True)
        for fname in sorted(filenames):
            if fname in EXCLUDED_DIR_NAMES or fname.endswith(EXCLUDED_FILE_SUFFIXES):
                continue
            shutil.copy2(cur / fname, target_dir / fname)


def build_portable_client(
    *,
    out_dir: Path,
    version: str = "1.0.0",
    runtime_version: str = "1.0.0",
    offline: bool = False,
    connect_file: Path | None = None,
    repo_root: Path | None = None,
) -> Path:
    """Assemble the portable MaxAgent client directory structure in ``out_dir``."""
    ver = validate_version_string(version)
    rt_ver = validate_version_string(runtime_version)
    root = (repo_root or REPO_ROOT_DEFAULT).resolve()
    bundle_dir = Path(out_dir).resolve()
    bundle_dir.mkdir(parents=True, exist_ok=True)

    # 1. Start-MaxAgent.bat
    bat_src = root / "packaging" / "Start-MaxAgent.bat"
    shutil.copy2(bat_src, bundle_dir / "Start-MaxAgent.bat")

    # 2. runtime/
    runtime_dir = bundle_dir / "runtime"
    site_packages_dir = runtime_dir / "Lib" / "site-packages"
    site_packages_dir.mkdir(parents=True, exist_ok=True)

    bootstrap_src = root / "remote" / "client" / "bootstrap.py"
    shutil.copy2(bootstrap_src, runtime_dir / "bootstrap.py")

    (runtime_dir / "python312._pth").write_text(PYTHON_PTH_CONTENT, encoding="utf-8")
    (runtime_dir / "version.json").write_text(
        json.dumps(
            {"runtime_version": rt_ver, "app_version": ver},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    # 3. tools/ahk/AutoHotkey64.exe
    ahk_dir = bundle_dir / "tools" / "ahk"
    ahk_dir.mkdir(parents=True, exist_ok=True)
    ahk_dst = ahk_dir / "AutoHotkey64.exe"
    local_ahk = root / "tools" / "ahk" / "AutoHotkey64.exe"
    if local_ahk.is_file():
        shutil.copy2(local_ahk, ahk_dst)
    elif offline:
        # Offline skeleton marker so layout checks and non-Windows tests work deterministically.
        if not ahk_dst.exists():
            ahk_dst.write_bytes(b"")

    # 4. app/releases/<version>/ and app/active.json
    app_dir = bundle_dir / "app"
    release_dir = app_dir / "releases" / ver
    release_dir.mkdir(parents=True, exist_ok=True)

    for folder_name in BUNDLE_DIRS:
        src_folder = root / folder_name
        if src_folder.is_dir():
            _copy_filtered_tree(src_folder, release_dir / folder_name)

    (release_dir / "VERSION.txt").write_text(f"{ver}\n", encoding="utf-8")

    active_state = {
        "version": ver,
        "previous": None,
        "pending_verify": False,
        "attempts": 0,
        "blocked_versions": [],
    }
    (app_dir / "active.json").write_text(
        json.dumps(active_state, indent=2) + "\n",
        encoding="utf-8",
    )

    # 5. Optional connect.json
    if connect_file is not None:
        cf = Path(connect_file)
        if not cf.is_file():
            raise FileNotFoundError(f"connect.json not found: {cf}")
        shutil.copy2(cf, bundle_dir / "connect.json")

    return bundle_dir


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_portable_client.py",
        description="Build the portable MaxAgent client directory layout.",
    )
    parser.add_argument(
        "--out-dir",
        "--output-dir",
        "--output",
        "-o",
        dest="out_dir",
        required=True,
        help="Output directory for the portable client bundle.",
    )
    parser.add_argument(
        "--version",
        default="1.0.0",
        help="Initial app release version inside app/releases/<version>/ (default: 1.0.0).",
    )
    parser.add_argument(
        "--runtime-version",
        default="1.0.0",
        help="Portable runtime version written to runtime/version.json (default: 1.0.0).",
    )
    parser.add_argument(
        "--offline",
        "--no-download",
        "--skip-downloads",
        "--skeleton",
        "--layout-only",
        dest="offline",
        action="store_true",
        help="Create the full portable directory skeleton offline without downloading binaries.",
    )
    parser.add_argument(
        "--connect-file",
        default=None,
        help="Optional pre-baked connect.json pairing file to include in the bundle root.",
    )
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Optional repository root override.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    try:
        out = build_portable_client(
            out_dir=Path(args.out_dir),
            version=args.version,
            runtime_version=args.runtime_version,
            offline=bool(args.offline),
            connect_file=Path(args.connect_file) if args.connect_file else None,
            repo_root=Path(args.repo_root) if args.repo_root else None,
        )
    except (ValueError, FileNotFoundError) as exc:
        sys.stderr.write(f"[build_portable_client] ERROR: {exc}\n")
        return 2
    print(f"Built portable client bundle at {out}")
    return 0


__all__ = [
    "PYTHON_PTH_CONTENT",
    "build_portable_client",
    "build_arg_parser",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
