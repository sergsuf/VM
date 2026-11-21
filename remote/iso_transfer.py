"""Client ISO local pre-check and chunked resumable transfer engine."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import re
import shutil
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from remote.iso_catalog import compute_file_sha256, extract_3dsmax_version
from remote.protocol import IsoSpec, TransferProgress

DEFAULT_WINDOWS_SEARCH_ROOTS: tuple[str, ...] = (
    r"C:\Install",
    r"C:\\",
    r"D:\Install",
    r"D:\\",
    r"E:\Install",
    r"E:\\",
    r"F:\Install",
    r"F:\\",
    r"Z:\3dsMax\Install",
    r"Z:\\",
)

DEFAULT_CHUNK_SIZE: int = 256 * 1024
DEFAULT_STREAM_READ_SIZE: int = 64 * 1024
DEFAULT_TAIL_SIZE: int = 4096
DEFAULT_MAX_RETRIES: int = 6
DEFAULT_BACKOFF_BASE: float = 0.25
DEFAULT_MAX_BACKOFF: float = 5.0


class TransferError(RuntimeError):
    """Base error for ISO transfer failures."""


class TransferPausedError(TransferError):
    """Raised when transfer is paused via pause_event."""


class TransferCancelledError(TransferError):
    """Raised when transfer is cancelled via cancel_event."""


class InsufficientDiskSpaceError(TransferError):
    """Raised when destination filesystem lacks enough free space."""


class ChecksumMismatchError(TransferError):
    """Raised when downloaded file fails full SHA-256 verification."""


@dataclass(frozen=True)
class TransferResult:
    """Outcome of an ISO local pre-check or download."""

    local_path: Path
    skipped: bool
    bytes_downloaded: int
    sha256: str


class FileStreamLike(Protocol):
    """Minimal streaming response interface consumed by IsoTransferClient."""

    status: int

    def iter_chunks(self, chunk_size: int = DEFAULT_STREAM_READ_SIZE) -> Iterator[bytes]: ...

    def close(self) -> None: ...


class FileTransportLike(Protocol):
    """Minimal transport interface needed by IsoTransferClient."""

    def open_file_stream(
        self,
        file_id: str,
        *,
        start: int | None = None,
        end: int | None = None,
        download_url: str | None = None,
        timeout: float | None = None,
    ) -> FileStreamLike: ...


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


def default_iso_search_roots(
    cache_dir: Path | str | None = None,
    extra_roots: Sequence[Path | str] | None = None,
) -> list[Path]:
    """Build ordered, deduplicated list of directories to scan for pre-existing ISOs."""
    raw_roots: list[Path] = []
    if extra_roots:
        for root in extra_roots:
            raw_roots.append(Path(root))
    if cache_dir is not None:
        raw_roots.append(Path(cache_dir))
    if os.name == "nt":
        for win_root in DEFAULT_WINDOWS_SEARCH_ROOTS:
            raw_roots.append(Path(win_root))

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in raw_roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            deduped.append(root)
    return deduped


def _is_candidate_iso(candidate: Path, spec: IsoSpec) -> bool:
    if not candidate.is_file():
        return False
    name = candidate.name
    if not name.lower().endswith(".iso"):
        return False
    if name.lower() != spec.filename.lower():
        if extract_3dsmax_version(name) != spec.version:
            return False
    try:
        return candidate.stat().st_size == spec.size_bytes
    except OSError:
        return False


def find_local_verified_iso(
    spec: IsoSpec,
    *,
    cache_dir: Path | str | None = None,
    search_roots: Sequence[Path | str] | None = None,
    progress_cb: Callable[[TransferProgress], None] | None = None,
    cancel_event: threading.Event | None = None,
) -> Path | None:
    """Scan local roots for an ISO matching filename/version, size, and SHA-256."""
    if progress_cb is not None:
        progress_cb(
            TransferProgress(
                phase="checking",
                bytes_done=0,
                bytes_total=spec.size_bytes,
                percent=0.0,
                speed_bps=0.0,
                eta_seconds=None,
            )
        )

    roots = (
        default_iso_search_roots(cache_dir=cache_dir, extra_roots=search_roots)
        if search_roots is not None or cache_dir is not None
        else default_iso_search_roots()
    )

    expected_sha = spec.sha256.lower()
    for root in roots:
        if cancel_event is not None and cancel_event.is_set():
            raise TransferCancelledError("ISO pre-check cancelled")
        if not root.is_dir():
            continue
        try:
            entries = sorted(root.iterdir(), key=lambda p: p.name.lower())
        except OSError:
            continue

        for candidate in entries:
            if cancel_event is not None and cancel_event.is_set():
                raise TransferCancelledError("ISO pre-check cancelled")
            if not _is_candidate_iso(candidate, spec):
                continue
            try:
                digest = compute_file_sha256(candidate)
            except OSError:
                continue
            if digest.lower() == expected_sha:
                if progress_cb is not None:
                    progress_cb(
                        TransferProgress(
                            phase="skipped",
                            bytes_done=spec.size_bytes,
                            bytes_total=spec.size_bytes,
                            percent=100.0,
                            speed_bps=0.0,
                            eta_seconds=0.0,
                            local_path=str(candidate),
                        )
                    )
                return candidate
    return None


def _compute_tail_sha256(path: Path, offset: int, tail_size: int) -> tuple[str, int]:
    """Compute SHA-256 of the last min(offset, tail_size) bytes ending at offset."""
    if offset <= 0 or tail_size <= 0:
        return hashlib.sha256(b"").hexdigest(), 0
    actual_tail = min(offset, tail_size)
    start = offset - actual_tail
    h = hashlib.sha256()
    with path.open("rb") as fh:
        fh.seek(start)
        remaining = actual_tail
        while remaining > 0:
            chunk = fh.read(min(DEFAULT_STREAM_READ_SIZE, remaining))
            if not chunk:
                break
            h.update(chunk)
            remaining -= len(chunk)
    return h.hexdigest(), actual_tail


class _DirectUrlStream:
    """Minimal O(1)-RAM HTTP stream for direct URLs when no transport is supplied."""

    def __init__(self, resp: http.client.HTTPResponse, conn: http.client.HTTPConnection) -> None:
        self._resp = resp
        self._conn = conn
        self.status = int(resp.status)

    def iter_chunks(self, chunk_size: int = DEFAULT_STREAM_READ_SIZE) -> Iterator[bytes]:
        while True:
            chunk = self._resp.read(chunk_size)
            if not chunk:
                break
            yield chunk

    def close(self) -> None:
        try:
            self._resp.close()
        finally:
            self._conn.close()


def _open_direct_http_range(
    url: str,
    *,
    start: int,
    end: int,
    headers: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> _DirectUrlStream:
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"}:
        raise TransferError(f"Unsupported download URL scheme: {parsed.scheme!r}")
    host = parsed.hostname
    if not host:
        raise TransferError(f"Invalid download URL: {url!r}")
    port = parsed.port
    path = parsed.path or "/"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    conn: http.client.HTTPConnection
    if parsed.scheme == "https":
        conn = http.client.HTTPSConnection(host, port=port or 443, timeout=timeout)
    else:
        conn = http.client.HTTPConnection(host, port=port or 80, timeout=timeout)

    req_headers = dict(headers or {})
    req_headers["Range"] = f"bytes={start}-{end}"
    try:
        conn.request("GET", path, headers=req_headers)
        resp = conn.getresponse()
    except Exception:
        conn.close()
        raise
    return _DirectUrlStream(resp, conn)


def _sleep_interruptible(
    seconds: float,
    *,
    pause_event: threading.Event | None,
    cancel_event: threading.Event | None,
) -> None:
    deadline = time.monotonic() + max(0.0, seconds)
    while time.monotonic() < deadline:
        if cancel_event is not None and cancel_event.is_set():
            raise TransferCancelledError("Transfer cancelled during retry backoff")
        if pause_event is not None and pause_event.is_set():
            raise TransferPausedError("Transfer paused during retry backoff")
        time.sleep(min(0.05, max(0.0, deadline - time.monotonic())))


class IsoTransferClient:
    """Resumable chunked ISO downloader with pre-check, tail-hash recovery, and SHA-256 verify."""

    def __init__(
        self,
        transport: FileTransportLike | None = None,
        *,
        chunk_size: int = DEFAULT_CHUNK_SIZE,
        tail_size: int = DEFAULT_TAIL_SIZE,
        max_retries: int = DEFAULT_MAX_RETRIES,
        backoff_base: float = DEFAULT_BACKOFF_BASE,
        max_backoff: float = DEFAULT_MAX_BACKOFF,
        request_timeout: float = 30.0,
    ) -> None:
        self._transport = transport
        self._chunk_size = max(4096, int(chunk_size))
        self._tail_size = max(512, int(tail_size))
        self._max_retries = max(0, int(max_retries))
        self._backoff_base = max(0.01, float(backoff_base))
        self._max_backoff = max(self._backoff_base, float(max_backoff))
        self._request_timeout = float(request_timeout)

    def _open_range(
        self,
        spec: IsoSpec,
        start: int,
        end: int,
    ) -> FileStreamLike:
        if self._transport is not None:
            return self._transport.open_file_stream(
                spec.file_id,
                start=start,
                end=end,
                download_url=spec.download_url,
                timeout=self._request_timeout,
            )
        if spec.download_url:
            return _open_direct_http_range(
                spec.download_url,
                start=start,
                end=end,
                timeout=self._request_timeout,
            )
        raise TransferError(
            "IsoTransferClient requires either a transport or spec.download_url"
        )

    def _write_part_meta(
        self,
        meta_path: Path,
        part_path: Path,
        spec: IsoSpec,
        offset: int,
    ) -> None:
        tail_sha, actual_tail = _compute_tail_sha256(part_path, offset, self._tail_size)
        payload = {
            "file_id": spec.file_id,
            "filename": spec.filename,
            "size_bytes": spec.size_bytes,
            "sha256": spec.sha256.lower(),
            "bytes_written": offset,
            "tail_sha256": tail_sha,
            "tail_size": actual_tail,
        }
        _atomic_write_json(meta_path, payload)

    def _resolve_resume_offset(
        self,
        part_path: Path,
        meta_path: Path,
        spec: IsoSpec,
    ) -> int:
        if not part_path.is_file() or not meta_path.is_file():
            return 0
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, ValueError):
            return 0

        if not isinstance(meta, dict):
            return 0
        if (
            meta.get("filename") != spec.filename
            or meta.get("size_bytes") != spec.size_bytes
            or str(meta.get("sha256", "")).lower() != spec.sha256.lower()
        ):
            return 0

        try:
            actual_size = int(part_path.stat().st_size)
            recorded = int(meta.get("bytes_written", 0))
        except (OSError, TypeError, ValueError):
            return 0

        if recorded <= 0 or actual_size <= 0:
            return 0

        offset = min(actual_size, recorded)
        if actual_size > offset:
            with part_path.open("r+b") as fh:
                fh.truncate(offset)

        try:
            raw_tail_size = meta.get("tail_size")
            tail_size = int(raw_tail_size) if raw_tail_size is not None else self._tail_size
        except (TypeError, ValueError):
            return 0
        expected_tail = str(meta.get("tail_sha256") or "").strip().lower()
        if tail_size <= 0 or not expected_tail:
            return 0
        actual_tail, _ = _compute_tail_sha256(part_path, offset, tail_size)
        if actual_tail != expected_tail:
            # Torn-tail corruption: rewind by tail_size and truncate.
            rewound = max(0, offset - tail_size)
            with part_path.open("r+b") as fh:
                fh.truncate(rewound)
            self._write_part_meta(meta_path, part_path, spec, rewound)
            return rewound
        return offset

    def download(
        self,
        spec: IsoSpec,
        dest_dir: Path | str,
        *,
        search_roots: Sequence[Path | str] | None = None,
        progress_cb: Callable[[TransferProgress], None] | None = None,
        pause_event: threading.Event | None = None,
        cancel_event: threading.Event | None = None,
        skip_local_check: bool = False,
    ) -> TransferResult:
        """Ensure spec ISO exists in dest_dir (or search_roots), resuming if partial."""
        fname = spec.filename
        if (
            not isinstance(fname, str)
            or not fname
            or "\x00" in fname
            or "/" in fname
            or "\\" in fname
            or re.match(r"^[A-Za-z]:", fname)
            or fname in (".", "..")
            or ".." in fname
            or Path(fname).name != fname
        ):
            raise TransferError(f"Unsafe ISO filename: {fname!r}")

        dest = Path(dest_dir)
        dest.mkdir(parents=True, exist_ok=True)
        dest_resolved = dest.resolve()

        if not skip_local_check:
            existing = find_local_verified_iso(
                spec,
                cache_dir=dest,
                search_roots=search_roots,
                progress_cb=progress_cb,
                cancel_event=cancel_event,
            )
            if existing is not None:
                return TransferResult(
                    local_path=existing,
                    skipped=True,
                    bytes_downloaded=0,
                    sha256=spec.sha256.lower(),
                )

        final_path = dest / fname
        part_path = dest / f"{fname}.part"
        meta_path = dest / f"{fname}.part.meta.json"
        if (
            final_path.resolve().parent != dest_resolved
            or part_path.resolve().parent != dest_resolved
            or meta_path.resolve().parent != dest_resolved
        ):
            raise TransferError(f"ISO filename escapes destination directory: {fname!r}")

        offset = self._resolve_resume_offset(part_path, meta_path, spec)
        needed_bytes = max(0, spec.size_bytes - offset)
        free_bytes = shutil.disk_usage(dest).free
        if free_bytes < needed_bytes:
            if progress_cb is not None:
                pct = (offset / spec.size_bytes * 100.0) if spec.size_bytes > 0 else 0.0
                progress_cb(
                    TransferProgress(
                        phase="failed",
                        bytes_done=offset,
                        bytes_total=spec.size_bytes,
                        percent=round(pct, 2),
                        speed_bps=0.0,
                        eta_seconds=None,
                    )
                )
            raise InsufficientDiskSpaceError(
                f"Insufficient disk space in {dest}: need {needed_bytes} bytes, "
                f"only {free_bytes} bytes free"
            )

        # Ensure .part exists and is truncated to offset only after disk check passes.
        if offset == 0:
            with part_path.open("wb") as fh:
                fh.flush()
            self._write_part_meta(meta_path, part_path, spec, 0)

        started_at = time.monotonic()
        session_downloaded = 0

        while offset < spec.size_bytes:
            if cancel_event is not None and cancel_event.is_set():
                raise TransferCancelledError("Transfer cancelled")
            if pause_event is not None and pause_event.is_set():
                self._write_part_meta(meta_path, part_path, spec, offset)
                raise TransferPausedError("Transfer paused")

            req_end = min(offset + self._chunk_size - 1, spec.size_bytes - 1)
            last_exc: Exception | None = None

            for attempt in range(self._max_retries + 1):
                if cancel_event is not None and cancel_event.is_set():
                    raise TransferCancelledError("Transfer cancelled")
                if pause_event is not None and pause_event.is_set():
                    self._write_part_meta(meta_path, part_path, spec, offset)
                    raise TransferPausedError("Transfer paused")

                stream: FileStreamLike | None = None
                try:
                    stream = self._open_range(spec, offset, req_end)
                    status = int(stream.status)

                    if status in (429, 500, 502, 503, 504):
                        raise TransferError(f"Transient HTTP status {status}")
                    if status == 416:
                        if offset >= spec.size_bytes:
                            break
                        raise TransferError(
                            f"HTTP 416 Range Not Satisfiable at offset {offset}/{spec.size_bytes}"
                        )
                    if status not in (200, 206):
                        raise TransferError(f"Unexpected HTTP status {status}")

                    skip_prefix = offset if (status == 200 and offset > 0) else 0
                    expected_len = (req_end - offset + 1) if status == 206 else (spec.size_bytes - offset)
                    received_for_range = 0

                    with part_path.open("r+b") as fh:
                        fh.seek(offset)
                        for raw_chunk in stream.iter_chunks(DEFAULT_STREAM_READ_SIZE):
                            if cancel_event is not None and cancel_event.is_set():
                                raise TransferCancelledError("Transfer cancelled")
                            if pause_event is not None and pause_event.is_set():
                                fh.flush()
                                self._write_part_meta(meta_path, part_path, spec, offset)
                                raise TransferPausedError("Transfer paused")

                            data = raw_chunk
                            if skip_prefix > 0:
                                if len(data) <= skip_prefix:
                                    skip_prefix -= len(data)
                                    continue
                                data = data[skip_prefix:]
                                skip_prefix = 0

                            if not data:
                                continue

                            remaining_file = spec.size_bytes - offset
                            if len(data) > remaining_file:
                                data = data[:remaining_file]

                            fh.write(data)
                            fh.flush()
                            written = len(data)
                            offset += written
                            session_downloaded += written
                            received_for_range += written

                            elapsed = max(1e-6, time.monotonic() - started_at)
                            speed_bps = session_downloaded / elapsed
                            remaining = max(0, spec.size_bytes - offset)
                            eta = (remaining / speed_bps) if speed_bps > 0 else None
                            pct = min(100.0, (offset / spec.size_bytes) * 100.0)
                            if progress_cb is not None:
                                progress_cb(
                                    TransferProgress(
                                        phase="downloading",
                                        bytes_done=offset,
                                        bytes_total=spec.size_bytes,
                                        percent=round(pct, 2),
                                        speed_bps=round(speed_bps, 2),
                                        eta_seconds=round(eta, 2) if eta is not None else None,
                                    )
                                )
                            if offset >= spec.size_bytes:
                                break

                    self._write_part_meta(meta_path, part_path, spec, offset)
                    if status == 206 and received_for_range < expected_len and offset < spec.size_bytes:
                        raise TransferError(
                            f"Truncated 206 response: got {received_for_range}/{expected_len} bytes"
                        )
                    if status == 200 and received_for_range == 0 and offset < spec.size_bytes:
                        raise TransferError(
                            f"Truncated 200 response at offset {offset}/{spec.size_bytes}: received 0 new bytes"
                        )
                    last_exc = None
                    break
                except (TransferCancelledError, TransferPausedError):
                    raise
                except Exception as exc:
                    status_code = getattr(exc, "status_code", None)
                    transient = (
                        isinstance(
                            exc,
                            (
                                OSError,
                                TimeoutError,
                                http.client.HTTPException,
                                urllib.error.URLError,
                                TransferError,
                            ),
                        )
                        and status_code not in (400, 401, 403, 404)
                    )
                    if not transient or attempt >= self._max_retries:
                        if progress_cb is not None:
                            pct = (
                                (offset / spec.size_bytes * 100.0)
                                if spec.size_bytes > 0
                                else 0.0
                            )
                            progress_cb(
                                TransferProgress(
                                    phase="failed",
                                    bytes_done=offset,
                                    bytes_total=spec.size_bytes,
                                    percent=round(pct, 2),
                                    speed_bps=0.0,
                                    eta_seconds=None,
                                )
                            )
                        raise TransferError(
                            f"ISO transfer failed at offset {offset}/{spec.size_bytes}: {exc}"
                        ) from exc
                    last_exc = exc
                    self._write_part_meta(meta_path, part_path, spec, offset)
                    delay = min(self._backoff_base * (2**attempt), self._max_backoff)
                    _sleep_interruptible(
                        delay, pause_event=pause_event, cancel_event=cancel_event
                    )
                finally:
                    if stream is not None:
                        try:
                            stream.close()
                        except Exception:
                            pass

            if last_exc is not None:
                raise TransferError(str(last_exc)) from last_exc

        # Full SHA-256 verification before promotion
        if progress_cb is not None:
            progress_cb(
                TransferProgress(
                    phase="verifying",
                    bytes_done=spec.size_bytes,
                    bytes_total=spec.size_bytes,
                    percent=100.0,
                    speed_bps=0.0,
                    eta_seconds=0.0,
                )
            )

        actual_sha = compute_file_sha256(part_path).lower()
        if actual_sha != spec.sha256.lower():
            quarantine_path = dest / f"{spec.filename}.part.corrupt.{int(time.time())}"
            try:
                os.replace(part_path, quarantine_path)
            except OSError:
                part_path.unlink(missing_ok=True)
            meta_path.unlink(missing_ok=True)
            if progress_cb is not None:
                progress_cb(
                    TransferProgress(
                        phase="failed",
                        bytes_done=spec.size_bytes,
                        bytes_total=spec.size_bytes,
                        percent=100.0,
                        speed_bps=0.0,
                        eta_seconds=None,
                    )
                )
            raise ChecksumMismatchError(
                f"SHA-256 mismatch for {spec.filename}: expected {spec.sha256.lower()}, "
                f"got {actual_sha}"
            )

        os.replace(part_path, final_path)
        meta_path.unlink(missing_ok=True)

        elapsed_total = max(1e-6, time.monotonic() - started_at)
        avg_speed = session_downloaded / elapsed_total
        if progress_cb is not None:
            progress_cb(
                TransferProgress(
                    phase="done",
                    bytes_done=spec.size_bytes,
                    bytes_total=spec.size_bytes,
                    percent=100.0,
                    speed_bps=round(avg_speed, 2),
                    eta_seconds=0.0,
                    local_path=str(final_path),
                )
            )

        return TransferResult(
            local_path=final_path,
            skipped=False,
            bytes_downloaded=session_downloaded,
            sha256=actual_sha,
        )


def download_iso(
    spec: IsoSpec,
    dest_dir: Path | str,
    *,
    transport: FileTransportLike | None = None,
    search_roots: Sequence[Path | str] | None = None,
    progress_cb: Callable[[TransferProgress], None] | None = None,
    pause_event: threading.Event | None = None,
    cancel_event: threading.Event | None = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
) -> TransferResult:
    """Convenience wrapper around IsoTransferClient.download."""
    client = IsoTransferClient(transport=transport, chunk_size=chunk_size)
    return client.download(
        spec,
        dest_dir,
        search_roots=search_roots,
        progress_cb=progress_cb,
        pause_event=pause_event,
        cancel_event=cancel_event,
    )


__all__ = [
    "DEFAULT_WINDOWS_SEARCH_ROOTS",
    "DEFAULT_CHUNK_SIZE",
    "DEFAULT_STREAM_READ_SIZE",
    "DEFAULT_TAIL_SIZE",
    "DEFAULT_MAX_RETRIES",
    "TransferError",
    "TransferPausedError",
    "TransferCancelledError",
    "InsufficientDiskSpaceError",
    "ChecksumMismatchError",
    "TransferResult",
    "default_iso_search_roots",
    "find_local_verified_iso",
    "IsoTransferClient",
    "download_iso",
]
