"""Pure-Python atomic background ``app/`` auto-updater (F-01, F-18).

Uses Python standard library only so it runs cleanly inside both Windows
``python-embed`` and Linux test environments.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import threading
import time
from typing import Any
import urllib.request
import uuid
import zipfile

from remote.protocol import UpdateManifest

UPDATE_RESTART_EXIT_CODE = 42

DEFAULT_ACTIVE_STATE: dict[str, Any] = {
    "version": "1.0.0",
    "previous": None,
    "pending_verify": False,
    "attempts": 0,
    "blocked_versions": [],
}

_UPDATE_SAFE_STAGE_STATUS = {
    ("PREP", "idle"),
    ("WAIT_LICENSE", "waiting_operator"),
    ("POST_CONFIG", "waiting_operator"),
    ("DONE", "completed"),
}
_UPDATE_SAFE_STATUSES = {"paused", "failed", "aborted"}


class UpdateError(RuntimeError):
    """Base error raised when an update cannot be validated or applied."""


class UpdateBlockedError(UpdateError):
    """Raised when a target version is present in ``blocked_versions``."""


class UpdateCompatibilityError(UpdateError):
    """Raised when SemVer or ``min_runtime_version`` checks fail."""


class UpdateVerificationError(UpdateError):
    """Raised when size, SHA-256, or archive safety (Zip-Slip) checks fail."""


_SEMVER_RE = re.compile(
    r"^(\d+(?:\.\d+)*)(?:-([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?(?:\+([0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


def _validate_safe_component(value: str, label: str) -> str:
    """Validate that ``value`` is a safe single path component without traversal."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise UpdateVerificationError(f"Invalid {label}: {value!r}")
    if "\x00" in value or "/" in value or "\\" in value:
        raise UpdateVerificationError(
            f"Path separator or null byte forbidden in {label}: {value!r}"
        )
    if re.match(r"^[A-Za-z]:", value):
        raise UpdateVerificationError(f"Drive letter forbidden in {label}: {value!r}")
    if value in (".", "..") or ".." in value:
        raise UpdateVerificationError(f"Path traversal forbidden in {label}: {value!r}")
    return value


def parse_semver(version: str) -> tuple[int, ...]:
    """Parse a SemVer-like string into a comparable tuple of integers."""
    if not isinstance(version, str):
        raise ValueError(f"Version must be a string, got {type(version).__name__}")
    cleaned = version.strip()
    if cleaned.startswith(("v", "V")):
        cleaned = cleaned[1:]
    match = _SEMVER_RE.match(cleaned)
    if not match:
        raise ValueError(f"Invalid SemVer string: {version!r}")
    core = match.group(1)
    nums = [int(part) for part in core.split(".")]
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)


def compare_semver(left: str, right: str) -> int:
    """Return -1 if ``left < right``, 0 if equal, +1 if ``left > right``."""
    a = list(parse_semver(left))
    b = list(parse_semver(right))
    length = max(len(a), len(b))
    a.extend([0] * (length - len(a)))
    b.extend([0] * (length - len(b)))
    if a < b:
        return -1
    if a > b:
        return 1
    return 0


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    """Atomically write JSON via same-directory ``.tmp`` + ``fsync`` + ``os.replace``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_name = f".{path.name}.{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex[:8]}.tmp"
    tmp_path = path.parent / tmp_name
    payload = json.dumps(data, indent=2, sort_keys=False) + "\n"
    try:
        with open(tmp_path, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _quarantine_corrupt_file(path: Path) -> None:
    """Back up a corrupt JSON file to ``<name>.corrupt.<ts>`` so startup can recover."""
    backup = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
    if backup.exists():
        backup = path.with_name(f"{path.name}.corrupt.{int(time.time())}.{uuid.uuid4().hex[:6]}")
    try:
        os.replace(path, backup)
    except OSError:
        pass


def read_active_state(active_path: Path, default_version: str = "1.0.0") -> dict[str, Any]:
    """Read ``app/active.json`` or return normalized defaults if absent or corrupt."""
    state: dict[str, Any] = {
        "version": default_version,
        "previous": None,
        "pending_verify": False,
        "attempts": 0,
        "blocked_versions": [],
    }
    if not active_path.is_file():
        return state
    try:
        raw = json.loads(active_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError, ValueError):
        _quarantine_corrupt_file(active_path)
        return state
    if not isinstance(raw, dict):
        _quarantine_corrupt_file(active_path)
        return state
    state["version"] = str(raw.get("version") or default_version)
    prev = raw.get("previous")
    state["previous"] = str(prev) if prev else None
    state["pending_verify"] = bool(raw.get("pending_verify", False))
    raw_attempts = raw.get("attempts", 0)
    try:
        state["attempts"] = max(0, int(raw_attempts if raw_attempts is not None else 0))
    except (TypeError, ValueError):
        state["attempts"] = 0
    blocked = raw.get("blocked_versions")
    if isinstance(blocked, list):
        state["blocked_versions"] = [str(v) for v in blocked if v]
    return state


def verify_sha256(file_path: Path, expected_sha256: str) -> bool:
    """Verify ``file_path`` SHA-256 digest (case-insensitive hex) using streaming reads."""
    hasher = hashlib.sha256()
    with open(file_path, "rb") as fh:
        while True:
            chunk = fh.read(65536)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest().lower() == expected_sha256.strip().lower()


def _validate_zip_member(raw_name: str, dest_resolved: Path) -> PurePosixPath:
    """Validate a zip entry name against Zip-Slip, absolute paths, and traversal."""
    if not raw_name or "\x00" in raw_name:
        raise UpdateVerificationError(f"Unsafe zip member name: {raw_name!r}")
    if raw_name.startswith(("/", "\\")):
        raise UpdateVerificationError(f"Absolute path forbidden in update archive: {raw_name!r}")
    if re.match(r"^[A-Za-z]:", raw_name):
        raise UpdateVerificationError(f"Drive-letter path forbidden in update archive: {raw_name!r}")

    normalized = raw_name.replace("\\", "/")
    parts = [p for p in normalized.split("/") if p]
    if any(part in (".", "..") for part in parts):
        if ".." in parts:
            raise UpdateVerificationError(f"Path traversal ('..') forbidden in archive: {raw_name!r}")
    clean_parts = [p for p in parts if p != "."]
    rel = PurePosixPath(*clean_parts) if clean_parts else PurePosixPath()

    target = (dest_resolved / Path(*clean_parts)).resolve()
    if target != dest_resolved and not target.is_relative_to(dest_resolved):
        raise UpdateVerificationError(f"Archive member escapes target directory: {raw_name!r}")
    return rel


def safe_extract_zip(archive_path: Path, dest_dir: Path) -> list[Path]:
    """Validate all zip members first, then safely extract into ``dest_dir``."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_resolved = dest_dir.resolve()
    extracted: list[Path] = []

    with zipfile.ZipFile(archive_path, "r") as zf:
        members = zf.infolist()
        validated: list[tuple[zipfile.ZipInfo, PurePosixPath]] = []
        for info in members:
            mode = (info.external_attr >> 16) & 0xFFFF
            if mode and stat.S_ISLNK(mode):
                raise UpdateVerificationError(
                    f"Symlink member forbidden in update archive: {info.filename!r}"
                )
            rel = _validate_zip_member(info.filename, dest_resolved)
            validated.append((info, rel))

        for info, rel in validated:
            if not rel.parts:
                continue
            target = dest_resolved.joinpath(*rel.parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with zf.open(info, "r") as src, open(target, "wb") as dst:
                shutil.copyfileobj(src, dst, length=65536)
            extracted.append(target)

    return extracted


def mark_healthy_boot(app_dir: Path) -> dict[str, Any]:
    """Atomically clear ``pending_verify`` and reset ``attempts`` in ``app/active.json``."""
    app_path = Path(app_dir)
    active_path = app_path / "active.json"
    state = read_active_state(active_path)
    state["pending_verify"] = False
    state["attempts"] = 0
    atomic_write_json(active_path, state)
    return state


confirm_healthy_boot = mark_healthy_boot


def prune_releases(app_dir: Path, keep: int = 2) -> list[Path]:
    """Remove obsolete directories under ``app/releases/`` while preserving active and previous."""
    app_path = Path(app_dir)
    releases_dir = app_path / "releases"
    if not releases_dir.is_dir():
        return []

    active = read_active_state(app_path / "active.json")
    protected = {active["version"]}
    if active.get("previous"):
        protected.add(str(active["previous"]))

    candidates: list[Path] = []
    for entry in releases_dir.iterdir():
        if not entry.is_dir():
            continue
        if entry.name.startswith(".staging_"):
            continue
        candidates.append(entry)

    def _sort_key(path: Path) -> tuple[int, tuple[int, ...], str]:
        try:
            return (1, parse_semver(path.name), path.name)
        except ValueError:
            return (0, (0, 0, 0), path.name)

    candidates.sort(key=_sort_key, reverse=True)

    kept_count = 0
    removed: list[Path] = []
    for entry in candidates:
        if entry.name in protected:
            kept_count += 1
            continue
        if kept_count < max(keep, 0):
            kept_count += 1
            continue
        shutil.rmtree(entry, ignore_errors=False)
        removed.append(entry)
    return removed


def can_accept_update(state_machine_or_state: Any) -> bool:
    """Return True if the agent/state machine is in a safe idle state for an update restart."""
    if state_machine_or_state is None:
        return True
    if hasattr(state_machine_or_state, "can_accept_update") and callable(
        state_machine_or_state.can_accept_update
    ):
        return bool(state_machine_or_state.can_accept_update())

    if isinstance(state_machine_or_state, dict):
        stage = str(state_machine_or_state.get("stage", ""))
        status = str(state_machine_or_state.get("status", ""))
    else:
        stage_obj = getattr(state_machine_or_state, "stage", "")
        status_obj = getattr(state_machine_or_state, "status", "")
        stage = getattr(stage_obj, "value", str(stage_obj))
        status = getattr(status_obj, "value", str(status_obj))

    if status in _UPDATE_SAFE_STATUSES:
        return True
    return (stage, status) in _UPDATE_SAFE_STAGE_STATUS


class AppUpdater:
    """Stages, verifies, and atomically activates ``app/releases/<version>/`` bundles."""

    def __init__(
        self,
        app_dir: Path | None = None,
        root_dir: Path | None = None,
        base_dir: Path | None = None,
        runtime_version: str = "1.0.0",
        current_version: str | None = None,
        *,
        headers: dict[str, str] | None = None,
        keep_releases: int = 2,
    ) -> None:
        if app_dir is not None:
            self.app_dir = Path(app_dir)
            self.root_dir = self.app_dir.parent
        elif root_dir is not None:
            self.root_dir = Path(root_dir)
            self.app_dir = self.root_dir / "app"
        elif base_dir is not None:
            self.root_dir = Path(base_dir)
            self.app_dir = self.root_dir / "app"
        else:
            self.root_dir = Path.cwd()
            self.app_dir = self.root_dir / "app"

        self.runtime_version = runtime_version
        self._explicit_current_version = current_version
        self.headers = dict(headers) if headers else {}
        self.keep_releases = keep_releases
        self._lock = threading.Lock()

    @property
    def active_path(self) -> Path:
        return self.app_dir / "active.json"

    @property
    def releases_dir(self) -> Path:
        return self.app_dir / "releases"

    @property
    def staging_dir(self) -> Path:
        return self.app_dir / "staging"

    def get_active_state(self) -> dict[str, Any]:
        default_ver = self._explicit_current_version or "1.0.0"
        return read_active_state(self.active_path, default_version=default_ver)

    def check_manifest(
        self,
        manifest: UpdateManifest,
        *,
        state_machine: Any = None,
    ) -> tuple[bool, str]:
        """Validate version SemVer, ``blocked_versions``, ``min_runtime_version``, and idle state."""
        _validate_safe_component(manifest.version, "manifest.version")
        _validate_safe_component(manifest.filename, "manifest.filename")
        parse_semver(manifest.version)

        active = self.get_active_state()
        current_ver = self._explicit_current_version or active["version"]

        if manifest.version in active.get("blocked_versions", []):
            return False, f"Version {manifest.version} is in blocked_versions"

        if compare_semver(manifest.version, current_ver) <= 0:
            return (
                False,
                f"Manifest version {manifest.version} is not newer than current {current_ver}",
            )

        if compare_semver(manifest.min_runtime_version, self.runtime_version) > 0:
            return (
                False,
                f"Requires runtime {manifest.min_runtime_version} > {self.runtime_version}",
            )

        if state_machine is not None and not can_accept_update(state_machine):
            return False, "Agent is currently busy and cannot accept an update restart"

        return True, "ok"

    def download_update(
        self,
        manifest: UpdateManifest,
        dest_path: Path,
        *,
        download_url: str | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 60.0,
    ) -> Path:
        """Download update archive to ``dest_path`` atomically via a temp file."""
        url = download_url or manifest.download_url
        if not url:
            raise UpdateError(f"No download URL provided for manifest {manifest.version}")

        merged_headers = dict(self.headers)
        if headers:
            merged_headers.update(headers)

        dest_path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = dest_path.parent / f".{dest_path.name}.{uuid.uuid4().hex[:8]}.part"
        req = urllib.request.Request(url, headers=merged_headers, method="GET")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp, open(tmp_path, "wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, dest_path)
            return dest_path
        except BaseException:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass
            raise

    def apply_update(
        self,
        manifest: UpdateManifest,
        *,
        archive_path: Path | None = None,
        download_url: str | None = None,
        headers: dict[str, str] | None = None,
        state_machine: Any = None,
    ) -> dict[str, Any]:
        """Verify, extract, and atomically swap ``app/active.json`` to ``manifest.version``."""
        _validate_safe_component(manifest.version, "manifest.version")
        _validate_safe_component(manifest.filename, "manifest.filename")
        parse_semver(manifest.version)

        with self._lock:
            active = self.get_active_state()
            current_ver = self._explicit_current_version or active["version"]

            if manifest.version in active.get("blocked_versions", []):
                raise UpdateBlockedError(f"Version {manifest.version} is blocked")
            if compare_semver(manifest.version, current_ver) <= 0:
                raise UpdateCompatibilityError(
                    f"Version {manifest.version} is not newer than {current_ver}"
                )
            if compare_semver(manifest.min_runtime_version, self.runtime_version) > 0:
                raise UpdateCompatibilityError(
                    f"min_runtime_version {manifest.min_runtime_version} exceeds {self.runtime_version}"
                )
            if state_machine is not None and not can_accept_update(state_machine):
                raise UpdateError("Agent is not in an update-safe state")

            if archive_path is not None:
                local_archive = Path(archive_path)
                if not local_archive.is_file():
                    raise UpdateError(f"Archive path does not exist: {local_archive}")
            else:
                self.staging_dir.mkdir(parents=True, exist_ok=True)
                staging_resolved = self.staging_dir.resolve()
                local_archive = self.staging_dir / manifest.filename
                if (
                    local_archive.resolve().parent != staging_resolved
                    or not local_archive.resolve().is_relative_to(staging_resolved)
                ):
                    raise UpdateVerificationError(
                        f"Manifest filename escapes staging directory: {manifest.filename!r}"
                    )
                self.download_update(
                    manifest,
                    local_archive,
                    download_url=download_url,
                    headers=headers,
                )

            if manifest.size_bytes > 0 and local_archive.stat().st_size != manifest.size_bytes:
                raise UpdateVerificationError(
                    f"Size mismatch for {local_archive.name}: "
                    f"expected {manifest.size_bytes}, got {local_archive.stat().st_size}"
                )

            if not verify_sha256(local_archive, manifest.sha256):
                raise UpdateVerificationError(
                    f"SHA-256 verification failed for {local_archive.name}"
                )

            self.releases_dir.mkdir(parents=True, exist_ok=True)
            releases_resolved = self.releases_dir.resolve()
            staging_release = self.releases_dir / f".staging_{manifest.version}_{uuid.uuid4().hex[:8]}"
            target_release = self.releases_dir / manifest.version
            if (
                staging_release.resolve().parent != releases_resolved
                or target_release.resolve().parent != releases_resolved
                or not target_release.resolve().is_relative_to(releases_resolved)
            ):
                raise UpdateVerificationError(
                    f"Manifest version escapes releases directory: {manifest.version!r}"
                )

            try:
                safe_extract_zip(local_archive, staging_release)
                if target_release.exists():
                    shutil.rmtree(target_release)
                os.replace(staging_release, target_release)
            except BaseException:
                shutil.rmtree(staging_release, ignore_errors=True)
                raise

            new_active = {
                "version": manifest.version,
                "previous": current_ver,
                "pending_verify": True,
                "attempts": 0,
                "blocked_versions": list(active.get("blocked_versions", [])),
            }
            atomic_write_json(self.active_path, new_active)
            prune_releases(self.app_dir, keep=self.keep_releases)
            return new_active

    stage_and_activate = apply_update
    download_and_apply = apply_update

    def mark_healthy_boot(self) -> dict[str, Any]:
        """Clear ``pending_verify`` and reset ``attempts`` for the active release."""
        return mark_healthy_boot(self.app_dir)

    confirm_healthy_boot = mark_healthy_boot

    def prune_releases(self, keep: int | None = None) -> list[Path]:
        """Prune obsolete release directories while preserving active and previous."""
        return prune_releases(self.app_dir, keep=self.keep_releases if keep is None else keep)


Updater = AppUpdater

__all__ = [
    "UPDATE_RESTART_EXIT_CODE",
    "DEFAULT_ACTIVE_STATE",
    "UpdateError",
    "UpdateBlockedError",
    "UpdateCompatibilityError",
    "UpdateVerificationError",
    "parse_semver",
    "compare_semver",
    "atomic_write_json",
    "read_active_state",
    "verify_sha256",
    "safe_extract_zip",
    "mark_healthy_boot",
    "confirm_healthy_boot",
    "prune_releases",
    "can_accept_update",
    "AppUpdater",
    "Updater",
]
