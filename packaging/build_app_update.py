"""Build an atomic ``app/`` update archive and ``UpdateManifest`` JSON (F-18, F-20)."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import sys
import zipfile

REPO_ROOT_DEFAULT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT_DEFAULT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT_DEFAULT))

from remote.protocol import UpdateManifest  # noqa: E402

BUNDLE_DIRS = ("remote", "cli", "ahk", "config", "max_scripts", "vm")
EXCLUDED_DIR_NAMES = {
    "__pycache__",
    ".git",
    ".agents",
    ".venv",
    "venv",
    "logs",
    "tests",
    "dist",
    "build",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}
EXCLUDED_FILE_SUFFIXES = (".pyc", ".pyo", ".pyd")
_SAFE_VERSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")


def validate_version_string(version: str) -> str:
    """Validate that ``version`` is non-empty and free of path-traversal characters."""
    if not isinstance(version, str) or not version.strip():
        raise ValueError("Version string must not be empty")
    cleaned = version.strip()
    if cleaned != version:
        raise ValueError(f"Version string must not have leading/trailing whitespace: {version!r}")
    if ".." in cleaned or "/" in cleaned or "\\" in cleaned or "\x00" in cleaned:
        raise ValueError(f"Unsafe characters or path traversal in version: {version!r}")
    if not _SAFE_VERSION_RE.match(cleaned):
        raise ValueError(f"Invalid version string: {version!r}")
    return cleaned


def _should_exclude(rel_parts: tuple[str, ...], name: str) -> bool:
    if any(part in EXCLUDED_DIR_NAMES for part in rel_parts):
        return True
    if name in EXCLUDED_DIR_NAMES:
        return True
    if name.endswith(EXCLUDED_FILE_SUFFIXES):
        return True
    return False


def _sha256_file(path: Path) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def build_app_update(
    *,
    version: str,
    out_dir: Path,
    min_runtime_version: str = "1.0.0",
    notes: str = "",
    repo_root: Path | None = None,
) -> tuple[Path, Path, UpdateManifest]:
    """Bundle ``remote/``, ``cli/``, ``ahk/``, ``config/``, ``max_scripts/``, ``vm/`` into a zip + manifest."""
    ver = validate_version_string(version)
    min_rt = validate_version_string(min_runtime_version)
    root = (repo_root or REPO_ROOT_DEFAULT).resolve()
    out_path = Path(out_dir).resolve()
    out_path.mkdir(parents=True, exist_ok=True)

    archive_name = f"max-agent-app-{ver}.zip"
    archive_path = out_path / archive_name
    manifest_path = out_path / "manifest.json"

    with zipfile.ZipFile(archive_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for folder_name in BUNDLE_DIRS:
            src_dir = root / folder_name
            if not src_dir.is_dir():
                continue
            for dirpath, dirnames, filenames in os.walk(src_dir):
                cur_dir = Path(dirpath)
                rel_dir = cur_dir.relative_to(root)
                dirnames[:] = sorted(
                    d for d in dirnames if not _should_exclude(rel_dir.parts + (d,), d)
                )
                for fname in sorted(filenames):
                    file_path = cur_dir / fname
                    rel_file = file_path.relative_to(root)
                    if _should_exclude(rel_file.parts[:-1], fname):
                        continue
                    arcname = PurePosixPath(*rel_file.parts).as_posix()
                    zf.write(file_path, arcname=arcname)

        zf.writestr("VERSION.txt", f"{ver}\n")

    size_bytes = archive_path.stat().st_size
    digest = _sha256_file(archive_path)
    manifest = UpdateManifest(
        version=ver,
        min_runtime_version=min_rt,
        file_id=f"app-{ver}",
        filename=archive_name,
        size_bytes=size_bytes,
        sha256=digest,
        notes=notes,
        download_url=None,
    )
    manifest_path.write_text(
        json.dumps(manifest.to_dict(), indent=2) + "\n",
        encoding="utf-8",
    )
    return archive_path, manifest_path, manifest


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="build_app_update.py",
        description="Build an atomic app/ update zip archive and UpdateManifest JSON.",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Target SemVer release version (e.g. 1.2.3).",
    )
    parser.add_argument(
        "--out-dir",
        "--output-dir",
        "--output",
        "-o",
        dest="out_dir",
        required=True,
        help="Output directory for the update zip and manifest.json.",
    )
    parser.add_argument(
        "--min-runtime-version",
        "--min-runtime",
        dest="min_runtime_version",
        default="1.0.0",
        help="Minimum compatible portable runtime version (default: 1.0.0).",
    )
    parser.add_argument(
        "--notes",
        default="",
        help="Optional release notes.",
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
        archive_path, manifest_path, manifest = build_app_update(
            version=args.version,
            out_dir=Path(args.out_dir),
            min_runtime_version=args.min_runtime_version,
            notes=args.notes,
            repo_root=Path(args.repo_root) if args.repo_root else None,
        )
    except ValueError as exc:
        sys.stderr.write(f"[build_app_update] ERROR: {exc}\n")
        return 2
    print(f"Built {archive_path} ({manifest.size_bytes} bytes, sha256={manifest.sha256})")
    print(f"Wrote {manifest_path}")
    return 0


__all__ = [
    "BUNDLE_DIRS",
    "EXCLUDED_DIR_NAMES",
    "validate_version_string",
    "build_app_update",
    "build_arg_parser",
    "main",
]

if __name__ == "__main__":
    raise SystemExit(main())
