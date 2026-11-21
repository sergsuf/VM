"""Stdlib-only opaque-box E2E test helpers, harnesses, and fixtures.

All imports in this module are strictly from the Python standard library (plus
`tests.e2e`). Production modules (`remote.*`, `cli.*`) are never imported at
module top-level; they are resolved dynamically inside test execution via
`require_module` / `require_attr` so `--check-syntax` and `--list` work cleanly
even before target modules exist, while test execution fails loudly (never skips)
when required modules or files are missing.
"""

from __future__ import annotations

import ast
import collections
import dataclasses
import hashlib
import http.client
import http.server
import importlib
import inspect
import io
import json
import os
import pathlib
import queue
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import typing
import unittest
import urllib.parse
import zipfile

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - Python 3.10 fallback
    tomllib = None

from tests.e2e import ALL_FEATURE_IDS

REPO_ROOT: pathlib.Path = pathlib.Path(__file__).resolve().parents[2]
_FEATURE_SET: frozenset[str] = frozenset(ALL_FEATURE_IDS)


# ---------------------------------------------------------------------------
# Feature Tagging & Deferred Production Requirement Helpers
# ---------------------------------------------------------------------------


def feature(*ids: str) -> typing.Callable[[typing.Any], typing.Any]:
    """Attach validated Feature IDs (`F-01`..`F-24`) to a test method."""
    if not ids:
        raise ValueError("@feature(...) requires at least one feature ID")
    cleaned: list[str] = []
    for fid in ids:
        if not isinstance(fid, str) or fid not in _FEATURE_SET:
            raise ValueError(
                f"Invalid feature ID {fid!r}; must be one of {ALL_FEATURE_IDS[0]}..{ALL_FEATURE_IDS[-1]}"
            )
        if fid not in cleaned:
            cleaned.append(fid)

    def decorator(func: typing.Any) -> typing.Any:
        existing = tuple(getattr(func, "_feature_ids", ()))
        merged = tuple(dict.fromkeys((*existing, *cleaned)))
        func._feature_ids = merged
        return func

    return decorator


def require_module(name: str) -> typing.Any:
    """Dynamically import a production module, failing loudly on absence.

    Never skips: missing or broken modules raise `AssertionError` so every
    test tier fails deterministically when an implementation contract is unmet.
    """
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    try:
        return importlib.import_module(name)
    except Exception as exc:
        raise AssertionError(
            f"Required implementation module {name!r} could not be imported: "
            f"{type(exc).__name__}: {exc}"
        ) from exc


def require_attr(module_name: str, attr_name: str) -> typing.Any:
    """Import `module_name` and require contracted attribute `attr_name`."""
    mod = require_module(module_name)
    if not hasattr(mod, attr_name):
        raise AssertionError(
            f"Required contract symbol {attr_name!r} not found in module {module_name!r}"
        )
    return getattr(mod, attr_name)


def require_file(rel_path: str) -> pathlib.Path:
    """Resolve a repository-relative path and fail loudly if it does not exist."""
    path = REPO_ROOT / rel_path
    if not path.exists():
        raise AssertionError(f"Required contract file {rel_path!r} does not exist at {path}")
    return path


# ---------------------------------------------------------------------------
# Generic Polling, Hashing, Ports & Workspace Fixtures
# ---------------------------------------------------------------------------


def find_free_port(host: str = "127.0.0.1") -> int:
    """Bind an ephemeral TCP port on `host` and return the available port number."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        return int(sock.getsockname()[1])


def wait_until(
    predicate: typing.Callable[[], typing.Any],
    *,
    timeout: float = 15.0,
    interval: float = 0.05,
    desc: str = "condition",
    diagnostics: typing.Callable[[], str] | None = None,
) -> typing.Any:
    """Poll `predicate()` until truthy or fail with diagnostic context."""
    deadline = time.monotonic() + timeout
    last_exc: Exception | None = None
    while time.monotonic() < deadline:
        try:
            result = predicate()
            if result:
                return result
        except Exception as exc:
            last_exc = exc
        time.sleep(interval)
    diag = ""
    if diagnostics is not None:
        try:
            diag = f"\nDiagnostics:\n{diagnostics()}"
        except Exception as exc:
            diag = f"\nDiagnostics failed: {exc}"
    exc_info = f" (last error: {type(last_exc).__name__}: {last_exc})" if last_exc else ""
    raise AssertionError(f"Timed out after {timeout:.1f}s waiting for {desc}{exc_info}{diag}")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: pathlib.Path, chunk_size: int = 1024 * 1024) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class TempWorkspace:
    """Isolated temporary directory tree for E2E tests (Linux & Windows safe)."""

    def __init__(self, prefix: str = "max_e2e_") -> None:
        self._prefix = prefix
        self._tmpdir: tempfile.TemporaryDirectory[str] | None = None
        self.root: pathlib.Path = pathlib.Path()
        self.operator_isos: pathlib.Path = pathlib.Path()
        self.operator_assets: pathlib.Path = pathlib.Path()
        self.operator_updates: pathlib.Path = pathlib.Path()
        self.client_data: pathlib.Path = pathlib.Path()
        self.client_isos: pathlib.Path = pathlib.Path()
        self.client_enu: pathlib.Path = pathlib.Path()
        self.app_root: pathlib.Path = pathlib.Path()
        self.logs: pathlib.Path = pathlib.Path()
        self.staging: pathlib.Path = pathlib.Path()
        self.out: pathlib.Path = pathlib.Path()

    def __enter__(self) -> TempWorkspace:
        self._tmpdir = tempfile.TemporaryDirectory(prefix=self._prefix)
        self.root = pathlib.Path(self._tmpdir.name).resolve()
        self.operator_isos = self.root / "operator_isos"
        self.operator_assets = self.root / "operator_assets"
        self.operator_updates = self.root / "operator_updates"
        self.client_data = self.root / "client_data"
        self.client_isos = self.root / "client_isos"
        self.client_enu = self.root / "client_enu"
        self.app_root = self.root / "portable_app"
        self.logs = self.root / "logs"
        self.staging = self.root / "staging"
        self.out = self.root / "out"
        for d in (
            self.operator_isos,
            self.operator_assets,
            self.operator_updates,
            self.client_data,
            self.client_isos,
            self.client_enu,
            self.app_root,
            self.logs,
            self.staging,
            self.out,
        ):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        self.cleanup()

    def cleanup(self) -> None:
        if self._tmpdir is None:
            return
        for attempt in range(5):
            try:
                self._tmpdir.cleanup()
                self._tmpdir = None
                return
            except OSError:
                if attempt == 4:
                    shutil.rmtree(self.root, ignore_errors=True)
                    self._tmpdir = None
                    return
                time.sleep(0.1)


# ---------------------------------------------------------------------------
# Deterministic Synthetic Asset Builders
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class SyntheticAsset:
    path: pathlib.Path
    filename: str
    version: str
    size_bytes: int
    sha256: str


def generate_deterministic_bytes(size_bytes: int, seed: int = 1) -> bytes:
    """Produce a non-repeating deterministic byte sequence of exact `size_bytes`.

    Every 32-byte block is uniquely keyed by `(seed, block_index)` via SHA-256
    so chunk boundaries, offsets, and tail hashes are always unique.
    """
    buf = bytearray()
    block_idx = 0
    while len(buf) < size_bytes:
        digest = hashlib.sha256(f"max-e2e:{seed}:{block_idx}".encode("ascii")).digest()
        buf.extend(digest)
        block_idx += 1
    return bytes(buf[:size_bytes])


def create_synthetic_iso(
    directory: pathlib.Path,
    *,
    filename: str | None = None,
    version: str = "2025",
    size_bytes: int = 262144,
    seed: int = 1,
) -> SyntheticAsset:
    """Create a deterministic synthetic 3ds Max ISO file and return its metadata."""
    directory.mkdir(parents=True, exist_ok=True)
    name = filename or f"Autodesk_3ds_Max_{version}_EFGJKPS_Win_64bit.iso"
    dest = directory / name
    data = generate_deterministic_bytes(size_bytes, seed=seed)
    dest.write_bytes(data)
    return SyntheticAsset(
        path=dest,
        filename=name,
        version=version,
        size_bytes=len(data),
        sha256=sha256_bytes(data),
    )


def create_synthetic_post_config_assets(
    assets_dir: pathlib.Path,
    *,
    version: str = "2025",
    include_malicious_mzp: bool = False,
) -> dict[str, pathlib.Path]:
    """Create realistic synthetic `POST_CONFIG` assets for 2024 or 2025+."""
    assets_dir.mkdir(parents=True, exist_ok=True)
    created: dict[str, pathlib.Path] = {}

    # Themes / UI files
    themes_dir = assets_dir / "themes"
    themes_dir.mkdir(parents=True, exist_ok=True)
    clrx = themes_dir / "StudioDark.clrx"
    clrx.write_text("<ColorTheme name='StudioDark'/>\n", encoding="utf-8")
    created["clrx"] = clrx

    cuix = themes_dir / "StudioUI.cuix"
    cuix.write_text("<CUIX name='StudioUI'/>\n", encoding="utf-8")
    created["cuix"] = cuix

    ini = themes_dir / "3dsmax.ini"
    ini.write_text("[Performance]\nHardwareLock=0\n[Studio]\nProfile=Custom\n", encoding="utf-8")
    created["ini"] = ini

    # Version-specific menu / hotkey files
    menus_dir = assets_dir / "menus"
    menus_dir.mkdir(parents=True, exist_ok=True)
    mnux = menus_dir / "StudioQuads_2024.mnux"
    mnux.write_text("<Menu2024 name='StudioQuads'/>\n", encoding="utf-8")
    created["mnux"] = mnux

    mnx = menus_dir / "StudioQuads_2025.mnx"
    mnx.write_text(json.dumps({"format": "qt-mnx", "version": version}), encoding="utf-8")
    created["mnx"] = mnx

    hsx = menus_dir / "StudioHotkeys_2025.hsx"
    hsx.write_text(json.dumps({"format": "qt-hsx", "version": version}), encoding="utf-8")
    created["hsx"] = hsx

    # Scripts / macros / icons / .mzp
    scripts_dir = assets_dir / "scripts"
    nested_dir = scripts_dir / "studio_tools"
    nested_dir.mkdir(parents=True, exist_ok=True)

    startup_ms = scripts_dir / "studio_startup.ms"
    startup_ms.write_text("format \"[Studio] startup loaded\\n\"\n", encoding="utf-8")
    created["ms"] = startup_ms

    nested_ms = nested_dir / "helper_module.ms"
    nested_ms.write_text("fn studioHelper = (true)\n", encoding="utf-8")
    created["nested_ms"] = nested_ms

    mse_file = scripts_dir / "encrypted_tool.mse"
    mse_file.write_bytes(b"MSE_PLACEHOLDER_BINARY_DATA")
    created["mse"] = mse_file

    mcr_file = scripts_dir / "StudioTools.mcr"
    mcr_file.write_text(
        "macroScript StudioQuickRender category:\"Studio\" ( print \"OK\" )\n",
        encoding="utf-8",
    )
    created["mcr"] = mcr_file

    icons_dir = scripts_dir / "icons"
    icons_dir.mkdir(parents=True, exist_ok=True)
    icon_file = icons_dir / "studio_24i.bmp"
    icon_file.write_bytes(b"BM" + b"\x00" * 32)
    created["icon"] = icon_file

    mzp_path = scripts_dir / "StudioPack.mzp"
    with zipfile.ZipFile(mzp_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("mzp.run", "name \"StudioPack\"\nversion 1\ncopy \"pack_init.ms\" to \"$userScripts\"\n")
        zf.writestr("pack_init.ms", "format \"[StudioPack] installed\\n\"\n")
    created["mzp"] = mzp_path

    if include_malicious_mzp:
        evil_mzp = scripts_dir / "EvilTraversal.mzp"
        with zipfile.ZipFile(evil_mzp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("../../escaped_from_mzp.ms", "-- traversal payload\n")
        created["evil_mzp"] = evil_mzp

    return created


def create_app_update_zip(
    dest_zip: pathlib.Path,
    version: str,
    *,
    files: dict[str, bytes] | None = None,
    raw_entries: list[tuple[str, bytes]] | None = None,
) -> tuple[pathlib.Path, str, int]:
    """Build a zip archive for `app/` update testing and return `(path, sha256, size)`."""
    dest_zip.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(dest_zip, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        if raw_entries is not None:
            for arcname, payload in raw_entries:
                zf.writestr(arcname, payload)
        else:
            payload_map = files or {
                "remote/__init__.py": f"__version__ = {version!r}\n".encode("utf-8"),
                "remote/client/__init__.py": b"",
                "remote/client/main.py": (
                    "import json, pathlib, sys\n"
                    f"print('hello from release {version}')\n"
                    "sys.exit(0)\n"
                ).encode("utf-8"),
                "VERSION.txt": f"{version}\n".encode("utf-8"),
            }
            for rel, content in payload_map.items():
                zf.writestr(rel, content)
    size_bytes = dest_zip.stat().st_size
    digest = sha256_file(dest_zip)
    return dest_zip, digest, size_bytes


# ---------------------------------------------------------------------------
# Stdlib HTTP Client, SSE Collector & Fault-Injecting Servers/Proxies
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class HttpResponse:
    status: int
    headers: dict[str, str]
    body: bytes

    def header(self, name: str, default: str = "") -> str:
        return self.headers.get(name.lower(), default)

    def text(self, encoding: str = "utf-8") -> str:
        return self.body.decode(encoding, errors="replace")

    def json(self) -> typing.Any:
        return json.loads(self.body.decode("utf-8"))


def auth_headers(
    token: str,
    session_id: str,
    client_id: str | None = None,
    extra: dict[str, str] | None = None,
) -> dict[str, str]:
    hdrs: dict[str, str] = {
        "Authorization": f"Bearer {token}",
        "X-Session-Id": session_id,
    }
    if client_id is not None:
        hdrs["X-Client-Id"] = client_id
    if extra:
        hdrs.update(extra)
    return hdrs


def http_request(
    base_url: str,
    method: str,
    path: str,
    *,
    headers: dict[str, str] | None = None,
    body: bytes | str | dict[str, typing.Any] | None = None,
    timeout: float = 10.0,
) -> HttpResponse:
    """Perform a raw HTTP request preserving exact status codes (200/204/206/401/403/404/416)."""
    parsed = urllib.parse.urlsplit(base_url)
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    prefix = parsed.path.rstrip("/")
    full_path = f"{prefix}{path}" if path.startswith("/") else f"{prefix}/{path}"

    req_headers = dict(headers or {})
    payload: bytes | None = None
    if isinstance(body, dict):
        payload = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    elif isinstance(body, str):
        payload = body.encode("utf-8")
    elif isinstance(body, (bytes, bytearray)):
        payload = bytes(body)

    if payload is not None:
        req_headers.setdefault("Content-Length", str(len(payload)))

    conn_cls = (
        http.client.HTTPSConnection
        if parsed.scheme == "https"
        else http.client.HTTPConnection
    )
    conn = conn_cls(host, port, timeout=timeout)
    try:
        conn.request(method.upper(), full_path, body=payload, headers=req_headers)
        resp = conn.getresponse()
        raw = resp.read()
        hdr_map = {k.lower(): v for k, v in resp.getheaders()}
        return HttpResponse(status=resp.status, headers=hdr_map, body=raw)
    finally:
        conn.close()


@dataclasses.dataclass
class SSEEvent:
    event: str
    data_raw: str
    data: typing.Any
    event_id: str | None = None


class SSECollector:
    """Thread-safe Server-Sent Events (`text/event-stream`) reader using stdlib `http.client`."""

    def __init__(
        self,
        base_url: str,
        path: str = "/api/v1/ui/events",
        *,
        headers: dict[str, str] | None = None,
        connect_timeout: float = 5.0,
    ) -> None:
        self.base_url = base_url
        self.path = path
        self.headers = dict(headers or {})
        self.connect_timeout = connect_timeout
        self.status: int | None = None
        self.content_type: str = ""
        self.events: list[SSEEvent] = []
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._connected_event = threading.Event()
        self._error: Exception | None = None
        self._conn: http.client.HTTPConnection | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> SSECollector:
        self._thread = threading.Thread(target=self._run, name="e2e-sse-collector", daemon=True)
        self._thread.start()
        if not self._connected_event.wait(timeout=self.connect_timeout):
            raise AssertionError(
                f"Timed out connecting to SSE endpoint {self.base_url}{self.path}"
            )
        if self._error is not None:
            raise AssertionError(
                f"SSE connection to {self.base_url}{self.path} failed: {self._error}"
            )
        return self

    def stop(self) -> None:
        self._stop_event.set()
        conn = self._conn
        if conn is not None:
            try:
                if conn.sock is not None:
                    conn.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                conn.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def __enter__(self) -> SSECollector:
        return self.start()

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        self.stop()

    def snapshot_events(self) -> list[SSEEvent]:
        with self._lock:
            return list(self.events)

    def wait_for_event(
        self,
        predicate: typing.Callable[[SSEEvent], bool],
        *,
        timeout: float = 10.0,
    ) -> SSEEvent:
        def _check() -> SSEEvent | None:
            with self._lock:
                for ev in self.events:
                    if predicate(ev):
                        return ev
            return None

        return wait_until(
            _check,
            timeout=timeout,
            interval=0.05,
            desc="matching SSE event",
            diagnostics=lambda: f"Received {len(self.snapshot_events())} SSE events: "
            + repr([(e.event, e.data_raw[:120]) for e in self.snapshot_events()[-10:]]),
        )

    def _run(self) -> None:
        parsed = urllib.parse.urlsplit(self.base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        prefix = parsed.path.rstrip("/")
        full_path = f"{prefix}{self.path}"
        hdrs = {"Accept": "text/event-stream", "Cache-Control": "no-cache", **self.headers}
        try:
            conn = http.client.HTTPConnection(host, port, timeout=self.connect_timeout)
            self._conn = conn
            conn.request("GET", full_path, headers=hdrs)
            resp = conn.getresponse()
            self.status = resp.status
            self.content_type = resp.getheader("Content-Type", "")
            self._connected_event.set()
            if resp.status != 200:
                return
            if conn.sock is not None:
                conn.sock.settimeout(0.25)

            event_name = "message"
            event_id: str | None = None
            data_lines: list[str] = []
            buf = b""

            while not self._stop_event.is_set():
                try:
                    chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(1)
                except socket.timeout:
                    continue
                except OSError:
                    break
                if not chunk:
                    break
                buf += chunk
                while b"\n" in buf:
                    raw_line, buf = buf.split(b"\n", 1)
                    line = raw_line.rstrip(b"\r").decode("utf-8", errors="replace")
                    if line == "":
                        if data_lines:
                            raw_data = "\n".join(data_lines)
                            try:
                                parsed_data = json.loads(raw_data)
                            except json.JSONDecodeError:
                                parsed_data = raw_data
                            ev = SSEEvent(
                                event=event_name,
                                data_raw=raw_data,
                                data=parsed_data,
                                event_id=event_id,
                            )
                            with self._lock:
                                self.events.append(ev)
                            event_name = "message"
                            event_id = None
                            data_lines = []
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("event:"):
                        event_name = line[6:].strip()
                    elif line.startswith("data:"):
                        val = line[5:]
                        if val.startswith(" "):
                            val = val[1:]
                        data_lines.append(val)
                    elif line.startswith("id:"):
                        event_id = line[3:].strip()
        except Exception as exc:
            self._error = exc
            self._connected_event.set()


class FaultyRangeServer:
    """Standalone stdlib HTTP Range server with programmable transfer faults & request logging."""

    def __init__(self) -> None:
        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self._files: dict[str, tuple[str, pathlib.Path]] = {}
        self.request_log: list[dict[str, typing.Any]] = []
        self._lock = threading.Lock()

        # Programmable fault knobs
        self.fail_requests_with_503: int = 0
        self.drop_after_bytes: int | None = None
        self.drop_times: int = 0
        self.corrupt_byte_at: int | None = None
        self.chunk_delay_sec: float = 0.0
        self.ignore_range_header: bool = False
        self._503_served: int = 0
        self._drops_served: int = 0

        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def register_file(self, file_id: str, path: pathlib.Path, filename: str | None = None) -> str:
        with self._lock:
            self._files[file_id] = (filename or path.name, path)
        return f"{self.base_url}/files/{file_id}"

    def start(self) -> FaultyRangeServer:
        parent = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: typing.Any) -> None:
                return

            def do_GET(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                range_hdr = self.headers.get("Range")
                entry: dict[str, typing.Any] = {
                    "method": "GET",
                    "path": parsed.path,
                    "range": range_hdr,
                    "ts": time.monotonic(),
                }
                if len(parts) < 2 or parts[0] != "files":
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    entry["status"] = 404
                    with parent._lock:
                        parent.request_log.append(entry)
                    return

                file_id = parts[1]
                is_meta = len(parts) == 3 and parts[2] == "meta"
                with parent._lock:
                    info = parent._files.get(file_id)
                if info is None or not info[1].exists():
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    entry["status"] = 404
                    with parent._lock:
                        parent.request_log.append(entry)
                    return

                filename, fpath = info
                total = fpath.stat().st_size

                if is_meta:
                    meta = json.dumps(
                        {
                            "file_id": file_id,
                            "filename": filename,
                            "size_bytes": total,
                            "sha256": sha256_file(fpath),
                            "chunk_size": 65536,
                        }
                    ).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(meta)))
                    self.end_headers()
                    self.wfile.write(meta)
                    entry["status"] = 200
                    with parent._lock:
                        parent.request_log.append(entry)
                    return

                # Optional transient 503 fault
                with parent._lock:
                    if parent._503_served < parent.fail_requests_with_503:
                        parent._503_served += 1
                        should_503 = True
                    else:
                        should_503 = False
                if should_503:
                    body = b"Service Temporarily Unavailable"
                    self.send_response(503)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    entry["status"] = 503
                    with parent._lock:
                        parent.request_log.append(entry)
                    return

                start = 0
                end = total - 1 if total > 0 else 0
                status = 200
                if range_hdr and not parent.ignore_range_header:
                    m = re.match(r"^bytes=(\d*)-(\d*)$", range_hdr.strip())
                    if not m:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        entry["status"] = 416
                        with parent._lock:
                            parent.request_log.append(entry)
                        return
                    s_str, e_str = m.group(1), m.group(2)
                    if s_str == "" and e_str == "":
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        entry["status"] = 416
                        with parent._lock:
                            parent.request_log.append(entry)
                        return
                    if s_str != "":
                        start = int(s_str)
                        end = int(e_str) if e_str != "" else total - 1
                    else:
                        suffix = int(e_str)
                        start = max(0, total - suffix)
                        end = total - 1
                    if total == 0 or start >= total or start > end:
                        self.send_response(416)
                        self.send_header("Content-Range", f"bytes */{total}")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        entry["status"] = 416
                        with parent._lock:
                            parent.request_log.append(entry)
                        return
                    end = min(end, total - 1)
                    status = 206

                length = (end - start + 1) if total > 0 else 0
                with fpath.open("rb") as f:
                    f.seek(start)
                    data = bytearray(f.read(length))

                if parent.corrupt_byte_at is not None:
                    c_off = parent.corrupt_byte_at
                    if start <= c_off <= end:
                        data[c_off - start] ^= 0xFF

                if parent.chunk_delay_sec > 0:
                    time.sleep(parent.chunk_delay_sec)

                with parent._lock:
                    should_drop = (
                        parent.drop_after_bytes is not None
                        and parent._drops_served < parent.drop_times
                    )
                    if should_drop:
                        parent._drops_served += 1
                    drop_at = parent.drop_after_bytes

                self.send_response(status)
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(data)))
                if status == 206:
                    self.send_header("Content-Range", f"bytes {start}-{end}/{total}")
                self.end_headers()

                entry["status"] = status
                entry["start"] = start
                entry["end"] = end
                entry["length"] = len(data)
                with parent._lock:
                    parent.request_log.append(entry)

                if should_drop and drop_at is not None:
                    partial = bytes(data[: max(0, min(drop_at, len(data)))])
                    if partial:
                        self.wfile.write(partial)
                        self.wfile.flush()
                    self.close_connection = True
                    return

                self.wfile.write(bytes(data))
                self.wfile.flush()

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="e2e-faulty-range-server", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def __enter__(self) -> FaultyRangeServer:
        return self.start()

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        self.stop()

    def get_file_requests(self) -> list[dict[str, typing.Any]]:
        with self._lock:
            return [r for r in self.request_log if not r["path"].endswith("/meta")]


class FaultInjectingProxy:
    """Recording and fault-injecting HTTP reverse proxy in front of Operator or Relay.

    Allows 100% opaque-box verification of HTTP Range resume offsets, zero file
    requests on ISO local pre-check hit, transient 503 retries, mid-stream drops,
    byte corruption detection, and throttled transfer pause/abort.
    """

    def __init__(self, target_base_url: str) -> None:
        self.target_base_url = target_base_url.rstrip("/")
        self.port = find_free_port()
        self.base_url = f"http://127.0.0.1:{self.port}"
        self.request_log: list[dict[str, typing.Any]] = []
        self._lock = threading.Lock()

        # Programmable fault controls targeting `/files/<id>` GET downloads
        self.fail_first_n_file_requests: int = 0
        self.drop_after_bytes: int | None = None
        self.drop_times: int = 0
        self.corrupt_byte_at: int | None = None
        self.chunk_delay_sec: float = 0.0
        self._503_served: int = 0
        self._drops_served: int = 0

        self._httpd: http.server.ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    def _is_file_download_path(self, path: str) -> bool:
        clean = urllib.parse.urlsplit(path).path
        return "/files/" in clean and not clean.endswith("/meta")

    def start(self) -> FaultInjectingProxy:
        parent = self
        parsed_target = urllib.parse.urlsplit(self.target_base_url)
        target_host = parsed_target.hostname or "127.0.0.1"
        target_port = parsed_target.port or 80

        class ProxyHandler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt: str, *args: typing.Any) -> None:
                return

            def _forward(self) -> None:
                method = self.command
                path = self.path
                is_file_dl = method == "GET" and parent._is_file_download_path(path)
                content_len = int(self.headers.get("Content-Length", "0") or "0")
                req_body = self.rfile.read(content_len) if content_len > 0 else None

                fwd_headers: dict[str, str] = {}
                for k, v in self.headers.items():
                    if k.lower() in ("host", "connection", "proxy-connection", "transfer-encoding"):
                        continue
                    fwd_headers[k] = v

                log_entry: dict[str, typing.Any] = {
                    "method": method,
                    "path": path,
                    "range": self.headers.get("Range"),
                    "is_file_download": is_file_dl,
                    "ts": time.monotonic(),
                }

                if is_file_dl:
                    with parent._lock:
                        if parent._503_served < parent.fail_first_n_file_requests:
                            parent._503_served += 1
                            do_503 = True
                        else:
                            do_503 = False
                    if do_503:
                        msg = b"Injected 503"
                        self.send_response(503)
                        self.send_header("Content-Length", str(len(msg)))
                        self.end_headers()
                        self.wfile.write(msg)
                        log_entry["status"] = 503
                        log_entry["bytes"] = len(msg)
                        with parent._lock:
                            parent.request_log.append(log_entry)
                        return

                    if parent.chunk_delay_sec > 0:
                        time.sleep(parent.chunk_delay_sec)

                conn = http.client.HTTPConnection(target_host, target_port, timeout=35.0)
                try:
                    conn.request(method, path, body=req_body, headers=fwd_headers)
                    resp = conn.getresponse()
                    raw = bytearray(resp.read())
                    status = resp.status
                    resp_headers = resp.getheaders()
                finally:
                    conn.close()

                if is_file_dl and status in (200, 206):
                    if parent.corrupt_byte_at is not None:
                        c_off = parent.corrupt_byte_at
                        # Parse Content-Range to check if offset falls in this chunk
                        cr = dict((k.lower(), v) for k, v in resp_headers).get("content-range", "")
                        m = re.match(r"^bytes\s+(\d+)-(\d+)/(\d+)$", cr)
                        chunk_start = int(m.group(1)) if m else 0
                        if chunk_start <= c_off < chunk_start + len(raw):
                            raw[c_off - chunk_start] ^= 0xFF

                    with parent._lock:
                        should_drop = (
                            parent.drop_after_bytes is not None
                            and parent._drops_served < parent.drop_times
                        )
                        if should_drop:
                            parent._drops_served += 1
                        drop_at = parent.drop_after_bytes
                else:
                    should_drop = False
                    drop_at = None

                self.send_response(status)
                for k, v in resp_headers:
                    if k.lower() in ("transfer-encoding", "connection"):
                        continue
                    self.send_header(k, v)
                if not any(k.lower() == "content-length" for k, _ in resp_headers):
                    self.send_header("Content-Length", str(len(raw)))
                self.end_headers()

                log_entry["status"] = status
                log_entry["bytes"] = len(raw)
                with parent._lock:
                    parent.request_log.append(log_entry)

                if should_drop and drop_at is not None:
                    part = bytes(raw[: max(0, min(drop_at, len(raw)))])
                    if part:
                        self.wfile.write(part)
                        self.wfile.flush()
                    self.close_connection = True
                    return

                if raw:
                    self.wfile.write(bytes(raw))
                    self.wfile.flush()

            def do_GET(self) -> None:
                self._forward()

            def do_POST(self) -> None:
                self._forward()

            def do_PUT(self) -> None:
                self._forward()

            def do_DELETE(self) -> None:
                self._forward()

        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", self.port), ProxyHandler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="e2e-fault-proxy", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

    def __enter__(self) -> FaultInjectingProxy:
        return self.start()

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        self.stop()

    def file_downloads(self) -> list[dict[str, typing.Any]]:
        with self._lock:
            return [r for r in self.request_log if r.get("is_file_download")]


# ---------------------------------------------------------------------------
# Subprocess Management (Pipe-Drained, Never Deadlocks)
# ---------------------------------------------------------------------------


def build_subprocess_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = os.environ.copy()
    existing = env.get("PYTHONPATH", "")
    repo_str = str(REPO_ROOT)
    env["PYTHONPATH"] = f"{repo_str}{os.pathsep}{existing}" if existing else repo_str
    env["PYTHONUNBUFFERED"] = "1"
    if extra:
        env.update(extra)
    return env


def run_cli_subprocess(
    args: list[str],
    *,
    cwd: pathlib.Path = REPO_ROOT,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    cmd = [sys.executable, "-u", *args]
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=build_subprocess_env(env),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


class ManagedProcess:
    """Managed Python subprocess with background pipe-draining threads."""

    def __init__(
        self,
        args: list[str],
        *,
        name: str = "proc",
        cwd: pathlib.Path = REPO_ROOT,
        env: dict[str, str] | None = None,
        max_lines: int = 400,
    ) -> None:
        self.args = args
        self.name = name
        self.cwd = cwd
        self.env = build_subprocess_env(env)
        self._stdout_lines: collections.deque[str] = collections.deque(maxlen=max_lines)
        self._stderr_lines: collections.deque[str] = collections.deque(maxlen=max_lines)
        self._lock = threading.Lock()
        self.proc: subprocess.Popen[str] | None = None
        self._threads: list[threading.Thread] = []

    def start(self) -> ManagedProcess:
        cmd = [sys.executable, "-u", *self.args]
        self.proc = subprocess.Popen(
            cmd,
            cwd=str(self.cwd),
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            bufsize=1,
        )
        assert self.proc.stdout is not None and self.proc.stderr is not None
        for stream, bucket, suffix in (
            (self.proc.stdout, self._stdout_lines, "stdout"),
            (self.proc.stderr, self._stderr_lines, "stderr"),
        ):
            t = threading.Thread(
                target=self._drain,
                args=(stream, bucket),
                name=f"e2e-drain-{self.name}-{suffix}",
                daemon=True,
            )
            t.start()
            self._threads.append(t)
        return self

    def _drain(self, stream: typing.IO[str], bucket: collections.deque[str]) -> None:
        try:
            for line in stream:
                with self._lock:
                    bucket.append(line.rstrip("\r\n"))
        except Exception:
            pass
        finally:
            try:
                stream.close()
            except Exception:
                pass

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def returncode(self) -> int | None:
        return None if self.proc is None else self.proc.poll()

    def output_tail(self, lines: int = 40) -> str:
        with self._lock:
            out = list(self._stdout_lines)[-lines:]
            err = list(self._stderr_lines)[-lines:]
        return (
            f"[{self.name} rc={self.returncode()}]\n"
            f"--- stdout ---\n" + "\n".join(out) + "\n"
            f"--- stderr ---\n" + "\n".join(err)
        )

    def kill(self) -> None:
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.kill()
            except OSError:
                pass
            try:
                self.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                pass

    def stop(self, timeout: float = 5.0) -> int | None:
        if self.proc is None:
            return None
        if self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                try:
                    self.proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    pass
        for t in self._threads:
            t.join(timeout=1.0)
        return self.proc.returncode


# ---------------------------------------------------------------------------
# Snapshot Normalizers (Opaque-Box Defensive Key Extraction)
# ---------------------------------------------------------------------------


def normalize_clients(snapshot: dict[str, typing.Any]) -> dict[str, dict[str, typing.Any]]:
    """Extract `{client_id: client_record}` whether snapshot stores a dict or list."""
    raw = snapshot.get("clients")
    if isinstance(raw, dict):
        return {str(k): v for k, v in raw.items() if isinstance(v, dict)}
    if isinstance(raw, list):
        out: dict[str, dict[str, typing.Any]] = {}
        for item in raw:
            if isinstance(item, dict):
                cid = item.get("client_id") or item.get("id")
                if not cid and isinstance(item.get("state"), dict):
                    cid = item["state"].get("client_id")
                if cid:
                    out[str(cid)] = item
        return out
    return {}


def extract_client_state(client_record: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Return the SessionState dict inside a snapshot client record."""
    if isinstance(client_record.get("state"), dict):
        return client_record["state"]
    if isinstance(client_record.get("session_state"), dict):
        return client_record["session_state"]
    return client_record


def extract_client_system_status(client_record: dict[str, typing.Any]) -> dict[str, typing.Any]:
    """Return the SystemStatus dict inside a snapshot client record."""
    for key in ("system_status", "system", "status_info", "host_status"):
        val = client_record.get(key)
        if isinstance(val, dict):
            return val
    return client_record


def normalize_iso_catalog(snapshot: dict[str, typing.Any]) -> list[dict[str, typing.Any]]:
    """Extract ISO catalog list from snapshot under standard key aliases."""
    for key in ("iso_catalog", "isos", "catalog"):
        val = snapshot.get(key)
        if isinstance(val, list):
            return [x for x in val if isinstance(x, dict)]
        if isinstance(val, dict):
            return [x for x in val.values() if isinstance(x, dict)]
    return []


# ---------------------------------------------------------------------------
# Live Subprocess Harnesses (Relay, Operator, Client)
# ---------------------------------------------------------------------------


class LiveRelayHarness:
    """Context manager running `python -u -m remote.relay_server` on a free port."""

    def __init__(self, *, admin_token: str = "e2e-relay-admin-token") -> None:
        self.host = "127.0.0.1"
        self.port = find_free_port(self.host)
        self.admin_token = admin_token
        self.base_url = f"http://{self.host}:{self.port}"
        self.proc: ManagedProcess | None = None

    def start(self) -> LiveRelayHarness:
        require_module("remote.relay_server")
        args = [
            "-m",
            "remote.relay_server",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--admin-token",
            self.admin_token,
        ]
        self.proc = ManagedProcess(args, name=f"relay-{self.port}").start()

        def _ready() -> bool:
            assert self.proc is not None
            if not self.proc.is_alive():
                raise AssertionError(
                    f"Relay process exited prematurely:\n{self.proc.output_tail()}"
                )
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.settimeout(0.3)
                return s.connect_ex((self.host, self.port)) == 0

        wait_until(
            _ready,
            timeout=10.0,
            interval=0.05,
            desc=f"relay server listening on {self.base_url}",
            diagnostics=lambda: self.proc.output_tail() if self.proc else "",
        )
        return self

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.stop()
            self.proc = None

    def __enter__(self) -> LiveRelayHarness:
        return self.start()

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        self.stop()


class LiveOperatorHarness:
    """Context manager running `python -u -m remote.operator.main` on a free port."""

    def __init__(
        self,
        *,
        iso_dir: pathlib.Path,
        assets_dir: pathlib.Path,
        updates_dir: pathlib.Path,
        mode: str = "direct",
        relay_url: str | None = None,
        session_id: str | None = None,
        token: str | None = None,
    ) -> None:
        self.host = "127.0.0.1"
        self.port = find_free_port(self.host)
        self.base_url = f"http://{self.host}:{self.port}"
        self.iso_dir = iso_dir
        self.assets_dir = assets_dir
        self.updates_dir = updates_dir
        self.mode = mode
        self.relay_url = relay_url
        self.session_id = session_id or f"S-E2E-{self.port}"
        self.token = token or f"e2e_tok_{self.port}_{os.urandom(8).hex()}"
        self.proc: ManagedProcess | None = None

    def start(self) -> LiveOperatorHarness:
        require_module("remote.operator.main")
        args = [
            "-m",
            "remote.operator.main",
            "--host",
            self.host,
            "--port",
            str(self.port),
            "--iso-dir",
            str(self.iso_dir),
            "--assets-dir",
            str(self.assets_dir),
            "--updates-dir",
            str(self.updates_dir),
            "--mode",
            self.mode,
            "--session-id",
            self.session_id,
            "--token",
            self.token,
            "--no-browser",
        ]
        if self.relay_url:
            args.extend(["--relay-url", self.relay_url])

        self.proc = ManagedProcess(args, name=f"operator-{self.port}").start()

        def _ready() -> bool:
            assert self.proc is not None
            if not self.proc.is_alive():
                raise AssertionError(
                    f"Operator process exited prematurely:\n{self.proc.output_tail()}"
                )
            try:
                resp = http_request(self.base_url, "GET", "/api/v1/ui/snapshot", timeout=2.0)
                return resp.status == 200
            except OSError:
                return False

        wait_until(
            _ready,
            timeout=12.0,
            interval=0.08,
            desc=f"operator server ready at {self.base_url}",
            diagnostics=lambda: self.proc.output_tail() if self.proc else "",
        )
        return self

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.stop()
            self.proc = None

    def __enter__(self) -> LiveOperatorHarness:
        return self.start()

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        self.stop()

    def snapshot(self) -> dict[str, typing.Any]:
        resp = http_request(self.base_url, "GET", "/api/v1/ui/snapshot", timeout=5.0)
        if resp.status != 200:
            raise AssertionError(
                f"GET /api/v1/ui/snapshot returned {resp.status}: {resp.text()}"
            )
        return resp.json()

    def get_clients(self) -> dict[str, dict[str, typing.Any]]:
        return normalize_clients(self.snapshot())

    def get_iso_catalog(self) -> list[dict[str, typing.Any]]:
        return normalize_iso_catalog(self.snapshot())

    def get_connect_json(self) -> str:
        resp = http_request(self.base_url, "GET", "/api/v1/ui/pairing/connect.json", timeout=5.0)
        if resp.status != 200:
            raise AssertionError(
                f"GET /api/v1/ui/pairing/connect.json returned {resp.status}: {resp.text()}"
            )
        return resp.text()

    def send_command(
        self,
        client_id: str,
        cmd_type: typing.Any,
        payload: dict[str, typing.Any] | None = None,
    ) -> dict[str, typing.Any]:
        cmd_val = cmd_type.value if hasattr(cmd_type, "value") else str(cmd_type)
        resp = http_request(
            self.base_url,
            "POST",
            f"/api/v1/ui/clients/{urllib.parse.quote(client_id, safe='')}/command",
            body={"cmd_type": cmd_val, "payload": payload or {}},
            timeout=5.0,
        )
        if resp.status != 200:
            raise AssertionError(
                f"POST command {cmd_val} for {client_id} returned {resp.status}: {resp.text()}"
            )
        return resp.json()

    def wait_for_client(
        self,
        *,
        client_id: str | None = None,
        min_count: int = 1,
        timeout: float = 15.0,
    ) -> str:
        def _check() -> str | None:
            clients = self.get_clients()
            if client_id is not None:
                return client_id if client_id in clients else None
            if len(clients) >= min_count:
                return next(iter(clients.keys()))
            return None

        return wait_until(
            _check,
            timeout=timeout,
            interval=0.08,
            desc=f"client registration (id={client_id}, min={min_count})",
            diagnostics=lambda: f"Operator snapshot clients={list(self.get_clients().keys())}\n"
            + (self.proc.output_tail() if self.proc else ""),
        )

    def wait_for_stage(
        self,
        client_id: str,
        stage: str,
        status: str | None = None,
        *,
        timeout: float = 20.0,
    ) -> dict[str, typing.Any]:
        target_stage = stage.value if hasattr(stage, "value") else str(stage)
        target_status = (
            status.value if (status is not None and hasattr(status, "value")) else status
        )

        def _check() -> dict[str, typing.Any] | None:
            clients = self.get_clients()
            rec = clients.get(client_id)
            if not rec:
                return None
            st = extract_client_state(rec)
            if st.get("stage") != target_stage:
                return None
            if target_status is not None and st.get("status") != target_status:
                return None
            return rec

        return wait_until(
            _check,
            timeout=timeout,
            interval=0.08,
            desc=f"client {client_id} reaching stage={target_stage} status={target_status}",
            diagnostics=lambda: f"Current client record={json.dumps(self.get_clients().get(client_id), default=str)}\n"
            + (self.proc.output_tail() if self.proc else ""),
        )

    def open_sse(self) -> SSECollector:
        return SSECollector(self.base_url, "/api/v1/ui/events")


class LiveClientHarness:
    """Context manager running `python -u -m remote.client.main`."""

    def __init__(
        self,
        *,
        data_dir: pathlib.Path,
        iso_dir: pathlib.Path,
        connect_file: pathlib.Path | None = None,
        server_url: str | None = None,
        mode: str = "direct",
        session_id: str | None = None,
        token: str | None = None,
        driver: str = "simulated",
        headless: bool = True,
        status_port: int | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        self.data_dir = data_dir
        self.iso_dir = iso_dir
        self.connect_file = connect_file
        self.server_url = server_url
        self.mode = mode
        self.session_id = session_id
        self.token = token
        self.driver = driver
        self.headless = headless
        self.status_port = status_port if status_port is not None else find_free_port()
        self.status_url = f"http://127.0.0.1:{self.status_port}"
        self.env = env
        self.proc: ManagedProcess | None = None

    @property
    def state_file_path(self) -> pathlib.Path:
        return self.data_dir / "session_state.json"

    def _build_args(self) -> list[str]:
        args = [
            "-m",
            "remote.client.main",
            "--data-dir",
            str(self.data_dir),
            "--iso-dir",
            str(self.iso_dir),
            "--driver",
            self.driver,
            "--status-port",
            str(self.status_port),
        ]
        if self.headless:
            args.append("--headless")
        if self.connect_file is not None:
            args.extend(["--connect-file", str(self.connect_file)])
        else:
            args.extend(["--mode", self.mode])
            if self.server_url:
                args.extend(["--server-url", self.server_url])
            if self.session_id:
                args.extend(["--session-id", self.session_id])
            if self.token:
                args.extend(["--token", self.token])
        return args

    def start(self, *, wait_status_ui: bool = False) -> LiveClientHarness:
        require_module("remote.client.main")
        self.proc = ManagedProcess(
            self._build_args(), name=f"client-{self.status_port}", env=self.env
        ).start()
        if wait_status_ui:
            self.wait_for_status_ui()
        return self

    def wait_for_status_ui(self, timeout: float = 10.0) -> HttpResponse:
        def _check() -> HttpResponse | None:
            assert self.proc is not None
            if not self.proc.is_alive():
                raise AssertionError(
                    f"Client process exited prematurely:\n{self.proc.output_tail()}"
                )
            try:
                resp = http_request(self.status_url, "GET", "/", timeout=2.0)
                return resp if resp.status == 200 else None
            except OSError:
                return None

        return wait_until(
            _check,
            timeout=timeout,
            interval=0.08,
            desc=f"client status UI ready at {self.status_url}",
            diagnostics=lambda: self.proc.output_tail() if self.proc else "",
        )

    def kill(self) -> None:
        if self.proc is not None:
            self.proc.kill()
            self.proc = None

    def stop(self) -> None:
        if self.proc is not None:
            self.proc.stop()
            self.proc = None

    def restart(self, *, wait_status_ui: bool = False) -> LiveClientHarness:
        self.kill()
        return self.start(wait_status_ui=wait_status_ui)

    def __enter__(self) -> LiveClientHarness:
        return self.start()

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Simulated Driver Discovery & Protocol-Based Custom Agent Runner
# ---------------------------------------------------------------------------


def discover_simulated_drivers() -> typing.Any:
    """Return a `DriverBundle` of default simulated drivers using only pinned Protocols."""
    base_mod = require_module("remote.drivers.base")
    sim_mod = require_module("remote.drivers.simulated")
    bundle_cls = getattr(base_mod, "DriverBundle")

    for factory_name in (
        "create_simulated_drivers",
        "build_simulated_drivers",
        "make_simulated_drivers",
        "simulated_driver_bundle",
    ):
        factory = getattr(sim_mod, factory_name, None)
        if callable(factory):
            return factory()

    sys_cls = getattr(sim_mod, "SimulatedSystemDriver", None)
    inst_cls = getattr(sim_mod, "SimulatedInstallerDriver", None)
    fr_cls = getattr(sim_mod, "SimulatedFirstRunDriver", None)
    pc_cls = getattr(sim_mod, "SimulatedPostConfigDriver", None)
    if all(callable(c) for c in (sys_cls, inst_cls, fr_cls, pc_cls)):
        return bundle_cls(
            system=sys_cls(),
            installer=inst_cls(),
            first_run=fr_cls(),
            post_config=pc_cls(),
        )
    raise AssertionError(
        "remote.drivers.simulated must expose SimulatedSystemDriver, SimulatedInstallerDriver, "
        "SimulatedFirstRunDriver, SimulatedPostConfigDriver or a DriverBundle factory"
    )


class CustomDriverAgentHarness:
    """Runs a real `ClientAgent` thread connected over real HTTP to a live Operator/Relay,
    injecting custom Protocol-compliant drivers without coupling to internal simulator kwargs.
    """

    def __init__(
        self,
        *,
        server_url: str,
        session_id: str,
        token: str,
        data_dir: pathlib.Path,
        iso_dir: pathlib.Path,
        mode: str = "direct",
        system: typing.Any = None,
        installer: typing.Any = None,
        first_run: typing.Any = None,
        post_config: typing.Any = None,
        client_id: str | None = None,
    ) -> None:
        self.server_url = server_url
        self.session_id = session_id
        self.token = token
        self.data_dir = data_dir
        self.iso_dir = iso_dir
        self.mode = mode
        self._overrides = {
            "system": system,
            "installer": installer,
            "first_run": first_run,
            "post_config": post_config,
        }
        self.client_id = client_id
        self._agent: typing.Any = None
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._error: Exception | None = None

    def start(self) -> CustomDriverAgentHarness:
        base_mod = require_module("remote.drivers.base")
        auth_mod = require_module("remote.auth")
        agent_mod = require_module("remote.client.agent")

        default_bundle = discover_simulated_drivers()
        bundle = base_mod.DriverBundle(
            system=self._overrides["system"] or default_bundle.system,
            installer=self._overrides["installer"] or default_bundle.installer,
            first_run=self._overrides["first_run"] or default_bundle.first_run,
            post_config=self._overrides["post_config"] or default_bundle.post_config,
        )
        pairing = auth_mod.PairingConfig(
            mode=self.mode,
            server_url=self.server_url,
            session_id=self.session_id,
            token=self.token,
        )

        agent_cls = getattr(agent_mod, "ClientAgent", None)
        if agent_cls is None:
            raise AssertionError("remote.client.agent must expose ClientAgent")

        sig = inspect.signature(agent_cls.__init__)
        kwargs: dict[str, typing.Any] = {}
        params = sig.parameters
        if "pairing" in params:
            kwargs["pairing"] = pairing
        if "config" in params and "pairing" not in params:
            kwargs["config"] = pairing
        if "drivers" in params:
            kwargs["drivers"] = bundle
        if "driver_bundle" in params:
            kwargs["driver_bundle"] = bundle
        if "data_dir" in params:
            kwargs["data_dir"] = self.data_dir
        if "iso_dir" in params:
            kwargs["iso_dir"] = self.iso_dir
        if "client_id" in params and self.client_id is not None:
            kwargs["client_id"] = self.client_id
        if "server_url" in params and "pairing" not in params:
            kwargs["server_url"] = self.server_url
        if "session_id" in params and "pairing" not in params:
            kwargs["session_id"] = self.session_id
        if "token" in params and "pairing" not in params:
            kwargs["token"] = self.token
        if "mode" in params and "pairing" not in params:
            kwargs["mode"] = self.mode
        if "transport" in params:
            transport_mod = require_module("remote.transport")
            t_cls = (
                getattr(transport_mod, "RelayTransport", None)
                if self.mode == "relay"
                else getattr(transport_mod, "DirectTransport", None)
                or getattr(transport_mod, "HttpTransport", None)
            )
            if t_cls is not None:
                t_sig = inspect.signature(t_cls.__init__)
                t_kwargs: dict[str, typing.Any] = {}
                for k, v in (
                    ("base_url", self.server_url),
                    ("server_url", self.server_url),
                    ("relay_url", self.server_url),
                    ("session_id", self.session_id),
                    ("token", self.token),
                    ("client_id", self.client_id or "custom-e2e-client"),
                    ("pairing", pairing),
                ):
                    if k in t_sig.parameters:
                        t_kwargs[k] = v
                kwargs["transport"] = t_cls(**t_kwargs)

        self._agent = agent_cls(**kwargs)

        def _runner() -> None:
            try:
                if hasattr(self._agent, "run_forever"):
                    self._agent.run_forever()
                elif hasattr(self._agent, "run"):
                    self._agent.run()
                elif hasattr(self._agent, "start"):
                    self._agent.start()
                    self._stop_event.wait()
                else:
                    raise AssertionError("ClientAgent exposes neither run(), run_forever(), nor start()")
            except SystemExit:
                return
            except Exception as exc:
                self._error = exc

        self._thread = threading.Thread(target=_runner, name="e2e-custom-agent", daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._agent is not None:
            for m in ("stop", "shutdown", "close"):
                fn = getattr(self._agent, m, None)
                if callable(fn):
                    try:
                        fn()
                    except Exception:
                        pass
                    break
        if self._thread is not None:
            self._thread.join(timeout=5.0)

    def __enter__(self) -> CustomDriverAgentHarness:
        return self.start()

    def __exit__(self, exc_type: typing.Any, exc: typing.Any, tb: typing.Any) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# Packaging & Bootstrap Opaque-Box CLI Helpers
# ---------------------------------------------------------------------------


def _probe_help(script_rel: str) -> str:
    script = require_file(script_rel)
    res = run_cli_subprocess([str(script), "--help"], timeout=15.0)
    if res.returncode != 0:
        raise AssertionError(
            f"{script_rel} --help failed (rc={res.returncode}):\n{res.stdout}\n{res.stderr}"
        )
    return f"{res.stdout}\n{res.stderr}"


def invoke_build_app_update(
    version: str,
    out_dir: pathlib.Path,
    *,
    min_runtime_version: str | None = None,
    notes: str | None = None,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Invoke `packaging/build_app_update.py` using its documented `--help` flags."""
    script = require_file("packaging/build_app_update.py")
    help_text = _probe_help("packaging/build_app_update.py")
    out_dir.mkdir(parents=True, exist_ok=True)

    args = [str(script), "--version", version]
    for flag in ("--out-dir", "--output-dir", "--output", "-o"):
        if flag in help_text:
            args.extend([flag, str(out_dir)])
            break
    if min_runtime_version is not None:
        for flag in ("--min-runtime-version", "--min-runtime"):
            if flag in help_text:
                args.extend([flag, min_runtime_version])
                break
    if notes is not None and "--notes" in help_text:
        args.extend(["--notes", notes])
    return run_cli_subprocess(args, timeout=timeout)


def invoke_build_portable_client(
    out_dir: pathlib.Path,
    *,
    version: str = "1.0.0",
    offline: bool = True,
    timeout: float = 30.0,
) -> subprocess.CompletedProcess[str]:
    """Invoke `packaging/build_portable_client.py` in offline/layout mode."""
    script = require_file("packaging/build_portable_client.py")
    help_text = _probe_help("packaging/build_portable_client.py")
    out_dir.mkdir(parents=True, exist_ok=True)

    args = [str(script)]
    for flag in ("--out-dir", "--output-dir", "--output", "-o"):
        if flag in help_text:
            args.extend([flag, str(out_dir)])
            break
    if "--version" in help_text:
        args.extend(["--version", version])
    if offline:
        for flag in ("--offline", "--no-download", "--skip-downloads", "--skeleton", "--layout-only"):
            if flag in help_text:
                args.append(flag)
                break
    return run_cli_subprocess(args, timeout=timeout)


def prepare_bootstrap_sandbox(app_root: pathlib.Path) -> pathlib.Path:
    """Copy `remote/client/bootstrap.py` into `app_root/runtime/bootstrap.py`.

    This ensures both `__file__`-relative (`Path(__file__).parent.parent`) and
    `cwd`-relative root resolution land cleanly on `app_root`.
    """
    src = require_file("remote/client/bootstrap.py")
    runtime_dir = app_root / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    dst = runtime_dir / "bootstrap.py"
    shutil.copy2(src, dst)
    version_file = runtime_dir / "version.json"
    if not version_file.exists():
        version_file.write_text(json.dumps({"runtime_version": "1.0.0"}), encoding="utf-8")
    return dst


def build_bootstrap_command_args(
    bootstrap_path: pathlib.Path,
    app_root: pathlib.Path,
    extra_args: list[str] | None = None,
) -> list[str]:
    """Build argument list for invoking `bootstrap.py` cleanly inside `app_root`."""
    probe = run_cli_subprocess([str(bootstrap_path), "--help"], cwd=app_root, timeout=10.0)
    help_text = f"{probe.stdout}\n{probe.stderr}"
    args = [str(bootstrap_path)]
    if probe.returncode == 0:
        for flag in ("--root", "--app-root", "--base-dir"):
            if flag in help_text:
                args.extend([flag, str(app_root)])
                break
        for flag in ("--no-elevate", "--skip-elevation", "--no-uac"):
            if flag in help_text:
                args.append(flag)
                break
    if extra_args:
        args.extend(extra_args)
    return args


def run_bootstrap_in_sandbox(
    app_root: pathlib.Path,
    *,
    extra_args: list[str] | None = None,
    timeout: float = 20.0,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    bootstrap_path = prepare_bootstrap_sandbox(app_root)
    args = build_bootstrap_command_args(bootstrap_path, app_root, extra_args=extra_args)
    return run_cli_subprocess(args, cwd=app_root, env=env, timeout=timeout)


# ---------------------------------------------------------------------------
# Pure-Stdlib TOML / YAML / Coverage Audit Helpers
# ---------------------------------------------------------------------------


def parse_pyproject_toml(path: pathlib.Path) -> dict[str, typing.Any]:
    """Parse `pyproject.toml` using stdlib `tomllib` (3.11+) with a 3.10 fallback."""
    raw = path.read_text(encoding="utf-8")
    if tomllib is not None:
        return tomllib.loads(raw)

    # Minimal stdlib fallback for Python 3.10 covering sections, arrays, strings
    data: dict[str, typing.Any] = {}
    section_path: list[str] = []
    buf_key: str | None = None
    buf_val: list[str] = []

    def _set(keys: list[str], k: str, v: typing.Any) -> None:
        cur = data
        for part in keys:
            cur = cur.setdefault(part, {})
        cur[k] = v

    for raw_line in raw.splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if buf_key is not None:
            buf_val.append(line)
            if "]" in line:
                joined = " ".join(buf_val)
                items = re.findall(r'"([^"]*)"|\'([^\']*)\'', joined)
                _set(section_path, buf_key, [a or b for a, b in items])
                buf_key = None
                buf_val = []
            continue
        if line.startswith("[") and line.endswith("]"):
            section_path = [p.strip() for p in line[1:-1].split(".")]
            continue
        if "=" in line:
            k, v = line.split("=", 1)
            k = k.strip().strip('"').strip("'")
            v = v.strip()
            if v.startswith("[") and not v.endswith("]"):
                buf_key = k
                buf_val = [v]
            elif v.startswith("[") and v.endswith("]"):
                items = re.findall(r'"([^"]*)"|\'([^\']*)\'', v)
                _set(section_path, k, [a or b for a, b in items])
            else:
                _set(section_path, k, v.strip('"').strip("'"))
    return data


def stdlib_check_yaml_well_formed(path: pathlib.Path) -> str:
    """Validate basic YAML well-formedness using stdlib only (no `PyYAML` import)."""
    text = path.read_text(encoding="utf-8")
    if not text.strip():
        raise AssertionError(f"YAML file {path} is empty")
    for lineno, line in enumerate(text.splitlines(), 1):
        if "\t" in line.split("#", 1)[0]:
            raise AssertionError(f"Tab indentation in YAML {path}:{lineno}")
    return text


def iter_test_cases(suite: unittest.TestSuite) -> typing.Iterator[unittest.TestCase]:
    """Recursively yield every `unittest.TestCase` inside a `TestSuite`."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from iter_test_cases(item)
        else:
            yield item


def extract_feature_coverage(
    tier_suites: dict[int, unittest.TestSuite],
) -> tuple[dict[str, dict[int, list[str]]], list[str]]:
    """Collect feature coverage `{F-XX: {tier: [test_id]}}` and list untagged tests."""
    coverage: dict[str, dict[int, list[str]]] = {
        fid: {tier: [] for tier in tier_suites} for fid in ALL_FEATURE_IDS
    }
    untagged: list[str] = []
    for tier, suite in tier_suites.items():
        for tc in iter_test_cases(suite):
            method = getattr(tc, tc._testMethodName, None)
            fids = tuple(getattr(method, "_feature_ids", ()))
            tid = tc.id()
            if not fids:
                untagged.append(tid)
                continue
            for fid in fids:
                if fid in coverage:
                    coverage[fid][tier].append(tid)
    return coverage, untagged


def audit_python_file_stdlib_and_no_skips(path: pathlib.Path) -> list[str]:
    """AST-audit a `tests/e2e/*.py` file for stdlib-only imports and zero skip calls."""
    errors: list[str] = []
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    stdlib_names = set(sys.stdlib_module_names) | {"__future__"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top not in stdlib_names and alias.name != "tests" and not alias.name.startswith("tests.e2e"):
                    errors.append(
                        f"{path.name}:{node.lineno}: non-stdlib import {alias.name!r} forbidden in tests/e2e"
                    )
        elif isinstance(node, ast.ImportFrom):
            if node.level > 0:
                continue
            mod = node.module or ""
            top = mod.split(".", 1)[0]
            if top not in stdlib_names and not mod.startswith("tests.e2e"):
                errors.append(
                    f"{path.name}:{node.lineno}: non-stdlib from-import {mod!r} forbidden in tests/e2e"
                )
        elif isinstance(node, ast.Attribute):
            if node.attr in ("skip", "skipIf", "skipUnless", "skipTest", "SkipTest"):
                errors.append(
                    f"{path.name}:{node.lineno}: forbidden skip attribute {node.attr!r}"
                )
        elif isinstance(node, ast.Name):
            if node.id in ("SkipTest", "skipIf", "skipUnless"):
                errors.append(
                    f"{path.name}:{node.lineno}: forbidden skip identifier {node.id!r}"
                )
    return errors
