"""Operator ISO & Asset Catalog with sidecar SHA-256 caching and O(1)-RAM HTTP Range streaming."""

from __future__ import annotations

import email.utils
import hashlib
import json
import os
import re
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any

from remote.protocol import IsoSpec, utc_now_iso

SUPPORTED_VERSIONS: tuple[str, ...] = ("2024", "2025", "2026", "2027", "2028")
DEFAULT_CHUNK_SIZE: int = 256 * 1024
STREAM_BUFFER_SIZE: int = 64 * 1024

_MAX_NAME_RE = re.compile(r"3ds[\s._-]*max", re.IGNORECASE)
_VERSION_RE = re.compile(r"(?<!\d)(202[4-8])(?!\d)")
_SHA256_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


class RangeNotSatisfiableError(ValueError):
    """Raised when an HTTP Range header cannot be satisfied for a file's size."""

    def __init__(self, total_size: int, message: str = "Range Not Satisfiable") -> None:
        super().__init__(message)
        self.total_size = total_size


def extract_3dsmax_version(filename: str) -> str | None:
    """Return 3ds Max version ('2024'..'2028') if filename is a matching .iso, else None."""
    if not isinstance(filename, str):
        return None
    name = filename.strip()
    if not name.lower().endswith(".iso"):
        return None
    if not _MAX_NAME_RE.search(name):
        return None
    match = _VERSION_RE.search(name)
    if not match:
        return None
    version = match.group(1)
    if version not in SUPPORTED_VERSIONS:
        return None
    return version


def make_file_id(path: Path | str, version: str | None = None) -> str:
    """Return a deterministic URL-safe file_id for a catalog file."""
    name = Path(path).name
    digest = hashlib.sha256(name.encode("utf-8")).hexdigest()[:12]
    if version and version in SUPPORTED_VERSIONS:
        return f"iso-{version}-{digest}"
    return f"file-{digest}"


def compute_file_sha256(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Stream-compute lowercase hex SHA-256 of a file in O(1) RAM."""
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sidecar_path_for(iso_path: Path) -> Path:
    """Return the canonical sidecar SHA-256 JSON path for an ISO file."""
    return Path(f"{iso_path}.sha256.json")


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(
        f"{target.name}.tmp.{os.getpid()}.{threading.get_ident()}"
    )
    data = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8")
    try:
        with tmp_path.open("wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def load_or_compute_sha256(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    """Load SHA-256 from valid sidecar or recompute and atomically persist sidecar."""
    st = path.stat()
    size_bytes = int(st.st_size)
    mtime_ns = int(st.st_mtime_ns)
    sidecar = sidecar_path_for(path)

    if sidecar.is_file():
        try:
            raw = json.loads(sidecar.read_text(encoding="utf-8"))
            if (
                isinstance(raw, dict)
                and raw.get("size_bytes") == size_bytes
                and raw.get("mtime_ns") == mtime_ns
                and isinstance(raw.get("sha256"), str)
                and _SHA256_HEX_RE.match(raw["sha256"].lower())
            ):
                return raw["sha256"].lower()
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    digest = compute_file_sha256(path, chunk_size=chunk_size)
    sidecar_payload = {
        "filename": path.name,
        "size_bytes": size_bytes,
        "mtime_ns": mtime_ns,
        "sha256": digest,
        "updated_at": utc_now_iso(),
    }
    _atomic_write_json(sidecar, sidecar_payload)
    return digest


def parse_range_header(range_header: str, total_size: int) -> tuple[int, int]:
    """Parse an HTTP Range header ('bytes=...') into inclusive (start, end) offsets.

    Raises RangeNotSatisfiableError if the range is malformed, multi-range,
    inverted, or out of bounds for total_size.
    """
    if total_size <= 0:
        raise RangeNotSatisfiableError(total_size, "Cannot satisfy range for empty file")
    if not isinstance(range_header, str):
        raise RangeNotSatisfiableError(total_size, "Invalid Range header type")

    raw = range_header.strip()
    if not raw.lower().startswith("bytes="):
        raise RangeNotSatisfiableError(total_size, f"Unsupported range unit: {range_header!r}")

    spec = raw[6:].strip()
    if not spec or "," in spec or "-" not in spec:
        raise RangeNotSatisfiableError(total_size, f"Invalid range spec: {range_header!r}")

    start_str, end_str = spec.split("-", 1)
    start_str = start_str.strip()
    end_str = end_str.strip()

    if not start_str and not end_str:
        raise RangeNotSatisfiableError(total_size, f"Empty range bounds: {range_header!r}")

    if not start_str:
        # Suffix-byte-range-spec: bytes=-N
        if not end_str.isdigit():
            raise RangeNotSatisfiableError(total_size, f"Invalid suffix range: {range_header!r}")
        suffix = int(end_str)
        if suffix <= 0:
            raise RangeNotSatisfiableError(total_size, f"Zero suffix range: {range_header!r}")
        start = max(0, total_size - suffix)
        end = total_size - 1
        return start, end

    if not start_str.isdigit():
        raise RangeNotSatisfiableError(total_size, f"Invalid range start: {range_header!r}")
    start = int(start_str)
    if start >= total_size:
        raise RangeNotSatisfiableError(
            total_size, f"Range start {start} >= total_size {total_size}"
        )

    if not end_str:
        return start, total_size - 1

    if not end_str.isdigit():
        raise RangeNotSatisfiableError(total_size, f"Invalid range end: {range_header!r}")
    end = int(end_str)
    if end < start:
        raise RangeNotSatisfiableError(
            total_size, f"Inverted range {start}-{end}"
        )
    if end >= total_size:
        end = total_size - 1
    return start, end


def _if_range_matches(if_range: str | None, *, etag: str | None, mtime_sec: float) -> bool:
    """Return True if If-Range is absent or matches the current entity."""
    if not if_range:
        return True
    val = if_range.strip()
    if not val:
        return True
    if val.startswith('"') or val.startswith('W/"'):
        if etag is None:
            return False
        norm_val = val[2:] if val.startswith("W/") else val
        norm_etag = etag[2:] if etag.startswith("W/") else etag
        return norm_val == norm_etag
    # HTTP-date comparison
    try:
        parsed_dt = email.utils.parsedate_to_datetime(val)
    except (ValueError, TypeError, OverflowError):
        return False
    if parsed_dt is None:
        return False
    try:
        return int(parsed_dt.timestamp()) == int(mtime_sec)
    except (ValueError, TypeError, OverflowError):
        return False


def serve_file_range(
    handler: BaseHTTPRequestHandler,
    path: Path,
    *,
    etag: str | None = None,
    content_type: str = "application/octet-stream",
    head_only: bool = False,
    stream_buffer_size: int = STREAM_BUFFER_SIZE,
) -> None:
    """Serve a file over HTTP/1.1 with Range, If-Range, 206/200/416, and O(1)-RAM streaming."""
    if not path.is_file():
        handler.send_error(404, "File not found")
        return

    st = path.stat()
    total_size = int(st.st_size)
    last_modified = email.utils.formatdate(st.st_mtime, usegmt=True)
    entity_etag = etag or f'"{st.st_size:x}-{st.st_mtime_ns:x}"'

    range_header = handler.headers.get("Range")
    if_range_header = handler.headers.get("If-Range")

    use_range = bool(range_header) and _if_range_matches(
        if_range_header, etag=entity_etag, mtime_sec=st.st_mtime
    )

    if use_range and range_header is not None:
        try:
            start, end = parse_range_header(range_header, total_size)
        except RangeNotSatisfiableError:
            handler.send_response(416)
            handler.send_header("Content-Range", f"bytes */{total_size}")
            handler.send_header("Accept-Ranges", "bytes")
            handler.send_header("Content-Length", "0")
            handler.end_headers()
            return

        length = end - start + 1
        handler.send_response(206)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Range", f"bytes {start}-{end}/{total_size}")
        handler.send_header("Content-Length", str(length))
        handler.send_header("ETag", entity_etag)
        handler.send_header("Last-Modified", last_modified)
        handler.end_headers()
    else:
        start = 0
        length = total_size
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Accept-Ranges", "bytes")
        handler.send_header("Content-Length", str(total_size))
        handler.send_header("ETag", entity_etag)
        handler.send_header("Last-Modified", last_modified)
        handler.end_headers()

    if head_only or length == 0:
        return

    remaining = length
    with path.open("rb") as fh:
        if start > 0:
            fh.seek(start)
        while remaining > 0:
            to_read = min(stream_buffer_size, remaining)
            chunk = fh.read(to_read)
            if not chunk:
                break
            try:
                handler.wfile.write(chunk)
            except (BrokenPipeError, ConnectionResetError):
                break
            remaining -= len(chunk)


@dataclass
class CatalogEntry:
    """In-memory catalog entry binding an IsoSpec to its local path and ETag."""

    spec: IsoSpec
    path: Path
    mtime_ns: int
    etag: str
    chunk_size: int = DEFAULT_CHUNK_SIZE


class IsoCatalog:
    """Thread-safe 3ds Max ISO & asset catalog with sidecar SHA-256 caching."""

    def __init__(
        self,
        iso_dir: Path | str | None = None,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
    ) -> None:
        self._iso_dir: Path | None = Path(iso_dir) if iso_dir is not None else None
        self._chunk_size = int(chunk_size)
        self._lock = threading.RLock()
        self._by_file_id: dict[str, CatalogEntry] = {}
        self._by_version: dict[str, CatalogEntry] = {}
        self._manual_ids: set[str] = set()

    @property
    def iso_dir(self) -> Path | None:
        return self._iso_dir

    def scan(self, iso_dir: Path | str | None = None) -> list[IsoSpec]:
        """Scan directory for 3ds Max 2024..2028 ISOs and refresh sidecar SHA-256 cache."""
        with self._lock:
            target = Path(iso_dir) if iso_dir is not None else self._iso_dir
            if iso_dir is not None:
                self._iso_dir = target

            # Remove previously scanned ISO entries while preserving manually registered assets.
            for fid in list(self._by_file_id):
                if fid not in self._manual_ids:
                    del self._by_file_id[fid]
            self._by_version.clear()

            if target is None or not target.is_dir():
                return []

            scanned: list[CatalogEntry] = []
            for item in sorted(target.iterdir(), key=lambda p: p.name.lower()):
                if not item.is_file():
                    continue
                version = extract_3dsmax_version(item.name)
                if version is None:
                    continue
                st = item.stat()
                digest = load_or_compute_sha256(item)
                file_id = make_file_id(item, version=version)
                spec = IsoSpec(
                    file_id=file_id,
                    filename=item.name,
                    version=version,
                    size_bytes=int(st.st_size),
                    sha256=digest,
                )
                etag = f'"{digest[:16]}-{st.st_size}"'
                entry = CatalogEntry(
                    spec=spec,
                    path=item,
                    mtime_ns=int(st.st_mtime_ns),
                    etag=etag,
                    chunk_size=self._chunk_size,
                )
                self._by_file_id[file_id] = entry
                self._by_version.setdefault(version, entry)
                scanned.append(entry)

            return [e.spec for e in scanned]

    def register_file(
        self,
        file_id: str,
        path: Path | str,
        *,
        version: str = "0",
        sha256: str | None = None,
        filename: str | None = None,
        download_url: str | None = None,
    ) -> IsoSpec:
        """Register an arbitrary file (e.g., update bundle zip or explicit ISO) for HTTP serving."""
        p = Path(path)
        st = p.stat()
        digest = sha256.lower() if sha256 else compute_file_sha256(p)
        spec = IsoSpec(
            file_id=file_id,
            filename=filename or p.name,
            version=version,
            size_bytes=int(st.st_size),
            sha256=digest,
            download_url=download_url,
        )
        etag = f'"{digest[:16]}-{st.st_size}"'
        entry = CatalogEntry(
            spec=spec,
            path=p,
            mtime_ns=int(st.st_mtime_ns),
            etag=etag,
            chunk_size=self._chunk_size,
        )
        with self._lock:
            self._by_file_id[file_id] = entry
            self._manual_ids.add(file_id)
        return spec

    def list_specs(self) -> list[IsoSpec]:
        with self._lock:
            return [
                entry.spec
                for fid, entry in self._by_file_id.items()
                if fid not in self._manual_ids
            ]

    def get_entry(self, file_id: str) -> CatalogEntry | None:
        with self._lock:
            return self._by_file_id.get(file_id)

    def get_by_version(self, version: str) -> CatalogEntry | None:
        with self._lock:
            return self._by_version.get(version)

    def get_meta(self, file_id: str) -> dict[str, Any] | None:
        with self._lock:
            entry = self._by_file_id.get(file_id)
            if entry is None:
                return None
            return {
                "file_id": entry.spec.file_id,
                "filename": entry.spec.filename,
                "version": entry.spec.version,
                "size_bytes": entry.spec.size_bytes,
                "sha256": entry.spec.sha256,
                "chunk_size": entry.chunk_size,
                "etag": entry.etag,
            }

    def serve_http_file(
        self,
        handler: BaseHTTPRequestHandler,
        file_id: str,
        *,
        head_only: bool = False,
    ) -> bool:
        """Serve a catalog file over HTTP with Range/If-Range support. Returns True if found."""
        entry = self.get_entry(file_id)
        if entry is None or not entry.path.is_file():
            handler.send_error(404, "File not found")
            return False
        serve_file_range(
            handler,
            entry.path,
            etag=entry.etag,
            head_only=head_only,
        )
        return True


__all__ = [
    "SUPPORTED_VERSIONS",
    "DEFAULT_CHUNK_SIZE",
    "STREAM_BUFFER_SIZE",
    "RangeNotSatisfiableError",
    "CatalogEntry",
    "IsoCatalog",
    "extract_3dsmax_version",
    "make_file_id",
    "compute_file_sha256",
    "sidecar_path_for",
    "load_or_compute_sha256",
    "parse_range_header",
    "serve_file_range",
]
