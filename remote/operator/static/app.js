(() => {
  const STAGES = ['PREP', 'DOWNLOAD_ISO', 'INSTALL_MAX', 'WAIT_LICENSE', 'FIRST_RUN', 'POST_CONFIG', 'DONE'];
  let snapshot = { pairing: {}, pairing_uri: '', clients: {}, iso_catalog: [], post_config_steps: [], update_manifest: null };
  let selectedClientId = null;

  function escapeHtml(val) {
    return String(val ?? '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;');
  }

  function fmtBytes(b) {
    if (!b) return '0 B';
    const u = ['B', 'KB', 'MB', 'GB', 'TB'];
    let i = 0, v = b;
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    return v.toFixed(1) + ' ' + u[i];
  }

  function activeClient() {
    const ids = Object.keys(snapshot.clients || {});
    if (!ids.length) return null;
    if (!selectedClientId || !snapshot.clients[selectedClientId]) {
      selectedClientId = ids[0];
    }
    return snapshot.clients[selectedClientId];
  }

  async function sendCommand(cmdType, payload = {}) {
    const client = activeClient();
    if (!client) {
      alert('No client connected yet.');
      return;
    }
    await fetch(`/api/v1/ui/clients/${encodeURIComponent(client.client_id)}/command`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd_type: cmdType, payload })
    });
  }

  function render() {
    const pairing = snapshot.pairing || {};
    document.getElementById('mode-badge').textContent = pairing.mode || 'direct';
    document.getElementById('pair-sid').textContent = pairing.session_id || '-';
    document.getElementById('pairing-uri-box').textContent = snapshot.pairing_uri || '-';

    const upd = snapshot.update_manifest;
    document.getElementById('update-manifest-label').textContent = upd ? `Latest update: v${upd.version} (${upd.filename})` : 'No update bundle';

    // ISO Catalog
    const isoSel = document.getElementById('iso-select');
    const prevVal = isoSel.value;
    const isos = snapshot.iso_catalog || [];
    isoSel.innerHTML = isos.length
      ? isos.map(i => `<option value="${escapeHtml(i.file_id)}" data-ver="${escapeHtml(i.version)}">${escapeHtml(i.filename)} (v${escapeHtml(i.version)}, ${fmtBytes(i.size_bytes)})</option>`).join('')
      : '<option value="">-- No ISOs scanned --</option>';
    if (prevVal && isos.some(i => i.file_id === prevVal)) isoSel.value = prevVal;

    // Clients list
    const cList = document.getElementById('clients-list');
    const cEntries = Object.values(snapshot.clients || {});
    if (!cEntries.length) {
      cList.innerHTML = '<div class="empty-note">Waiting for client connection...</div>';
    } else {
      const cur = activeClient();
      cList.innerHTML = cEntries.map(c => {
        const st = c.state || {};
        const cls = cur && cur.client_id === c.client_id ? 'client-item active' : 'client-item';
        return `<div class="${cls}" data-cid="${escapeHtml(c.client_id)}">
          <div><strong>${escapeHtml(c.client_id)}</strong><div class="muted">${escapeHtml(st.stage || 'PREP')} / ${escapeHtml(st.status || 'idle')}</div></div>
          <span class="badge ${c.connected ? 'ok' : 'warn'}">${c.connected ? 'online' : 'stale'}</span>
        </div>`;
      }).join('');
      cList.querySelectorAll('.client-item').forEach(el => {
        el.addEventListener('click', () => { selectedClientId = el.dataset.cid; render(); });
      });
    }

    const client = activeClient();
    const sys = (client && client.system_status) || {};
    const st = (client && client.state) || {};

    document.getElementById('sys-host').textContent = sys.hostname || '-';
    document.getElementById('sys-os').textContent = sys.os_version || '-';
    document.getElementById('sys-admin').textContent = sys.is_admin === undefined ? '-' : (sys.is_admin ? 'Administrator' : 'Standard User');
    document.getElementById('sys-sid').textContent = sys.session_id ?? '-';
    document.getElementById('sys-sac').textContent = sys.smart_app_control_state || 'unknown';
    document.getElementById('sys-def').textContent = sys.defender_rtp_enabled === null || sys.defender_rtp_enabled === undefined ? 'unknown' : (sys.defender_rtp_enabled ? 'Enabled' : 'Disabled');
    document.getElementById('sys-tamper').textContent = sys.defender_tamper_protected === null || sys.defender_tamper_protected === undefined ? 'unknown' : (sys.defender_tamper_protected ? 'Protected' : 'Off');
    document.getElementById('sys-disk').textContent = sys.free_disk_bytes ? fmtBytes(sys.free_disk_bytes) : '-';
    document.getElementById('sys-max').textContent = (sys.installed_max_versions || []).join(', ') || 'None';
    document.getElementById('sys-ver').textContent = sys.agent_version ? `v${sys.agent_version} / rt ${sys.runtime_version}` : '-';

    const stage = st.stage || 'PREP';
    const status = st.status || 'idle';
    document.getElementById('stage-status-badge').textContent = `${stage} / ${status}`;

    const sIdx = STAGES.indexOf(stage);
    document.getElementById('stage-stepper').innerHTML = STAGES.map((s, idx) => {
      let cls = 'step-pill';
      if (idx < sIdx || (stage === 'DONE' && status === 'completed')) cls += ' done';
      else if (idx === sIdx) cls += status === 'failed' ? ' failed' : ' active';
      return `<div class="${cls}">${s}</div>`;
    }).join('');

    const wlBanner = document.getElementById('wait-license-banner');
    wlBanner.classList.toggle('hidden', stage !== 'WAIT_LICENSE');

    const tr = st.transfer;
    if (tr) {
      const speed = tr.speed_bps ? ` @ ${(tr.speed_bps / (1024 * 1024)).toFixed(1)} MB/s` : '';
      const eta = tr.eta_seconds !== null && tr.eta_seconds !== undefined ? ` ETA ${Math.round(tr.eta_seconds)}s` : '';
      document.getElementById('transfer-label').textContent = `${tr.phase} ${(tr.percent || 0).toFixed(1)}% (${fmtBytes(tr.bytes_done)} / ${fmtBytes(tr.bytes_total)})${speed}${eta}`;
      document.getElementById('transfer-progress').value = tr.percent || 0;
    } else {
      document.getElementById('transfer-label').textContent = st.iso_skipped_download ? 'Skipped (local ISO verified)' : '-';
      document.getElementById('transfer-progress').value = st.iso_skipped_download ? 100 : 0;
    }

    document.getElementById('install-label').textContent = `${st.install_percent || 0}% ${st.install_message || ''}`;
    document.getElementById('install-progress').value = st.install_percent || 0;

    // Post-config steps
    const steps = (st.post_config_steps && st.post_config_steps.length) ? st.post_config_steps : (snapshot.post_config_steps || []);
    document.getElementById('post-config-list').innerHTML = steps.map(p => {
      const badgeCls = p.status === 'done' ? 'ok' : (p.status === 'failed' ? 'err' : (p.status === 'running' ? 'warn' : ''));
      return `<div class="pc-step"><span>${escapeHtml(p.name)} <span class="muted">(${escapeHtml(p.category)})</span></span><span class="badge ${badgeCls}">${escapeHtml(p.status)}</span></div>`;
    }).join('');

    // DONE summary
    const doneCard = document.getElementById('done-summary-card');
    if (stage === 'DONE' && st.summary && Object.keys(st.summary).length) {
      doneCard.classList.remove('hidden');
      document.getElementById('done-summary-pre').textContent = JSON.stringify(st.summary, null, 2);
    } else {
      doneCard.classList.add('hidden');
    }

    // Logs
    const lvlFilter = document.getElementById('log-level-filter').value;
    const ranks = { DEBUG: 0, INFO: 1, WARN: 2, ERROR: 3 };
    const minRank = lvlFilter === 'ALL' ? 0 : (ranks[lvlFilter] ?? 0);
    const logs = (client && client.logs) || [];
    const logsBox = document.getElementById('logs-box');
    logsBox.innerHTML = logs
      .filter(l => (ranks[l.level] ?? 1) >= minRank)
      .map(l => `<div><span class="muted">[${escapeHtml(l.ts)}]</span> <strong>${escapeHtml(l.level)}</strong> [${escapeHtml(l.stage)}] ${escapeHtml(l.msg)}</div>`)
      .join('');
    logsBox.scrollTop = logsBox.scrollHeight;
  }

  // Wire buttons
  document.querySelectorAll('[data-cmd]').forEach(btn => {
    btn.addEventListener('click', () => {
      const payload = btn.dataset.payload ? JSON.parse(btn.dataset.payload) : {};
      sendCommand(btn.dataset.cmd, payload);
    });
  });

  document.getElementById('btn-start-install').addEventListener('click', () => {
    const sel = document.getElementById('iso-select');
    const fid = sel.value;
    const ver = document.getElementById('version-input').value.trim() || '2025';
    const iso = (snapshot.iso_catalog || []).find(i => i.file_id === fid);
    const payload = iso ? { version: iso.version || ver, iso_spec: iso } : { version: ver };
    sendCommand('start_install', payload);
  });

  document.getElementById('iso-select').addEventListener('change', (e) => {
    const opt = e.target.selectedOptions[0];
    if (opt && opt.dataset.ver) {
      document.getElementById('version-input').value = opt.dataset.ver;
    }
  });

  document.getElementById('btn-rescan').addEventListener('click', async () => {
    await fetch('/api/v1/ui/rescan', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: '{}'
    });
  });

  document.getElementById('btn-copy-uri').addEventListener('click', () => {
    if (navigator.clipboard && snapshot.pairing_uri) {
      navigator.clipboard.writeText(snapshot.pairing_uri);
    }
  });

  document.getElementById('log-level-filter').addEventListener('change', render);

  // Initial load + SSE stream
  fetch('/api/v1/ui/snapshot').then(r => r.json()).then(d => { snapshot = d; render(); }).catch(console.error);

  const es = new EventSource('/api/v1/ui/events');
  const sseBadge = document.getElementById('sse-badge');
  es.onopen = () => { sseBadge.textContent = 'SSE: Live'; sseBadge.className = 'badge ok'; };
  es.onerror = () => { sseBadge.textContent = 'SSE: Reconnecting'; sseBadge.className = 'badge warn'; };

  es.addEventListener('snapshot', (ev) => { snapshot = JSON.parse(ev.data); render(); });
  es.addEventListener('client_registered', (ev) => {
    const c = JSON.parse(ev.data);
    snapshot.clients[c.client_id] = c;
    render();
  });
  es.addEventListener('heartbeat', (ev) => {
    const d = JSON.parse(ev.data);
    const c = snapshot.clients[d.client_id] || { client_id: d.client_id, state: {}, logs: [] };
    c.system_status = d.system_status;
    c.last_seen = d.last_seen;
    c.connected = true;
    snapshot.clients[d.client_id] = c;
    render();
  });
  es.addEventListener('state', (ev) => {
    const d = JSON.parse(ev.data);
    const c = snapshot.clients[d.client_id] || { client_id: d.client_id, system_status: {}, logs: [] };
    c.state = d.state;
    c.connected = true;
    snapshot.clients[d.client_id] = c;
    render();
  });
  es.addEventListener('logs', (ev) => {
    const d = JSON.parse(ev.data);
    const c = snapshot.clients[d.client_id] || { client_id: d.client_id, system_status: {}, state: {}, logs: [] };
    c.logs = (c.logs || []).concat(d.entries || []).slice(-500);
    snapshot.clients[d.client_id] = c;
    render();
  });
  es.addEventListener('ack', (ev) => {
    const d = JSON.parse(ev.data);
    if (snapshot.clients[d.client_id]) {
      snapshot.clients[d.client_id].last_ack = d.ack;
    }
  });
})();
