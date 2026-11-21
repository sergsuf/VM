"""Localhost-only pure-Python HTTP Status UI for ClientAgent (TeamViewer-safe)."""

from __future__ import annotations

import html as html_lib
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse
import webbrowser

from remote.auth import PairingConfig
from remote.protocol import Stage

_MAX_JSON_BODY_BYTES = 16 * 1024 * 1024

if TYPE_CHECKING:
    from remote.client.agent import ClientAgent


_STATUS_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>3ds Max Client Agent — Status</title>
<style>
  :root {
    --bg: #0f172a; --card: #1e293b; --border: #334155;
    --text: #f1f5f9; --muted: #94a3b8; --accent: #38bdf8;
    --ok: #22c55e; --warn: #f59e0b; --err: #ef4444;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font-family: "Segoe UI", system-ui, sans-serif; background: var(--bg); color: var(--text); padding: 16px; }
  h1, h2, h3 { margin: 0 0 10px 0; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 14px; border-bottom: 1px solid var(--border); padding-bottom: 10px; }
  .pill { display: inline-block; padding: 4px 10px; border-radius: 999px; font-size: 12px; font-weight: 600; background: #334155; }
  .pill.ok { background: rgba(34,197,94,0.2); color: #86efac; border: 1px solid #22c55e; }
  .pill.warn { background: rgba(245,158,11,0.2); color: #fcd34d; border: 1px solid #f59e0b; }
  .pill.err { background: rgba(239,68,68,0.2); color: #fca5a5; border: 1px solid #ef4444; }
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(320px, 1fr)); gap: 14px; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 10px; padding: 14px; }
  .row { display: flex; justify-content: space-between; gap: 8px; margin: 6px 0; font-size: 14px; }
  .muted { color: var(--muted); }
  .btn-row { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 10px; }
  button { background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 7px 12px; cursor: pointer; font-size: 13px; font-weight: 600; }
  button:hover { background: #1d4ed8; }
  button.secondary { background: #334155; }
  button.secondary:hover { background: #475569; }
  button.warn { background: #d97706; }
  button.warn:hover { background: #b45309; }
  .stepper { display: flex; flex-wrap: wrap; gap: 6px; margin: 10px 0; }
  .step { padding: 6px 10px; border-radius: 6px; font-size: 12px; background: #0f172a; border: 1px solid var(--border); color: var(--muted); }
  .step.active { border-color: var(--accent); color: var(--accent); font-weight: 700; }
  .step.done { border-color: var(--ok); color: #86efac; }
  .banner { background: rgba(245,158,11,0.16); border: 1px solid var(--warn); border-radius: 8px; padding: 12px; margin-bottom: 14px; display: none; }
  .banner.visible { display: block; }
  progress { width: 100%; height: 14px; margin: 6px 0; }
  textarea, input { width: 100%; background: #0f172a; color: var(--text); border: 1px solid var(--border); border-radius: 6px; padding: 8px; font-family: monospace; font-size: 12px; }
  .logs { background: #020617; border: 1px solid var(--border); border-radius: 8px; padding: 10px; height: 220px; overflow-y: auto; font-family: monospace; font-size: 12px; line-height: 1.45; }
</style>
</head>
<body data-stage="__INITIAL_STAGE__">
  <div class="header">
    <div>
      <h2>3ds Max Deployment Client Agent</h2>
      <div class="muted" id="client-meta">Client: __CLIENT_ID__</div>
    </div>
    <div>
      <span class="pill" id="conn-pill">Connecting...</span>
      <span class="pill" id="stage-pill">__INITIAL_STAGE__</span>
    </div>
  </div>

  <div class="banner __LICENSE_BANNER_CLASS__" id="license-banner">
    <strong>WAIT_LICENSE — Manual Licensing Required</strong>
    <p style="margin:6px 0 8px 0;">Please complete Autodesk 3ds Max licensing/activation on this machine, then click Confirm License (or let the Operator confirm).</p>
    <button class="warn" onclick="doAction('confirm_license', {})">Confirm License &amp; Continue</button>
  </div>

  <div class="card" style="margin-bottom:14px;">
    <h3>7-Stage Deployment Progress</h3>
    <div class="stepper" id="stepper"></div>
    <div class="row"><span class="muted">Target Version</span><strong id="target-ver">-</strong></div>
    <div class="row"><span class="muted">Transfer</span><span id="transfer-text">-</span></div>
    <progress id="transfer-bar" max="100" value="0"></progress>
    <div class="row"><span class="muted">Installer</span><span id="install-text">-</span></div>
    <progress id="install-bar" max="100" value="0"></progress>
  </div>

  <div class="grid">
    <div class="card">
      <h3>System &amp; Security Controls</h3>
      <div class="row"><span class="muted">Admin / UAC</span><strong id="admin-status">-</strong></div>
      <div class="row"><span class="muted">Smart App Control</span><strong id="sac-status">-</strong></div>
      <div class="row"><span class="muted">Windows Defender RTP</span><strong id="def-status">-</strong></div>
      <div class="row"><span class="muted">Tamper Protection</span><strong id="tamper-status">-</strong></div>
      <div class="row"><span class="muted">Free Disk</span><strong id="disk-status">-</strong></div>
      <div class="btn-row">
        <button onclick="doAction('request_elevation', {})">Request Admin (UAC)</button>
        <button class="warn" onclick="doAction('smart_app_control', {mode:'guided'})">Disable Smart App Control</button>
        <button class="warn" onclick="doAction('defender_control', {action:'disable'})">Disable Defender RTP</button>
        <button class="secondary" onclick="doAction('defender_control', {action:'add_exclusions'})">Add Defender Exclusions</button>
        <button class="secondary" onclick="doAction('defender_control', {action:'open_settings'})">Open Defender Settings</button>
        <button class="secondary" onclick="doAction('refresh_status', {})">Refresh Status</button>
      </div>
    </div>

    <div class="card">
      <h3>Operator Pairing (connect.json / maxpair://)</h3>
      <div class="row"><span class="muted">Mode</span><strong id="pair-mode">-</strong></div>
      <div class="row"><span class="muted">Server URL</span><span id="pair-url">-</span></div>
      <div class="row"><span class="muted">Session ID</span><strong id="pair-sid">-</strong></div>
      <textarea id="pair-input" rows="3" placeholder="Paste connect.json or maxpair://... URI"></textarea>
      <div class="btn-row">
        <button onclick="submitPair()">Pair / Reconnect</button>
      </div>
    </div>
  </div>

  <div class="card" style="margin-top:14px;">
    <h3>Live Agent Logs</h3>
    <div class="logs" id="logs-box"></div>
  </div>

<script>
const STAGES = ['PREP','DOWNLOAD_ISO','INSTALL_MAX','WAIT_LICENSE','FIRST_RUN','POST_CONFIG','DONE'];

function escapeHtml(val) {
  return String(val ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

async function doAction(action, payload) {
  try {
    await fetch('/api/action', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({action, payload})
    });
    await refresh();
  } catch (e) { console.error(e); }
}

async function submitPair() {
  const raw = document.getElementById('pair-input').value.trim();
  if (!raw) return;
  const payload = raw.startsWith('maxpair://') ? {pairing_uri: raw} : {connect_json: raw};
  await doAction('pair', payload);
}

function fmtBytes(b) {
  if (!b) return '0 B';
  const u = ['B','KB','MB','GB','TB'];
  let i = 0, v = b;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
  return v.toFixed(1) + ' ' + u[i];
}

async function refresh() {
  try {
    const r = await fetch('/api/status');
    const d = await r.json();
    const st = d.state || {};
    const sys = d.system || {};
    const pair = d.pairing || {};

    document.getElementById('client-meta').textContent = `Client: ${d.client_id} | Host: ${sys.hostname || '-'}`;
    const conn = document.getElementById('conn-pill');
    conn.textContent = d.connected ? 'Connected' : 'Disconnected';
    conn.className = 'pill ' + (d.connected ? 'ok' : 'warn');

    const stage = st.stage || 'PREP';
    const status = st.status || 'idle';
    document.getElementById('stage-pill').textContent = `${stage} (${status})`;
    document.body.dataset.stage = stage;

    const banner = document.getElementById('license-banner');
    banner.className = 'banner' + (stage === 'WAIT_LICENSE' ? ' visible' : '');

    const idx = STAGES.indexOf(stage);
    document.getElementById('stepper').innerHTML = STAGES.map((s, i) => {
      let cls = 'step';
      if (i < idx || (stage === 'DONE' && status === 'completed')) cls += ' done';
      else if (i === idx) cls += ' active';
      return `<div class="${cls}">${s}</div>`;
    }).join('');

    document.getElementById('target-ver').textContent = st.target_version || '-';
    const tr = st.transfer;
    if (tr) {
      document.getElementById('transfer-text').textContent = `${tr.phase} — ${tr.percent.toFixed(1)}% (${fmtBytes(tr.bytes_done)} / ${fmtBytes(tr.bytes_total)})`;
      document.getElementById('transfer-bar').value = tr.percent || 0;
    } else {
      document.getElementById('transfer-text').textContent = st.iso_skipped_download ? 'Skipped (local ISO verified)' : '-';
      document.getElementById('transfer-bar').value = st.iso_skipped_download ? 100 : 0;
    }
    document.getElementById('install-text').textContent = `${st.install_percent || 0}% ${st.install_message || ''}`;
    document.getElementById('install-bar').value = st.install_percent || 0;

    document.getElementById('admin-status').textContent = sys.is_admin ? 'Administrator' : 'Standard User';
    document.getElementById('sac-status').textContent = sys.smart_app_control_state || 'unknown';
    document.getElementById('def-status').textContent = sys.defender_rtp_enabled === null ? 'unknown' : (sys.defender_rtp_enabled ? 'Enabled' : 'Disabled');
    document.getElementById('tamper-status').textContent = sys.defender_tamper_protected === null ? 'unknown' : (sys.defender_tamper_protected ? 'Protected' : 'Off');
    document.getElementById('disk-status').textContent = fmtBytes(sys.free_disk_bytes || 0);

    document.getElementById('pair-mode').textContent = pair.mode || 'unpaired';
    document.getElementById('pair-url').textContent = pair.server_url || '-';
    document.getElementById('pair-sid').textContent = pair.session_id || '-';

    const logsBox = document.getElementById('logs-box');
    const logs = d.logs || [];
    logsBox.innerHTML = logs.map(l => `<div><span class="muted">[${escapeHtml(l.ts)}]</span> <strong>${escapeHtml(l.level)}</strong> [${escapeHtml(l.stage)}] ${escapeHtml(l.msg)}</div>`).join('');
    logsBox.scrollTop = logsBox.scrollHeight;
  } catch (e) { console.error(e); }
}

refresh();
setInterval(refresh, 1500);
</script>
</body>
</html>
"""


class ClientStatusServer:
    """Localhost-only HTTP server for ClientAgent status and TeamViewer controls."""

    def __init__(
        self,
        agent: ClientAgent,
        host: str = "127.0.0.1",
        port: int = 8770,
    ) -> None:
        if host != "127.0.0.1":
            raise ValueError("ClientStatusServer must bind strictly to 127.0.0.1")
        self.agent = agent
        self.host = "127.0.0.1"
        self._requested_port = int(port)
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int:
        if self._httpd is not None:
            return int(self._httpd.server_address[1])
        return self._requested_port

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def _build_status_payload(self) -> dict[str, Any]:
        pairing = self.agent.pairing
        st = self.agent.state
        sys_st = self.agent.system_status
        return {
            "client_id": self.agent.client_id,
            "connected": self.agent.connected,
            "stage": st.stage.value,
            "status": st.status.value,
            "state": st.to_dict(),
            "system": sys_st.to_dict(),
            "pairing": {
                "paired": pairing is not None,
                "mode": pairing.mode if pairing else None,
                "server_url": pairing.server_url if pairing else None,
                "session_id": pairing.session_id if pairing else None,
            },
            "logs": self.agent.recent_logs,
        }

    def start(self) -> None:
        if self._httpd is not None:
            return
        parent = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return

            def _send_json(self, code: int, data: dict[str, Any]) -> None:
                body = json.dumps(data, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path == "/":
                    st = parent.agent.state
                    banner_cls = "visible" if st.stage == Stage.WAIT_LICENSE else ""
                    html = (
                        _STATUS_HTML.replace("__INITIAL_STAGE__", html_lib.escape(st.stage.value, quote=True))
                        .replace("__CLIENT_ID__", html_lib.escape(parent.agent.client_id, quote=True))
                        .replace("__LICENSE_BANNER_CLASS__", banner_cls)
                    )
                    if st.stage == Stage.WAIT_LICENSE:
                        html = html.replace(
                            "</body>",
                            "<!-- state:wait_license --></body>",
                        )
                    body = html.encode("utf-8")
                    self.send_response(HTTPStatus.OK)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path in ("/api/status", "/status"):
                    self._send_json(HTTPStatus.OK, parent._build_status_payload())
                    return

                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})

            def do_POST(self) -> None:  # noqa: N802
                path = self.path.split("?", 1)[0]
                if path != "/api/action":
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
                    return

                origin = self.headers.get("Origin")
                if origin:
                    origin_netloc = urlparse(origin).netloc.lower()
                    host_header = (self.headers.get("Host") or "").lower()
                    if not origin_netloc or (host_header and origin_netloc != host_header):
                        self.close_connection = True
                        self._send_json(
                            HTTPStatus.FORBIDDEN,
                            {"error": "Cross-origin request forbidden"},
                        )
                        return

                raw_len = self.headers.get("Content-Length") or "0"
                try:
                    length = int(raw_len)
                except ValueError:
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "Invalid Content-Length"}
                    )
                    return
                if length < 0:
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.BAD_REQUEST, {"error": "Negative Content-Length"}
                    )
                    return
                if length > _MAX_JSON_BODY_BYTES:
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.REQUEST_ENTITY_TOO_LARGE,
                        {"error": "Payload too large"},
                    )
                    return

                ctype_hdr = self.headers.get("Content-Type")
                ctype_main = (ctype_hdr or "").split(";", 1)[0].strip().lower()
                if (ctype_hdr is not None or length > 0) and ctype_main != "application/json":
                    self.close_connection = True
                    self._send_json(
                        HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                        {"error": "Content-Type must be application/json"},
                    )
                    return

                raw = self.rfile.read(length) if length > 0 else b"{}"
                try:
                    data = json.loads(raw.decode("utf-8"))
                except Exception:
                    self._send_json(HTTPStatus.BAD_REQUEST, {"error": "Invalid JSON"})
                    return
                if not isinstance(data, dict):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "JSON body must be an object"},
                    )
                    return
                action = str(data.get("action") or "")
                raw_payload = data.get("payload")
                if raw_payload is None:
                    payload = {}
                elif not isinstance(raw_payload, dict):
                    self._send_json(
                        HTTPStatus.BAD_REQUEST,
                        {"error": "JSON payload must be an object"},
                    )
                    return
                else:
                    payload = raw_payload
                if action == "pair":
                    try:
                        if payload.get("pairing_uri"):
                            pairing = PairingConfig.from_pairing_uri(
                                str(payload["pairing_uri"])
                            )
                        elif payload.get("connect_json"):
                            pairing = PairingConfig.from_connect_json(
                                str(payload["connect_json"])
                            )
                        else:
                            pairing = PairingConfig.from_dict(payload)
                        parent.agent.set_pairing(pairing)
                        self._send_json(HTTPStatus.OK, {"ok": True})
                    except Exception as exc:
                        self._send_json(
                            HTTPStatus.BAD_REQUEST, {"ok": False, "error": str(exc)}
                        )
                    return

                result = parent.agent.execute_local_action(action, payload)
                self._send_json(HTTPStatus.OK, result)

        class _Server(ThreadingHTTPServer):
            daemon_threads = True
            allow_reuse_address = True

        self._httpd = _Server(("127.0.0.1", self._requested_port), _Handler)
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="client-status-ui", daemon=True
        )
        self._thread.start()

    def shutdown(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None

    def __enter__(self) -> ClientStatusServer:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.shutdown()


def launch_status_ui_window(url: str, headless: bool = False) -> bool:
    """Open Client Status UI in Edge --app mode, default browser, or console."""
    print(f"Client Status UI: {url}", flush=True)
    if headless:
        return False

    if os.name == "nt":
        edge_candidates = [
            shutil.which("msedge"),
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        ]
        for cand in edge_candidates:
            if cand and Path(cand).is_file():
                try:
                    subprocess.Popen([str(cand), f"--app={url}"])
                    return True
                except Exception:
                    pass

    try:
        return bool(webbrowser.open(url))
    except Exception:
        return False
