"""Unit & integration tests for remote.iso_catalog and remote.iso_transfer."""

from __future__ import annotations

import hashlib
import http.client
import json
import os
import shutil
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from remote.iso_catalog import (
    IsoCatalog,
    RangeNotSatisfiableError,
    extract_3dsmax_version,
    parse_range_header,
)
from remote.iso_transfer import (
    ChecksumMismatchError,
    InsufficientDiskSpaceError,
    IsoTransferClient,
    TransferCancelledError,
    TransferPausedError,
    find_local_verified_iso,
)
from remote.protocol import IsoSpec, TransferProgress


class _CatalogTestServer:
    def __init__(self, catalog: IsoCatalog) -> None:
        self.catalog = catalog
        self.requests: list[tuple[str, str, str | None]] = []

        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_HEAD(self) -> None:
                parent.requests.append(("HEAD", self.path, self.headers.get("Range")))
                if self.path.startswith("/api/v1/files/"):
                    fid = self.path.split("/")[4]
                    parent.catalog.serve_http_file(self, fid, head_only=True)
                    return
                self.send_error(404)

            def do_GET(self) -> None:
                parent.requests.append(("GET", self.path, self.headers.get("Range")))
                if self.path.startswith("/api/v1/files/") and self.path.endswith("/meta"):
                    fid = self.path.split("/")[4]
                    meta = parent.catalog.get_meta(fid)
                    if meta is None:
                        self.send_error(404)
                        return
                    body = json.dumps(meta).encode("utf-8")
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                if self.path.startswith("/api/v1/files/"):
                    fid = self.path.split("/")[4]
                    parent.catalog.serve_http_file(self, fid)
                    return
                self.send_error(404)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def port(self) -> int:
        return int(self._httpd.server_address[1])

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3.0)


class TestIsoCatalog(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.iso_dir = Path(self._tmp.name) / "isos"
        self.iso_dir.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_extract_3dsmax_version_filtering(self) -> None:
        self.assertEqual(extract_3dsmax_version("Autodesk_3ds_Max_2024_Win_64bit.iso"), "2024")
        self.assertEqual(extract_3dsmax_version("3dsMax2025.ISO"), "2025")
        self.assertEqual(extract_3dsmax_version("3ds max 2028_ru.iso"), "2028")
        self.assertIsNone(extract_3dsmax_version("Autodesk_3ds_Max_2023_Win.iso"))
        self.assertIsNone(extract_3dsmax_version("Autodesk_3ds_Max_2029_Win.iso"))
        self.assertIsNone(extract_3dsmax_version("Autodesk_Maya_2025_Win_64bit.iso"))
        self.assertIsNone(extract_3dsmax_version("Autodesk_3ds_Max_2025_Win.txt"))

    def test_scan_creates_sidecar_and_invalidates_on_change(self) -> None:
        iso_path = self.iso_dir / "Autodesk_3ds_Max_2025_Win_64bit.iso"
        data1 = os.urandom(64 * 1024)
        iso_path.write_bytes(data1)
        (self.iso_dir / "Autodesk_Maya_2025.iso").write_bytes(b"maya")
        (self.iso_dir / "Autodesk_3ds_Max_2023.iso").write_bytes(b"old")
        (self.iso_dir / "notes.txt").write_bytes(b"notes")

        catalog = IsoCatalog(self.iso_dir)
        specs = catalog.scan()
        self.assertEqual(len(specs), 1)
        spec = specs[0]
        self.assertEqual(spec.version, "2025")
        self.assertEqual(spec.sha256, hashlib.sha256(data1).hexdigest())

        sidecar = Path(f"{iso_path}.sha256.json")
        self.assertTrue(sidecar.is_file())

        # Second scan with unchanged size/mtime should hit cache without recomputing
        with mock.patch("remote.iso_catalog.compute_file_sha256") as m_hash:
            specs2 = catalog.scan()
            m_hash.assert_not_called()
            self.assertEqual(specs2[0].sha256, spec.sha256)

        # Modify content & size -> sidecar invalidated
        data2 = os.urandom(64 * 1024 + 17)
        iso_path.write_bytes(data2)
        specs3 = catalog.scan()
        self.assertEqual(specs3[0].size_bytes, len(data2))
        self.assertEqual(specs3[0].sha256, hashlib.sha256(data2).hexdigest())

        # Modify mtime only (same size) -> sidecar invalidated
        st = iso_path.stat()
        os.utime(iso_path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000))
        with mock.patch(
            "remote.iso_catalog.compute_file_sha256",
            wraps=lambda p, chunk_size=1048576: hashlib.sha256(p.read_bytes()).hexdigest(),
        ) as m_hash:
            catalog.scan()
            self.assertEqual(m_hash.call_count, 1)

    def test_parse_range_header_edge_cases(self) -> None:
        self.assertEqual(parse_range_header("bytes=0-0", 100), (0, 0))
        self.assertEqual(parse_range_header("bytes=99-99", 100), (99, 99))
        self.assertEqual(parse_range_header("bytes=50-", 100), (50, 99))
        self.assertEqual(parse_range_header("bytes=-10", 100), (90, 99))
        self.assertEqual(parse_range_header("bytes=10-500", 100), (10, 99))

        for bad in ("bytes=100-110", "bytes=50-10", "bytes=-", "bytes=0-1,2-3", "items=0-5"):
            with self.assertRaises(RangeNotSatisfiableError):
                parse_range_header(bad, 100)

    def test_http_range_200_206_416_head_and_if_range(self) -> None:
        iso_path = self.iso_dir / "Autodesk_3ds_Max_2026_Win.iso"
        payload = bytes((i % 251) for i in range(128 * 1024))
        iso_path.write_bytes(payload)

        catalog = IsoCatalog(self.iso_dir)
        spec = catalog.scan()[0]
        entry = catalog.get_entry(spec.file_id)
        assert entry is not None

        srv = _CatalogTestServer(catalog)
        try:
            conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=5.0)
            # 200 OK full
            conn.request("GET", f"/api/v1/files/{spec.file_id}")
            r = conn.getresponse()
            body = r.read()
            self.assertEqual(r.status, 200)
            self.assertEqual(r.getheader("Accept-Ranges"), "bytes")
            self.assertEqual(body, payload)

            # HEAD
            conn.request("HEAD", f"/api/v1/files/{spec.file_id}")
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 200)
            self.assertEqual(int(r.getheader("Content-Length") or "0"), len(payload))

            # 206 single byte
            conn.request("GET", f"/api/v1/files/{spec.file_id}", headers={"Range": "bytes=0-0"})
            r = conn.getresponse()
            self.assertEqual(r.status, 206)
            self.assertEqual(r.getheader("Content-Range"), f"bytes 0-0/{len(payload)}")
            self.assertEqual(r.read(), payload[:1])

            # 206 last byte
            last = len(payload) - 1
            conn.request(
                "GET",
                f"/api/v1/files/{spec.file_id}",
                headers={"Range": f"bytes={last}-{last}"},
            )
            r = conn.getresponse()
            self.assertEqual(r.status, 206)
            self.assertEqual(r.read(), payload[-1:])

            # 416 out of bounds
            conn.request(
                "GET",
                f"/api/v1/files/{spec.file_id}",
                headers={"Range": f"bytes={len(payload)}-{len(payload) + 10}"},
            )
            r = conn.getresponse()
            r.read()
            self.assertEqual(r.status, 416)
            self.assertEqual(r.getheader("Content-Range"), f"bytes */{len(payload)}")

            # If-Range mismatch -> 200 OK full body
            conn.request(
                "GET",
                f"/api/v1/files/{spec.file_id}",
                headers={"Range": "bytes=0-99", "If-Range": '"stale-etag"'},
            )
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), payload)

            # Malformed If-Range HTTP-date -> 200 OK full body (no crash)
            conn.request(
                "GET",
                f"/api/v1/files/{spec.file_id}",
                headers={"Range": "bytes=0-99", "If-Range": "not-a-valid-http-date"},
            )
            r = conn.getresponse()
            self.assertEqual(r.status, 200)
            self.assertEqual(r.read(), payload)

            # If-Range match -> 206 Partial Content
            conn.request(
                "GET",
                f"/api/v1/files/{spec.file_id}",
                headers={"Range": "bytes=0-99", "If-Range": entry.etag},
            )
            r = conn.getresponse()
            self.assertEqual(r.status, 206)
            self.assertEqual(r.read(), payload[:100])
            conn.close()
        finally:
            srv.close()


class _FaultyRangeServer:
    """HTTP server injecting transient 503s and mid-stream socket drops."""

    def __init__(self, data: bytes, *, fail_503_count: int = 1, drop_after: int | None = None) -> None:
        self.data = data
        self.fail_503_remaining = fail_503_count
        self.drop_after = drop_after
        self.requests = 0

        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:  # noqa: A002
                return

            def do_GET(self) -> None:
                parent.requests += 1
                if parent.fail_503_remaining > 0:
                    parent.fail_503_remaining -= 1
                    self.send_response(503)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                rng = self.headers.get("Range")
                if rng:
                    start, end = parse_range_header(rng, len(parent.data))
                    slice_data = parent.data[start : end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(parent.data)}")
                    self.send_header("Content-Length", str(len(slice_data)))
                    self.end_headers()
                else:
                    slice_data = parent.data
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(slice_data)))
                    self.end_headers()

                if parent.drop_after is not None and parent.drop_after > 0:
                    cut = parent.drop_after
                    parent.drop_after = None
                    self.wfile.write(slice_data[:cut])
                    self.wfile.flush()
                    self.connection.close()
                    return

                self.wfile.write(slice_data)

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._httpd.daemon_threads = True
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    @property
    def url(self) -> str:
        port = int(self._httpd.server_address[1])
        return f"http://127.0.0.1:{port}/iso.iso"

    def close(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        self._thread.join(timeout=3.0)


class TestIsoTransfer(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.src_dir = self.root / "src"
        self.dest_dir = self.root / "dest"
        self.local_root = self.root / "local_install"
        self.src_dir.mkdir()
        self.dest_dir.mkdir()
        self.local_root.mkdir()

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _make_iso(self, name: str = "Autodesk_3ds_Max_2025_Win.iso", size: int = 256 * 1024) -> tuple[Path, bytes, IsoSpec]:
        data = os.urandom(size)
        path = self.src_dir / name
        path.write_bytes(data)
        catalog = IsoCatalog(self.src_dir)
        spec = catalog.scan()[0]
        return path, data, spec

    def test_local_precheck_skips_valid_and_rejects_corrupt(self) -> None:
        _, data, spec = self._make_iso(size=64 * 1024)
        local_iso = self.local_root / spec.filename
        local_iso.write_bytes(data)

        events: list[TransferProgress] = []
        client = IsoTransferClient()
        res = client.download(
            spec,
            self.dest_dir,
            search_roots=[self.local_root],
            progress_cb=events.append,
        )
        self.assertTrue(res.skipped)
        self.assertEqual(res.local_path, local_iso)
        self.assertEqual(events[0].phase, "checking")
        self.assertEqual(events[-1].phase, "skipped")
        self.assertEqual(events[-1].percent, 100.0)

        # Flip 1 byte in local file (same name, same size) -> must reject
        corrupt = bytearray(data)
        corrupt[1234] ^= 0xFF
        local_iso.write_bytes(bytes(corrupt))
        self.assertIsNone(find_local_verified_iso(spec, search_roots=[self.local_root]))

    def test_torn_tail_resume_and_pre_file_id_compatibility(self) -> None:
        _, data, spec = self._make_iso(size=256 * 1024)
        half = 128 * 1024
        tail_size = 4096

        # Pre-populate .part with valid first half, but corrupt last 512 bytes (torn tail)
        part_path = self.dest_dir / f"{spec.filename}.part"
        meta_path = self.dest_dir / f"{spec.filename}.part.meta.json"
        good_tail_sha = hashlib.sha256(data[half - tail_size : half]).hexdigest()

        corrupted_half = bytearray(data[:half])
        corrupted_half[-100] ^= 0xAA
        part_path.write_bytes(bytes(corrupted_half))
        meta_path.write_text(
            json.dumps(
                {
                    "file_id": "pre",
                    "filename": spec.filename,
                    "size_bytes": spec.size_bytes,
                    "sha256": spec.sha256,
                    "bytes_written": half,
                    "tail_sha256": good_tail_sha,
                    "tail_size": tail_size,
                }
            ),
            encoding="utf-8",
        )

        srv = _FaultyRangeServer(data, fail_503_count=0)
        try:
            spec_with_url = IsoSpec(
                file_id=spec.file_id,
                filename=spec.filename,
                version=spec.version,
                size_bytes=spec.size_bytes,
                sha256=spec.sha256,
                download_url=srv.url,
            )
            client = IsoTransferClient(chunk_size=64 * 1024, tail_size=tail_size)
            res = client.download(spec_with_url, self.dest_dir)
            self.assertFalse(res.skipped)
            self.assertEqual(res.local_path.read_bytes(), data)
            self.assertFalse(part_path.exists())
            self.assertFalse(meta_path.exists())
        finally:
            srv.close()

    def test_faulty_server_503_and_midstream_drop_recovery(self) -> None:
        _, data, spec = self._make_iso(size=192 * 1024)
        srv = _FaultyRangeServer(data, fail_503_count=2, drop_after=16 * 1024)
        try:
            spec_url = IsoSpec(
                file_id=spec.file_id,
                filename=spec.filename,
                version=spec.version,
                size_bytes=spec.size_bytes,
                sha256=spec.sha256,
                download_url=srv.url,
            )
            events: list[TransferProgress] = []
            client = IsoTransferClient(chunk_size=64 * 1024, backoff_base=0.02, max_backoff=0.05)
            res = client.download(spec_url, self.dest_dir, progress_cb=events.append)
            self.assertEqual(res.local_path.read_bytes(), data)
            phases = [e.phase for e in events]
            self.assertIn("downloading", phases)
            self.assertIn("verifying", phases)
            self.assertEqual(phases[-1], "done")
            self.assertGreaterEqual(srv.requests, 5)
        finally:
            srv.close()

    def test_disk_space_precheck_fails_before_creating_part(self) -> None:
        _, _, spec = self._make_iso(size=128 * 1024)
        spec_url = IsoSpec(
            file_id=spec.file_id,
            filename=spec.filename,
            version=spec.version,
            size_bytes=spec.size_bytes,
            sha256=spec.sha256,
            download_url="http://127.0.0.1:9/unused.iso",
        )
        fake_usage = shutil._ntuple_diskusage(1024 * 1024, 1024 * 1024 - 1024, 1024)
        with mock.patch("shutil.disk_usage", return_value=fake_usage):
            client = IsoTransferClient()
            with self.assertRaises(InsufficientDiskSpaceError):
                client.download(spec_url, self.dest_dir)
        self.assertFalse((self.dest_dir / f"{spec.filename}.part").exists())

    def test_checksum_mismatch_quarantines_part(self) -> None:
        _, data, spec = self._make_iso(size=64 * 1024)
        bad_data = bytes((b ^ 0x55) for b in data)
        srv = _FaultyRangeServer(bad_data, fail_503_count=0)
        try:
            spec_url = IsoSpec(
                file_id=spec.file_id,
                filename=spec.filename,
                version=spec.version,
                size_bytes=spec.size_bytes,
                sha256=spec.sha256,
                download_url=srv.url,
            )
            client = IsoTransferClient(chunk_size=32 * 1024)
            with self.assertRaises(ChecksumMismatchError):
                client.download(spec_url, self.dest_dir)
            self.assertFalse((self.dest_dir / spec.filename).exists())
            self.assertFalse((self.dest_dir / f"{spec.filename}.part").exists())
            corrupt_files = list(self.dest_dir.glob("*.part.corrupt.*"))
            self.assertEqual(len(corrupt_files), 1)
        finally:
            srv.close()

    def test_pause_and_cancel_events(self) -> None:
        _, data, spec = self._make_iso(size=128 * 1024)
        srv = _FaultyRangeServer(data, fail_503_count=0)
        try:
            spec_url = IsoSpec(
                file_id=spec.file_id,
                filename=spec.filename,
                version=spec.version,
                size_bytes=spec.size_bytes,
                sha256=spec.sha256,
                download_url=srv.url,
            )
            pause_ev = threading.Event()
            cancel_ev = threading.Event()

            def _pause_after_first(prog: TransferProgress) -> None:
                if prog.phase == "downloading" and prog.bytes_done > 0:
                    pause_ev.set()

            client = IsoTransferClient(chunk_size=32 * 1024)
            with self.assertRaises(TransferPausedError):
                client.download(
                    spec_url, self.dest_dir, progress_cb=_pause_after_first, pause_event=pause_ev
                )
            self.assertTrue((self.dest_dir / f"{spec.filename}.part").is_file())
            self.assertTrue((self.dest_dir / f"{spec.filename}.part.meta.json").is_file())

            # Cancel immediately
            cancel_ev.set()
            with self.assertRaises(TransferCancelledError):
                client.download(spec_url, self.dest_dir, cancel_event=cancel_ev)

            # Resume cleanly
            res = client.download(spec_url, self.dest_dir)
            self.assertEqual(res.local_path.read_bytes(), data)
        finally:
            srv.close()


if __name__ == "__main__":
    unittest.main()
