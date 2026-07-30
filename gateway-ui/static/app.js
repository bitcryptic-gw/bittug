'use strict';

// ── State ─────────────────────────────────────────────────────────────────────

const state = {
  token:           null,
  logLines:        [],
  logInterval:     null,
  dashInterval:    null,
  netInterval:    null,
  verInterval:     null,
  tokenRevealed:   false,
  wingbitsAbort:   null,
  otaChanges:      null,
  otaAbort:        null,
  otaCountdown:    null,
  powerAction:     null,
  powerTimer:      null,
  rebootArmed:     false,
  rebootTimer:     null,
  cachedVersion:   null,
};

const depinState = {
  interval: null,
  uninstalling: null,
  autoUpdate: {},
  updating: null,
};

// ── API ───────────────────────────────────────────────────────────────────────

async function api(path, method = 'GET', body = null) {
  const opts = {
    method,
    headers: { Authorization: `Bearer ${state.token}` },
  };
  if (body !== null) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  let r;
  try {
    r = await fetch(path, opts);
  } catch {
    throw new Error('Network error — service may be restarting');
  }
  if (r.status === 401) {
    localStorage.removeItem('gw_token');
    showModal(true);
    throw new Error('unauthorized');
  }
  if (!r.ok) {
    let detail = 'Request failed';
    try { detail = (await r.json()).detail || detail; } catch {}
    throw new Error(detail);
  }
  return r.json();
}

// ── Auth / Modal ──────────────────────────────────────────────────────────────

function showModal(isError = false) {
  document.getElementById('app').classList.add('hidden');
  document.getElementById('modal-overlay').classList.remove('hidden');
  document.getElementById('modal-error').classList.toggle('hidden', !isError);
  document.getElementById('modal-token').value = '';
  document.getElementById('modal-token').focus();
}

function hideModal() {
  document.getElementById('modal-overlay').classList.add('hidden');
  document.getElementById('app').classList.remove('hidden');
}

async function handleTokenSubmit() {
  const input = document.getElementById('modal-token');
  const token = input.value.trim();
  if (!token) return;

  const prev = state.token;
  state.token = token;

  try {
    await api('/api/identity');
    localStorage.setItem('gw_token', token);
    hideModal();
    initApp();
  } catch (e) {
    state.token = prev;
    if (e.message !== 'unauthorized') {
      document.getElementById('modal-error').textContent = e.message;
    }
    document.getElementById('modal-error').classList.remove('hidden');
  }
}

// ── Tab Navigation ────────────────────────────────────────────────────────────

function switchTab(name) {
  document.querySelectorAll('.tab-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.tab === name);
  });
  document.querySelectorAll('.tab-panel').forEach(p => {
    p.classList.toggle('hidden', p.id !== `tab-${name}`);
    p.classList.toggle('active', p.id === `tab-${name}`);
  });

  clearInterval(state.logInterval);
  clearInterval(state.dashInterval);
  clearInterval(state.netInterval);
  clearInterval(depinState.interval);
  state.logInterval = null;
  state.dashInterval = null;
  state.netInterval = null;
  depinState.interval = null;

  if (state.wingbitsAbort) {
    state.wingbitsAbort.abort();
    state.wingbitsAbort = null;
  }

  sessionStorage.setItem('activeTab', name);

  if (name === 'dashboard')     startDashboardRefresh();
  if (name === 'applications')  loadApplications();
  if (name === 'depin')         startDepinRefresh();
  if (name === 'network')       startNetworkRefresh();
  if (name === 'logs')          startLogAutoRefresh();
  if (name === 'settings')      loadSettings();
}

// ── Dashboard ─────────────────────────────────────────────────────────────────

function startDashboardRefresh() {
  loadDashboard();
  state.dashInterval = setInterval(loadDashboard, 30_000);
}

async function loadDashboard() {
  const [status, sysinfo, groups] = await Promise.allSettled([
    api('/api/status'),
    api('/api/sysinfo'),
    api('/api/status/groups'),
  ]);
  if (groups.status  === 'fulfilled') renderDashServices(groups.value);
  if (sysinfo.status === 'fulfilled') renderSysinfo(sysinfo.value, true);
}

function renderDashServices(d) {
  const expanded = new Set();
  document.querySelectorAll('#dash-services-body .service-group').forEach(el => {
    if (!el.classList.contains('collapsed')) expanded.add(el.dataset.group);
  });
  const groupOrder = ['helium', 'wingbits', 'tailscale', 'web-ui'];
  const labels = { helium: 'Helium', wingbits: 'Wingbits', tailscale: 'Tailscale', 'web-ui': 'Web UI' };
  const stateClass = { active: 'status-active', fault: 'status-fault', optional: 'status-optional' };
  const stateLabel = { active: 'active', fault: 'fault', optional: 'not configured' };
  const el = document.getElementById('dash-services-body');
  el.innerHTML = groupOrder.map(key => {
    const g = d[key] || { group_state: 'optional', units: [] };
    const cls = stateClass[g.group_state] || 'status-optional';
    const label = labels[key] || key;
    const detail = (g.units || []).map(u => {
      const uc = stateClass[u.state] || 'status-optional';
      return `<span class="service-dot-detail ${uc}">● ${u.unit.replace('.service', '')}</span>`;
    }).join(' ');
    const collapsedClass = expanded.has(key) ? '' : 'collapsed';
    return `<div class="service-group ${collapsedClass}" data-group="${key}">
      <span class="service-dot ${cls}" title="${label}: ${stateLabel[g.group_state] || 'not configured'}">● ${label}<span class="chevron">▸</span></span>
      <span class="service-dot-sub">${detail}</span>
    </div>`;
  }).join('') + `<div class="services-legend">
    <span class="service-dot-detail status-active">● Running</span>
    <span class="service-dot-detail status-fault">● Fault</span>
    <span class="service-dot-detail status-optional">● Not configured</span>
  </div>`;
}

// ── Applications: Helium + Wingbits ──────────────────────────────────────────

async function loadApplications() {
  const [identity, status, beacon, bands, wingbits] = await Promise.allSettled([
    api('/api/identity'),
    api('/api/status'),
    api('/api/beacon'),
    api('/api/bands'),
    api('/api/wingbits'),
  ]);

  if (identity.status === 'fulfilled') renderAppIdentity(identity.value);
  if (status.status   === 'fulfilled') renderHeliumServices(status.value);
  if (beacon.status   === 'fulfilled') renderBeacon(beacon.value);
  if (bands.status    === 'fulfilled') renderBands(bands.value);
  if (wingbits.status === 'fulfilled') renderWingbits(wingbits.value);
}

function renderAppIdentity(d) {
  const el = document.getElementById('identity-body');
  el.innerHTML = kv([
    ['Name',    d.name    || '<span class="dim">unavailable</span>'],
    ['Key', d.key ? `<code class="addr">${abbrev(d.key)}</code>` : '<span class="dim">unavailable</span>'],
    ['EUI',     d.eui     ? `<code>${d.eui}</code>` : '<span class="dim">—</span>'],
    ['Region',  d.region  ? `<span class="badge badge-info">${d.region}</span>` : '<span class="dim">—</span>'],
  ]);
}

function renderHeliumServices(d) {
  const el = document.getElementById('helium-services-body');
  el.innerHTML = [
    d.pktfwd || { unit: 'pktfwd.service', state: 'not-installed', since: '' },
    d['gateway-rs'] || { unit: 'gateway-rs.service', state: 'not-installed', since: '' },
  ].map(s => serviceRow(s)).join('');
}

function serviceRow(s) {
  const cls   = s.state === 'active' ? 'green' : s.state === 'failed' ? 'red' : 'dim';
  const label = s.state === 'active' ? '● active' : `● ${s.state}`;
  const short = s.unit.replace('.service', '');
  const since = s.since ? `<div class="since">since ${fmtTimestamp(s.since)}</div>` : '';
  return `
    <div class="service-row">
      <div class="service-info">
        <span class="service-name">${short}</span>
        <span class="badge badge-${cls}">${label}</span>
        ${since}
      </div>
      <button class="btn btn-sm btn-restart" data-service="${short}">Restart</button>
    </div>`;
}

function renderBeacon(d) {
  const el = document.getElementById('beacon-body');
  const lb = d.last_beacon
    ? fmtTimestamp(d.last_beacon.timestamp)
    : '<span class="dim">none recorded</span>';
  el.innerHTML = '<p class="hint" style="margin-bottom:.75rem;line-height:1.5">PoC beaconing retired network-wide (HIP-149); this card reflects legacy IoT data no longer generated.</p>' + kv([
    ['Last beacon',      lb],
    ['Next beacon',      '<span class="dim">N/A</span>'],
    ['Witnesses (24 h)', `<strong>${d.witness_count_24h}</strong>`],
  ]);
}

function renderBands(d) {
  const sel = document.getElementById('band-select');
  sel.innerHTML = d.regions.map(r =>
    `<option value="${r}"${r === d.current ? ' selected' : ''}>${r}</option>`
  ).join('');
  document.getElementById('current-band').textContent = d.current || '—';
}

async function applyBand() {
  const region = document.getElementById('band-select').value;
  const btn    = document.getElementById('btn-apply-band');
  btn.disabled = true;
  btn.textContent = 'Applying…';
  try {
    await api('/api/band', 'POST', { region });
    // Re-fetch from backend so the UI reflects what was actually persisted
    const [identity, bands] = await Promise.all([
      api('/api/identity'),
      api('/api/bands'),
    ]);
    renderAppIdentity(identity);
    renderBands(bands);
    showResult('band-result', `Band set to ${region}`, false);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('band-result', e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Apply Band';
  }
}

// ── Timezone ────────────────────────────────────────────────────────────────

async function loadTimezones() {
  try {
    const d = await api('/api/timezones');
    renderTimezones(d);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('timezone-result', e.message, true);
  }
}

function renderTimezones(d) {
  const list = document.getElementById('timezone-list');
  list.innerHTML = d.timezones.map(tz =>
    `<option value="${tz}">`
  ).join('');
  const input = document.getElementById('timezone-input');
  input.value = d.current || 'Etc/UTC';
  document.getElementById('current-timezone').textContent = d.current || 'Etc/UTC';
}

async function applyTimezone() {
  const tz = document.getElementById('timezone-input').value.trim();
  if (!tz) return;
  const btn = document.getElementById('btn-apply-timezone');
  btn.disabled = true;
  btn.textContent = 'Applying…';
  try {
    await api('/api/timezone', 'POST', { timezone: tz });
    const d = await api('/api/timezones');
    renderTimezones(d);
    showResult('timezone-result', `Timezone set to ${tz}`, false);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('timezone-result', e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Apply Timezone';
  }
}

// ── Hostname ────────────────────────────────────────────────────────────────

async function loadHostname() {
  try {
    const d = await api('/api/hostname');
    renderHostname(d);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('hostname-result', e.message, true);
  }
}

function renderHostname(d) {
  document.getElementById('current-hostname').textContent = d.hostname || '—';
  document.getElementById('current-ts-name').textContent = d.tailscale_name || '—';
}

async function applyHostname() {
  const name = document.getElementById('hostname-input').value.trim();
  if (!name) { showResult('hostname-result', 'Enter a hostname', true); return; }
  const btn = document.getElementById('btn-apply-hostname');
  btn.disabled = true;
  btn.textContent = 'Applying…';
  try {
    const r = await api('/api/hostname', 'POST', { hostname: name });
    if (r.ok) {
      showResult('hostname-result', `Hostname changed to ${name}`, false);
    } else if (r.partial) {
      showResult('hostname-result', `OS hostname changed to ${name}, but Tailscale rename failed — ${r.detail || 'retry from Settings'}`, true);
    } else {
      showResult('hostname-result', r.detail || 'Hostname change failed', true);
    }
    const d = await api('/api/hostname');
    renderHostname(d);
    // Refresh system info and Tailscale display to pick up new hostname everywhere
    setTimeout(loadNetwork, 1000);
    setTimeout(loadDashboard, 1000);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('hostname-result', e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Apply Hostname';
  }
}

// ── Applications: Wingbits ───────────────────────────────────────────────────

async function loadWingbits() {
  try {
    const d = await api('/api/wingbits');
    renderWingbits(d);
  } catch (e) {
    if (e.message !== 'unauthorized') {
      document.getElementById('wingbits-services-body').innerHTML =
        `<span class="dim">Error: ${e.message}</span>`;
    }
  }
}

function renderWingbits(d) {
  const el = document.getElementById('wingbits-services-body');
  el.innerHTML = [d.readsb, d.wingbits].map(s => wingbitsServiceRow(s)).join('');

  const allActive = d.readsb.state === 'active' && d.wingbits.state === 'active';
  const noneInstalled = d.readsb.state === 'not-installed' && d.wingbits.state === 'not-installed';
  const overall = document.getElementById('wingbits-overall');
  if (allActive) {
    overall.textContent = '● Active';
    overall.className = 'badge badge-green';
  } else if (noneInstalled) {
    overall.textContent = '● Not configured';
    overall.className = 'badge badge-dim';
  } else {
    overall.textContent = '● Degraded';
    overall.className = 'badge badge-yellow';
  }

  const diag = document.getElementById('wingbits-diagnostic');
  if (d.readsb.diagnostic) {
    diag.innerHTML = `<span class="warn-text">⚠ ${d.readsb.diagnostic}</span>`;
    diag.classList.remove('hidden');
  } else {
    diag.classList.add('hidden');
  }
}

function wingbitsServiceRow(s) {
  let cls, label;
  if (s.state === 'active') {
    cls = 'green'; label = '● Running';
  } else if (s.state === 'not-installed') {
    cls = 'dim'; label = '● Not installed';
  } else {
    cls = 'yellow'; label = '● Stopped';
  }
  const short = s.unit.replace('.service', '');
  const since = s.since ? `<div class="since">since ${fmtTimestamp(s.since)}</div>` : '';
  return `
    <div class="service-row">
      <div class="service-info">
        <span class="service-name">${short}</span>
        <span class="badge badge-${cls}">${label}</span>
        ${since}
      </div>
    </div>`;
}

function _wingbitsValidateCmd(cmd) {
  return cmd.includes('https://gitlab.com/wingbits/config/-/raw/master/download.sh')
    && /loc="[^"]*"/.test(cmd) && /id="[^"]*"/.test(cmd);
}

function _wingbitsUpdateBtn() {
  const cmd = document.getElementById('wingbits-cmd').value.trim();
  const btn = document.getElementById('btn-wingbits-run');
  const msg = document.getElementById('wingbits-cmd-msg');
  if (!cmd) {
    btn.disabled = true;
    msg.classList.add('hidden');
    document.getElementById('wingbits-cmd').classList.remove('invalid');
    return;
  }
  if (_wingbitsValidateCmd(cmd)) {
    btn.disabled = false;
    msg.classList.add('hidden');
    document.getElementById('wingbits-cmd').classList.remove('invalid');
  } else {
    btn.disabled = true;
    msg.textContent = 'Paste the full install command from your Wingbits dashboard (starts with curl -sL https://gitlab.com/wingbits/... and contains loc="..." id="...")';
    msg.className = 'wingbits-input-msg';
    document.getElementById('wingbits-cmd').classList.add('invalid');
  }
}

async function runWingbitsSetup() {
  const cmd = document.getElementById('wingbits-cmd').value.trim();
  if (!_wingbitsValidateCmd(cmd)) return;

  const btn = document.getElementById('btn-wingbits-run');
  const output = document.getElementById('wingbits-output');
  const banner = document.getElementById('wingbits-banner');
  const outputActions = document.getElementById('wingbits-output-actions');

  btn.disabled = true;
  btn.textContent = 'Running…';
  output.classList.remove('hidden');
  output.textContent = '';
  banner.classList.add('hidden');
  outputActions.classList.add('hidden');

  state.wingbitsAbort = new AbortController();

  try {
    const r = await fetch('/api/wingbits/setup', {
      method: 'POST',
      headers: { Authorization: `Bearer ${state.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ cmd }),
      signal: state.wingbitsAbort.signal,
    });

    if (r.status === 409) { output.textContent = 'Setup already in progress.'; return; }
    if (r.status === 503) { const err = await r.json(); output.textContent = `Error: ${err.detail}`; return; }
    if (!r.ok) {
      let detail = 'Request failed';
      try { detail = (await r.json()).detail || detail; } catch {}
      output.textContent = `Error: ${detail}`; return;
    }

    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let exitCode = null;

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const part of parts) {
        for (const line of part.split('\n')) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            try {
              const obj = JSON.parse(data);
              if (obj.exit_code !== undefined) { exitCode = obj.exit_code; break; }
            } catch {}
            output.textContent += data + '\n';
            output.scrollTop = output.scrollHeight;
          }
        }
        if (exitCode !== null) break;
      }
    }

    if (exitCode === 0) {
      banner.textContent = '✓ Setup completed successfully';
      banner.className = 'wingbits-banner wingbits-banner-success';
    } else if (exitCode !== null) {
      banner.textContent = '✗ Setup failed (exit code ' + exitCode + ')';
      banner.className = 'wingbits-banner wingbits-banner-fail';
    }
    if (exitCode !== null) {
      banner.classList.remove('hidden');
      outputActions.classList.remove('hidden');
      loadWingbits();
    }
  } catch (e) {
    if (e.name !== 'AbortError') output.textContent += '\nError: ' + e.message;
  } finally {
    state.wingbitsAbort = null;
    btn.disabled = false;
    btn.textContent = 'Run Setup';
  }
}

function clearWingbitsOutput() {
  document.getElementById('wingbits-output').classList.add('hidden');
  document.getElementById('wingbits-output').textContent = '';
  document.getElementById('wingbits-banner').classList.add('hidden');
  document.getElementById('wingbits-output-actions').classList.add('hidden');
}

// ── System Info ──────────────────────────────────────────────────────────────

function renderSysinfo(d, showHostname) {
  const el = document.getElementById('sysinfo-body');

  if (d.hostname) state.sysHostname = d.hostname;

  function barPctClass(pct) {
    if (pct >= 90) return 'util-bar-red';
    if (pct >= 70) return 'util-bar-amber';
    return 'util-bar-green';
  }

  function tempBarClass(c) {
    if (c >= 75) return 'util-bar-red';
    if (c >= 60) return 'util-bar-amber';
    return 'util-bar-green';
  }

  function tempTextClass(c) {
    if (c >= 75) return 'temp-red';
    if (c >= 60) return 'temp-amber';
    return 'temp-green';
  }

  function barHtml(pct, cls) {
    if (pct === null || pct === undefined) return '';
    const w = Math.min(Math.max(pct, 0), 100);
    return `<div class="util-bar"><div class="util-bar-fill ${cls}" style="width:${w}%"></div></div>`;
  }

  // CPU temp
  let temp = d.cpu_temp;
  if (temp !== 'unavailable') {
    temp = d.cpu_temp.replace("temp=", "").replace("'C", " °C");
  }
  const cpuRaw = d.cpu_temp_raw;
  const cpuText = cpuRaw !== null && cpuRaw !== undefined
    ? `<span class="${tempTextClass(cpuRaw)}">${temp}</span>`
    : `<span class="dim">${temp}</span>`;
  const cpuBar = cpuRaw !== null && cpuRaw !== undefined
    ? barHtml(cpuRaw, tempBarClass(cpuRaw))
    : '';

  // Memory
  let memLine = '';
  const memMatch = d.memory.match(/^Mem:\s+(\d+)\s+(\d+)/m);
  if (memMatch && d.mem_used_pct !== null && d.mem_used_pct !== undefined) {
    memLine = `<span>${memMatch[2]} / ${memMatch[1]} MB <span class="dim">(${d.mem_used_pct}%)</span></span>`;
  }
  if (!memLine) memLine = `<span class="dim">${d.memory || 'unavailable'}</span>`;

  // Storage
  let diskLine = '';
  const diskRows = d.disk.trim().split('\n');
  if (diskRows.length >= 2) {
    const parts = diskRows[1].trim().split(/\s+/);
    if (parts.length >= 5) {
      diskLine = `<span>${parts[2]} used of ${parts[1]} <span class="dim">(${d.disk_used_pct}%)</span></span>`;
    }
  }
  if (!diskLine) diskLine = `<span class="dim">${d.disk || 'unavailable'}</span>`;

  const rows = [
    ['CPU temp', cpuText + cpuBar],
    ['Memory', memLine + barHtml(d.mem_used_pct, barPctClass(d.mem_used_pct || 0))],
    ['Storage', diskLine + barHtml(d.disk_used_pct, barPctClass(d.disk_used_pct || 0))],
    ['Uptime', `<span>${d.uptime || 'unavailable'}</span>`],
  ];
  if (showHostname && d.hostname) {
    rows.unshift(['Hostname', `<code>${d.hostname}</code>`]);
  }
  el.innerHTML = kv(rows);
}

// ── Network — Interfaces ─────────────────────────────────────────────────────

function startNetworkRefresh() {
  loadNetwork();
  state.netInterval = setInterval(loadNetwork, 30_000);
}

async function loadNetwork() {
  const [ifaces, ts, scan, saved] = await Promise.allSettled([
    api('/api/network/interfaces'),
    api('/api/network/tailscale'),
    api('/api/network/wifi/scan'),
    api('/api/network/wifi/saved'),
  ]);
  if (ifaces.status === 'fulfilled') renderInterfaces(ifaces.value);
  if (ts.status     === 'fulfilled') {
    renderTailscaleInterface(ts.value);
    renderTailscaleOptions(ts.value);
  }
  if (ifaces.status === 'fulfilled') {
    renderWifiNetworks(
      scan.status === 'fulfilled' ? scan.value : null,
      saved.status === 'fulfilled' ? saved.value : null,
      ifaces.value.wlan0 || {}
    );
  }
}

function renderInterfaces(d) {
  const eth0 = d.eth0 || {};
  const wlan0 = d.wlan0 || {};
  for (const [name, info] of [['eth0', eth0], ['wlan0', wlan0]]) {
    const el = document.getElementById(`iface-${name}`);
    const linkLabel = info.link === 'Up' ? '<span class="badge badge-green">● Up</span>'
      : info.link === 'N/A' ? '<span class="badge badge-dim">N/A</span>'
      : '<span class="badge badge-yellow">● Down</span>';
    const rows = [
      ['Link', linkLabel],
      ['MAC', info.mac ? `<code>${info.mac}</code>` : '<span class="dim">—</span>'],
      ['IPv4', info.ipv4 ? `<code>${info.ipv4}</code>` : '<span class="dim">—</span>'],
      ['IPv6', info.ipv6 ? `<code class="addr">${abbrev(info.ipv6)}</code>` : '<span class="dim">—</span>'],
    ];
    if (name === 'wlan0') {
      rows.splice(1, 0, ['SSID', info.ssid || 'N/A']);
    }
    el.innerHTML = kv(rows);
  }
  // WiFi toggle
  const toggle = document.getElementById('wifi-toggle');
  const statusEl = document.getElementById('wifi-toggle-status');
  const warning = document.getElementById('wlan0-wifi-warning');
  const eth0Up = eth0.link === 'Up';
  warning.classList.toggle('hidden', eth0Up);
  if (wlan0.wifi_enabled === null) {
    toggle.disabled = true;
    toggle.checked = false;
    document.getElementById('wlan0-wifi-toggle-body').classList.add('dim');
  } else {
    toggle.disabled = false;
    toggle.checked = wlan0.wifi_enabled;
    document.getElementById('wlan0-wifi-toggle-body').classList.remove('dim');
  }
  statusEl.textContent = '';
}


// ── Network — WiFi Scan & Saved ──────────────────────────────────────────────

function _wifiSigClass(signal) {
  if (signal >= 70) return 'wifi-signal-3';
  if (signal >= 50) return 'wifi-signal-2';
  if (signal >= 30) return 'wifi-signal-1';
  return 'wifi-signal-0';
}

function _wifiSigBar(signal) {
  const bars = signal >= 70 ? '●●●●' : signal >= 50 ? '●●●○' : signal >= 30 ? '●●○○' : '●○○○';
  return `<span class="wifi-signal ${_wifiSigClass(signal)}">${bars} ${signal}%</span>`;
}

function renderWifiNetworks(scanData, savedData, wlan0) {
  const section = document.getElementById('wifi-networks-section');
  const savedSection = document.getElementById('wifi-saved-section');
  const scanSection = document.getElementById('wifi-scan-section');
  const scanList = document.getElementById('wifi-scan-list');

  const wifiOn = wlan0.wifi_enabled === true;

  if (!wifiOn) {
    section.classList.add('hidden');
    return;
  }
  section.classList.remove('hidden');

  const saved = (savedData && savedData.saved) ? savedData.saved : [];
  if (saved.length > 0) {
    const maxTs = Math.max(...saved.map(s => s.timestamp || 0));
    const now = Date.now() / 1000;
    savedSection.classList.remove('hidden');
    savedSection.innerHTML = '<div class="wifi-scan-header"><span class="kv-label">Saved networks</span></div>' +
      saved.map(s => {
        let timeLabel = '';
        let prefTag = '';
        if (!s.timestamp) {
          timeLabel = '<span class="dim">Never connected</span>';
        } else {
          const diff = now - s.timestamp;
          let rel;
          if (diff < 60) rel = 'just now';
          else if (diff < 3600) rel = Math.round(diff / 60) + 'm ago';
          else if (diff < 86400) rel = Math.round(diff / 3600) + 'h ago';
          else rel = Math.round(diff / 86400) + 'd ago';
          timeLabel = '<span class="dim">Last: ' + rel + '</span>';
          if (s.timestamp === maxTs && maxTs > 0) {
            prefTag = ' <span class="badge badge-info" style="font-size:.65rem">Preferred</span>';
          }
        }
        return `
          <div class="wifi-network-row">
            <div class="wifi-network-info"><div><code>${s.name}</code>${prefTag}</div>${timeLabel}</div>
            <div>
              <button class="btn btn-sm btn-saved-connect" data-name="${s.name}">Connect</button>
              <button class="btn btn-sm btn-amber btn-saved-forget" data-name="${s.name}">Forget</button>
            </div>
          </div>
        `;
      }).join('');
  } else {
    savedSection.classList.add('hidden');
  }

  const available = (scanData && scanData.available) ? scanData : null;
  const networks = (available && available.networks) ? available.networks : [];

  scanSection.classList.remove('hidden');

  if (networks.length === 0) {
    scanList.innerHTML = '<div class="hint mt-sm">No networks found</div>';
  } else {
    scanList.innerHTML = networks.map(n => `
      <div class="wifi-network-row">
        <span class="wifi-network-info">${_wifiSigBar(n.signal)} <code>${n.ssid}</code>${n.open ? ' <span class="dim">(open)</span>' : ''}</span>
        <button class="btn btn-sm btn-wifi-connect" data-ssid="${n.ssid}"${n.open ? ' data-open="1"' : ''}>Connect</button>
      </div>
    `).join('');
  }

  const rows = document.querySelectorAll('.btn-wifi-connect');
  rows.forEach(btn => {
    btn.addEventListener('click', () => _wifiShowPassword(btn));
  });

  document.querySelectorAll('.btn-saved-connect').forEach(btn => {
    btn.addEventListener('click', () => _wifiConnectSaved(btn));
  });

  document.querySelectorAll('.btn-saved-forget').forEach(btn => {
    btn.addEventListener('click', () => _wifiShowForgetConfirm(btn));
  });
}


function _wifiShowPassword(btn) {
  const row = btn.closest('.wifi-network-row');
  if (!row) return;

  if (btn.dataset.open === '1') {
    _wifiConnectOpen(btn);
    return;
  }
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains('wifi-connect-inline')) {
    existing.remove();
    btn.textContent = 'Connect';
    return;
  }
  btn.textContent = 'Cancel';
  const ssid = btn.dataset.ssid;
  const inline = document.createElement('div');
  inline.className = 'wifi-connect-inline';
  inline.innerHTML = `
    <div class="wifi-connect-row">
      <input type="password" class="wingbits-input" placeholder="Password" autocomplete="off" spellcheck="false" style="max-width:200px">
      <button class="btn btn-sm btn-primary btn-wifi-submit" data-ssid="${ssid}" disabled>Join</button>
    </div>
    <span class="hint" style="font-size:.72rem">8\u201363 characters</span>
    <span class="wifi-connect-msg"></span>
  `;
  row.after(inline);

  const input = inline.querySelector('input');
  const submit = inline.querySelector('.btn-wifi-submit');
  const msg = inline.querySelector('.wifi-connect-msg');

  input.addEventListener('keydown', e => {
    if (e.key === 'Enter') submit.click();
  });

  input.addEventListener('input', () => {
    submit.disabled = (input.value.length < 8);
  });

  submit.addEventListener('click', async () => {
    const password = input.value;
    if (!password) {
      msg.textContent = 'Password required';
      msg.className = 'wifi-connect-msg result-error';
      return;
    }
    submit.disabled = true;
    input.disabled = true;
    msg.textContent = 'Connecting\u2026';
    msg.className = 'wifi-connect-msg dim';

    try {
      const resp = await fetch('/api/network/wifi/connect', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          Authorization: 'Bearer ' + state.token,
        },
        body: JSON.stringify({ ssid, password }),
      });
      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || 'HTTP ' + resp.status);
      }
      inline.remove();
      btn.textContent = 'Connect';
      msg.textContent = '';
      setTimeout(loadNetwork, 2000);
    } catch (err) {
      msg.textContent = 'Error \u2014 ' + err.message;
      msg.className = 'wifi-connect-msg result-error';
      submit.disabled = false;
      input.disabled = false;
    }
  });
}


async function _wifiConnectOpen(btn) {
  const ssid = btn.dataset.ssid;
  if (!ssid) return;
  const row = btn.closest('.wifi-network-row');
  let msgEl = row ? row.nextElementSibling : null;
  if (!msgEl || !msgEl.classList.contains('wifi-connect-msg')) {
    msgEl = document.createElement('span');
    msgEl.className = 'wifi-connect-msg';
    if (row && row.parentNode) {
      row.parentNode.insertBefore(msgEl, row.nextSibling);
    }
  }

  btn.disabled = true;
  msgEl.textContent = 'Connecting\u2026';
  msgEl.className = 'wifi-connect-msg dim';

  try {
    const resp = await fetch('/api/network/wifi/connect', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer ' + state.token,
      },
      body: JSON.stringify({ ssid, password: '' }),
    });
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      throw new Error(errData.detail || 'HTTP ' + resp.status);
    }
    msgEl.textContent = 'Done';
    msgEl.className = 'wifi-connect-msg result-ok';
    setTimeout(() => { msgEl.textContent = ''; }, 2000);
    setTimeout(loadNetwork, 2000);
  } catch (err) {
    msgEl.textContent = 'Error \u2014 ' + err.message;
    msgEl.className = 'wifi-connect-msg result-error';
  } finally {
    btn.disabled = false;
  }
}


async function _wifiConnectSaved(btn) {
  const name = btn.dataset.name;
  if (!name) return;
  const row = btn.closest('.wifi-network-row');
  let msgEl = row ? row.nextElementSibling : null;
  if (!msgEl || !msgEl.classList.contains('wifi-connect-msg')) {
    msgEl = document.createElement('span');
    msgEl.className = 'wifi-connect-msg';
    if (row && row.parentNode) {
      row.parentNode.insertBefore(msgEl, row.nextSibling);
    }
  }

  btn.disabled = true;
  msgEl.textContent = 'Connecting\u2026';
  msgEl.className = 'wifi-connect-msg dim';

  try {
    const resp = await fetch('/api/network/wifi/connect-saved', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer ' + state.token,
      },
      body: JSON.stringify({ name }),
    });
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      throw new Error(errData.detail || 'HTTP ' + resp.status);
    }
    msgEl.textContent = 'Done';
    msgEl.className = 'wifi-connect-msg result-ok';
    setTimeout(() => { msgEl.textContent = ''; }, 2000);
    setTimeout(loadNetwork, 2000);
  } catch (err) {
    msgEl.textContent = 'Error \u2014 ' + err.message;
    msgEl.className = 'wifi-connect-msg result-error';
  } finally {
    btn.disabled = false;
  }
}


function _wifiShowForgetConfirm(btn) {
  const row = btn.closest('.wifi-network-row');
  if (!row) return;
  const existing = row.querySelector('.wifi-forget-confirm');
  if (existing) {
    existing.remove();
    btn.textContent = 'Forget';
    return;
  }
  btn.textContent = 'Cancel';
  const name = btn.dataset.name;
  const confirm = document.createElement('div');
  confirm.className = 'wifi-forget-confirm';
  confirm.style.marginTop = '.3rem';
  confirm.innerHTML = `
    <span class="warn-text">Forget this network?</span>
    <button class="btn btn-sm btn-danger btn-wifi-forget-exec" data-name="${name}">Forget</button>
    <a href="#" class="confirm-cancel">&nbsp;Cancel</a>
  `;
  row.after(confirm);

  confirm.querySelector('.confirm-cancel').addEventListener('click', e => {
    e.preventDefault();
    confirm.remove();
    btn.textContent = 'Forget';
  });

  confirm.querySelector('.btn-wifi-forget-exec').addEventListener('click', () => {
    _wifiForget(btn, name, confirm);
  });
}


async function _wifiForget(btn, name, confirm) {
  btn.disabled = true;
  if (confirm) {
    const exec = confirm.querySelector('.btn-wifi-forget-exec');
    if (exec) exec.disabled = true;
  }

  try {
    const resp = await fetch('/api/network/wifi/forget', {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        Authorization: 'Bearer ' + state.token,
      },
      body: JSON.stringify({ name }),
    });
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      throw new Error(errData.detail || 'HTTP ' + resp.status);
    }
    if (confirm) confirm.remove();
    btn.textContent = 'Forget';
    setTimeout(loadNetwork, 1000);
  } catch (err) {
    if (confirm) {
      const msg = confirm.querySelector('.wifi-connect-msg');
      if (msg) {
        msg.textContent = 'Error \u2014 ' + err.message;
        msg.className = 'wifi-connect-msg result-error';
      } else {
        const newMsg = document.createElement('span');
        newMsg.className = 'wifi-connect-msg result-error';
        newMsg.textContent = 'Error \u2014 ' + err.message;
        confirm.appendChild(newMsg);
      }
    }
    btn.textContent = 'Forget';
  } finally {
    btn.disabled = false;
  }
}


// ── Network — Tailscale Interface Card ───────────────────────────────────────

function renderTailscaleInterface(d) {
  const el = document.getElementById('iface-tailscale');
  const banner = document.getElementById('tailscale-mismatch-banner');
  if (d.status === 'not-installed') {
    el.innerHTML = '<div class="kv-row"><span class="kv-label">Status</span><span class="badge badge-dim">○ Not installed</span></div>' +
      '<p class="hint mt">Run <code>sudo /opt/gateway/scripts/install-tailscale.sh</code> to install.</p>';
    banner.classList.add('hidden');
    return;
  }
  if (d.status === 'stopped') {
    el.innerHTML = '<div class="kv-row"><span class="kv-label">Status</span><span class="badge badge-yellow">● Stopped</span></div>' +
      '<p class="hint mt">Start with <code>sudo systemctl start tailscaled</code>.</p>';
    banner.classList.add('hidden');
    return;
  }
  if (d.status === 'needs-login') {
    const authUrl = (typeof d.auth_url === 'string' && /^https:\/\/[A-Za-z0-9./_-]+$/.test(d.auth_url)) ? d.auth_url : '';
    el.innerHTML = kv([
      ['Status', '<span class="badge badge-yellow">● Needs login</span>'],
      ['Version', d.version ? `<code>${d.version}</code>` : '<span class="dim">—</span>'],
    ]) +
      '<p class="hint mt">Tailscale is logged out — this usually means the device\'s machine record was removed from the Tailscale admin console. ' +
      (d.auto_reauth_key_present
        ? 'A saved auth key is present: the gateway retries authentication automatically (within ~10 minutes). If this state persists, the saved key may be revoked or expired — paste a fresh key below.'
        : 'No saved auth key is present, so automatic recovery is disabled. Paste an auth key below, or use the link to re-authenticate in the browser (preserves all settings, no key needed).') +
      '</p>' +
      (authUrl ? `<p class="mt"><a class="btn btn-primary" href="${authUrl}" target="_blank" rel="noopener">Re-authenticate in browser</a></p>` : '');
    banner.classList.add('hidden');
    return;
  }
  if (d.status !== 'connected') {
    el.innerHTML = `<div class="kv-row"><span class="kv-label">Status</span><span class="badge badge-yellow">● ${d.status}</span></div>`;
    banner.classList.add('hidden');
    return;
  }
  const onlineLabel = d.online
    ? '<span class="badge badge-green">● Connected</span>'
    : '<span class="badge badge-yellow">● Offline</span>';
  el.innerHTML = kv([
    ['Status', onlineLabel],
    ['Tailscale IP', d.ip ? `<code>${d.ip}</code>` : '<span class="dim">—</span>'],
    ['Hostname', d.hostname || '<span class="dim">—</span>'],
    ['Version', d.version ? `<code>${d.version}</code>` : '<span class="dim">—</span>'],
    ['Subnet Routing', d.subnet_routing_enabled ? '<span class="badge badge-green">Enabled</span>' : '<span class="badge badge-dim">Disabled</span>'],
    ['Tailscale SSH', d.ssh_enabled ? '<span class="badge badge-green">Enabled</span>' : '<span class="badge badge-dim">Disabled</span>'],
  ]);

  if (d.tailscale_hostname_mismatch && d.tailscale_hostname_actual) {
    const sysHost = state.sysHostname || 'unknown';
    if (d.tailscale_hostname_mismatch_type === 'drift') {
      banner.innerHTML = `Hostname mismatch detected — this device's system hostname is <code>${sysHost}</code> but Tailscale has it registered as <code>${d.tailscale_hostname_actual}</code>. This can happen if the hostname was changed while Tailscale was connected. Use Settings to update the hostname, or run <code>tailscale set --hostname=${sysHost}</code> to sync.`;
    } else {
      banner.innerHTML = `Tailscale hostname mismatch detected — this device is registered as <code>${d.tailscale_hostname_actual}</code> but its system hostname is <code>${sysHost}</code>. This usually means the device was re-flashed and Tailscale auto-renamed it to avoid colliding with a stale prior registration. <a href="https://login.tailscale.com/admin/machines" target="_blank" rel="noopener">Open Tailscale admin console to remove the old entry.</a>`;
    }
    banner.classList.remove('hidden');
  } else {
    banner.classList.add('hidden');
  }
}

// ── Network — Tailscale Options Card ─────────────────────────────────────────

function renderTailscaleOptions(d) {
  const optionsCard = document.getElementById('tailscale-options-card');
  const routingToggle = document.getElementById('ts-routing-toggle');
  const subnetsInput = document.getElementById('ts-subnets');
  const applyBtn = document.getElementById('btn-ts-routing');
  const sshToggle = document.getElementById('ts-ssh-toggle');
  const routingFields = document.getElementById('ts-routing-fields');

  const connected = d.status === 'connected' && d.online;

  optionsCard.classList.toggle('card-disabled', !connected);
  routingToggle.disabled = !connected;
  sshToggle.disabled = !connected;

  if (!connected) {
    subnetsInput.disabled = true;
    applyBtn.disabled = true;
    return;
  }

  // Only update toggle state from backend if UI hasn't been touched by the user.
  // Compare current backend state vs current UI state — if they differ, the user
  // hasn't interacted yet or the backend was changed externally (clobber).
  const hasRoutes = d.advertised_routes && d.advertised_routes.length > 0;

  if (routingToggle.checked !== hasRoutes) {
    routingToggle.checked = hasRoutes;
    subnetsInput.value = hasRoutes ? d.advertised_routes.join(', ') : '';
    routingFields.classList.toggle('hidden', !hasRoutes);
    subnetsInput.disabled = !hasRoutes;
    applyBtn.disabled = !hasRoutes;
  }

  if (sshToggle.checked !== d.ssh_enabled) {
    sshToggle.checked = d.ssh_enabled;
  }
}

// ── Network — Tailscale Auth ─────────────────────────────────────────────────

function _tsKeyValidate(key) {
  return /^tskey(-auth)?-[A-Za-z0-9_-]+$/.test(key);
}

function _tsKeyUpdateBtn() {
  const key = document.getElementById('tailscale-key').value.trim();
  const btn = document.getElementById('btn-tailscale-connect');
  const msg = document.getElementById('tailscale-key-msg');
  if (!key) {
    btn.disabled = true;
    msg.classList.add('hidden');
    return;
  }
  if (_tsKeyValidate(key)) {
    btn.disabled = false;
    msg.classList.add('hidden');
  } else {
    btn.disabled = true;
    msg.textContent = 'Key must start with tskey- or tskey-auth-';
    msg.className = 'wingbits-input-msg';
  }
}

async function connectTailscale() {
  const key = document.getElementById('tailscale-key').value.trim();
  if (!_tsKeyValidate(key)) return;
  const btn = document.getElementById('btn-tailscale-connect');
  btn.disabled = true;
  btn.textContent = 'Connecting…';
  try {
    await api('/api/network/tailscale/connect', 'POST', { key });
    showResult('tailscale-auth-result', 'Connected ✓', false);
    document.getElementById('tailscale-key').value = '';
    setTimeout(loadNetwork, 2000);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('tailscale-auth-result', e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Connect';
  }
}

// ── Network — Tailscale Subnet Routing ───────────────────────────────────────

async function applyTailscaleRouting() {
  const subnets = document.getElementById('ts-subnets').value.trim();
  const btn = document.getElementById('btn-ts-routing');
  btn.disabled = true;
  btn.textContent = 'Applying…';
  try {
    await api('/api/network/tailscale/routes', 'POST', { subnets });
    showResult('ts-routing-result', 'Applied ✓', false);
    setTimeout(loadNetwork, 2000);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('ts-routing-result', e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Apply';
  }
}

// ── Network — Tailscale SSH ──────────────────────────────────────────────────

async function applyTailscaleSsh(enabled) {
  try {
    await api('/api/network/tailscale/ssh', 'POST', { enabled });
    showResult('ts-ssh-result', enabled ? 'SSH enabled ✓' : 'SSH disabled ✓', false);
  } catch (e) {
    if (e.message !== 'unauthorized') {
      showResult('ts-ssh-result', e.message, true);
      document.getElementById('ts-ssh-toggle').checked = !enabled;
    }
  }
}

// ── Settings — OTA Updates ──────────────────────────────────────────────────

function renderOtaCard(d) {
  const localEl = document.getElementById('ota-local-ver');
  if (d.local && d.local !== 'unknown') {
    localEl.textContent = fmtVersion(d.local);
    localEl.className = 'kv-value';
  } else {
    localEl.textContent = 'Unknown';
    localEl.className = 'kv-value dim';
  }
  if (d.update_available) {
    document.getElementById('ota-status').classList.add('hidden');
    document.getElementById('ota-update-available').classList.remove('hidden');
    document.getElementById('ota-version-compare').innerHTML =
      `<span><span class="dim">${fmtVersion(d.local)}</span> <span style="color:var(--text-dim)">→</span> ` +
      `<a href="${d.release_url || '#'}" target="_blank" rel="noopener" style="color:var(--cyan)">${d.latest}</a></span>`;
    const notesWrap = document.getElementById('ota-release-notes-wrap');
    if (d.release_notes) {
      notesWrap.classList.remove('hidden');
      const html = marked.parse(d.release_notes)
        .replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '')
        .replace(/\s+on\w+\s*=\s*(?:"[^"]*"|'[^']*'|[^\s>]+)/gi, '');
      document.getElementById('ota-release-notes').innerHTML = html;
    } else {
      notesWrap.classList.add('hidden');
    }
    checkOtaChanges();
  } else {
    document.getElementById('ota-status').classList.remove('hidden');
    document.getElementById('ota-update-available').classList.add('hidden');
    if (d.check_failed) {
      const el = document.getElementById('ota-check-result');
      el.textContent = "Couldn't check for updates — try again later";
      el.className = 'result-msg dim';
      setTimeout(() => { el.textContent = ''; el.className = 'result-msg'; }, 5000);
    } else if (d.latest) {
      showResult('ota-check-result', `Latest: ${d.latest} — up to date`, false);
    }
  }
}

function updateHeaderBadge(ver) {
  const badge = document.getElementById('header-update-badge');
  if (ver && ver.update_available) {
    badge.classList.remove('hidden');
  } else {
    badge.classList.add('hidden');
  }
}

async function loadOtaStatus() {
  const btn = document.getElementById('btn-ota-check');
  btn.disabled = true;
  btn.textContent = 'Checking…';
  try {
    const d = await api('/api/system/version');
    state.cachedVersion = d;
    updateHeaderBadge(d);
    renderOtaCard(d);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('ota-check-result', e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Check for updates';
  }
}

async function viewOtaLog() {
  const el = document.getElementById('ota-log-display');
  try {
    const r = await fetch('/api/system/ota/log', {
      headers: { Authorization: `Bearer ${state.token}` },
    });
    const text = await r.text();
    el.textContent = text || '(no OTA log yet)';
    el.classList.remove('hidden');
  } catch (e) {
    el.textContent = 'Error: ' + e.message;
    el.classList.remove('hidden');
  }
}

async function checkOtaChanges() {
  try {
    const d = await api('/api/system/ota/changes');
    state.otaChanges = d;
    const checksEl = document.getElementById('ota-service-checks');
    if (d.affected_groups && d.affected_groups.length > 0) {
      checksEl.innerHTML = d.affected_groups.map(g =>
        `<label class="ota-check-row">
          <input type="checkbox" class="ota-svc-check" checked
            data-services="${g.services.join(',')}">
          <span>${g.label} (${g.services.map(s => s.replace('.service', '')).join(', ')})</span>
        </label>`
      ).join('');
    } else {
      checksEl.innerHTML = '<span class="dim">No services affected by this update.</span>';
    }
    const bootNote = document.getElementById('ota-boot-note');
    if (d.boot_changes && d.boot_changes.length > 0) {
      const provisioning = ['boot/bootstrap.sh', 'boot/firstrun.sh', 'boot/config.txt'];
      const release = ['boot/build-image.sh', 'boot/tag-release.sh'];

      const a = d.boot_changes.filter(f => provisioning.includes(f));
      const b = d.boot_changes.filter(f => release.includes(f));
      const other = d.boot_changes.filter(f => !provisioning.includes(f) && !release.includes(f));

      const esc = (arr) => arr.map(f => f.replace(/</g, '&lt;').replace(/>/g, '&gt;')).join(', ');

      const parts = [];
      if (a.length > 0) {
        parts.push('Provisioning files changed (' + esc(a) + ') \u2014 these apply to fresh device setups only; no action needed on this device.');
      }
      if (b.length > 0) {
        parts.push('Release tooling changed (' + esc(b) + ') \u2014 runs on the Mac only, not on this device; no action needed.');
      }
      if (other.length > 0) {
        parts.push('Files changed in boot/ (' + esc(other) + ') \u2014 no action needed on this device.');
      }

      bootNote.innerHTML = parts.join('<br>');
      bootNote.classList.remove('hidden');
    } else {
      bootNote.classList.add('hidden');
    }
    updateOtaConfirmBtn();
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('ota-check-result', e.message, true);
  }
}

function updateOtaConfirmBtn() {
  const btn = document.getElementById('btn-ota-update');
  const d = state.otaChanges;
  if (!d) { btn.disabled = true; return; }
  if (d.affected_groups && d.affected_groups.length === 0) { btn.disabled = false; return; }
  const checks = document.querySelectorAll('.ota-svc-check:checked');
  btn.disabled = checks.length === 0;
}

async function runOtaUpdate() {
  const checks = document.querySelectorAll('.ota-svc-check:checked');
  let services;
  if (checks.length > 0) {
    services = Array.from(checks).flatMap(cb => cb.dataset.services.split(','));
  } else if (state.otaChanges && state.otaChanges.affected_groups && state.otaChanges.affected_groups.length === 0) {
    services = ['gateway-ui.service'];
  } else {
    return;
  }
  const btn = document.getElementById('btn-ota-update');
  const output = document.getElementById('ota-output');
  btn.disabled = true;
  btn.textContent = 'Updating…';
  output.classList.remove('hidden');
  output.textContent = '';
  try {
    const r = await fetch('/api/system/ota/update', {
      method: 'POST',
      headers: { Authorization: `Bearer ${state.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ services }),
    });
    if (r.status === 409) { output.textContent = 'Update already in progress.'; return; }
    if (r.status === 503) { const err = await r.json(); output.textContent = 'Error: ' + err.detail; return; }
    if (!r.ok) {
      let detail = 'Request failed';
      try { detail = (await r.json()).detail || detail; } catch {}
      output.textContent = 'Error: ' + detail; return;
    }
    const reader = r.body.getReader();
    const decoder = new TextDecoder();
    let buf = '';
    let exitCode = null;
    let newVersion = null;
    const reloadMsg = document.getElementById('ota-reload-msg');
    const countdown = document.getElementById('ota-countdown');
    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buf += decoder.decode(value, { stream: true });
      const parts = buf.split('\n\n');
      buf = parts.pop();
      for (const part of parts) {
        for (const line of part.split('\n')) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6);
            try {
              const obj = JSON.parse(data);
              if (obj.exit_code !== undefined) {
                exitCode = obj.exit_code;
                if (obj.version) newVersion = obj.version;
                break;
              }
            } catch {
              // Plain text line
              output.textContent += data + '\n';
              output.scrollTop = output.scrollHeight;
            }
          }
        }
        if (exitCode !== null) break;
      }
    }
    if (exitCode === 0) {
      if (services.some(s => s === 'gateway-ui.service')) {
        // UI is restarting — show countdown
        reloadMsg.classList.remove('hidden');
        let sec = 5;
        countdown.textContent = sec;
        state.otaCountdown = setInterval(() => {
          sec--;
          countdown.textContent = sec;
          if (sec <= 0) {
            clearInterval(state.otaCountdown);
            location.reload();
          }
        }, 1000);
      } else {
        output.textContent += '\n✓ Update completed successfully' + (newVersion ? ' (' + newVersion + ')' : '');
      }
    } else if (exitCode !== null) {
      output.textContent += '\n✗ Update failed (exit ' + exitCode + ')';
    }
  } catch (e) {
    if (e.name === 'TypeError' && e.message.includes('network')) {
      // Connection dropped — UI restarting
      const reloadMsg = document.getElementById('ota-reload-msg');
      reloadMsg.classList.remove('hidden');
      let sec = 5;
      document.getElementById('ota-countdown').textContent = sec;
      state.otaCountdown = setInterval(() => {
        sec--;
        document.getElementById('ota-countdown').textContent = sec;
        if (sec <= 0) {
          clearInterval(state.otaCountdown);
          location.reload();
        }
      }, 1000);
    }
  } finally {
    if (!services.some(s => s === 'gateway-ui.service')) {
      btn.disabled = false;
      btn.textContent = 'Confirm Update';
    }
  }
}


// ── Logs ─────────────────────────────────────────────────────────────────────

function startLogAutoRefresh() {
  loadLogs();
  state.logInterval = setInterval(loadLogs, 10_000);
}

function getActiveLogUnits() {
  const pills = document.querySelectorAll('.log-pill.active');
  return Array.from(pills).map(p => p.dataset.unit).join(',');
}

async function loadLogs() {
  const units = getActiveLogUnits();
  try {
    const data = await api(`/api/logs?units=${encodeURIComponent(units)}`);
    state.logLines = data.lines;
    renderLogOutput();
    document.getElementById('log-status').textContent =
      `Updated ${new Date().toLocaleTimeString()}`;
  } catch (e) {
    if (e.message !== 'unauthorized') {
      document.getElementById('log-status').textContent = `Error: ${e.message}`;
    }
  }
}

function renderLogOutput() {
  const output = document.getElementById('log-output');
  output.textContent = state.logLines.length ? state.logLines.join('\n') : '(no matching log lines)';
  output.scrollTop = output.scrollHeight;
}

// ── Settings ─────────────────────────────────────────────────────────────────

async function loadSettings() {
  try {
    const s = await api('/api/settings');
    document.getElementById('port-input').value = s.port;
  } catch (e) {}
  // Render from cache immediately, then refresh
  if (state.cachedVersion) {
    renderOtaCard(state.cachedVersion);
  }
  loadOtaStatus();
  loadNtfyConfig();
  loadTimezones();
  loadHostname();
}

async function savePort() {
  const port = parseInt(document.getElementById('port-input').value, 10);
  try {
    await api('/api/settings/port', 'POST', { port });
    showResult('port-result', `Port saved — reconnect on :${port}`, false);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('port-result', e.message, true);
  }
}

async function revealToken() {
  const btn = document.getElementById('btn-reveal-token');
  if (state.tokenRevealed) {
    document.getElementById('token-display').textContent = '••••••••••••••••';
    btn.textContent = 'Reveal';
    state.tokenRevealed = false;
    return;
  }
  try {
    const r = await api('/api/settings/token');
    document.getElementById('token-display').textContent = r.full;
    btn.textContent = 'Hide';
    state.tokenRevealed = true;
  } catch (e) {
    if (e.message !== 'unauthorized') alert(`Error: ${e.message}`);
  }
}

async function regenToken() {
  if (!confirm('Regenerate bearer token?\n\nYour current session will end. Save the new token before reconnecting.')) return;
  try {
    const r = await api('/api/settings/token', 'POST');
    document.getElementById('new-token-value').textContent = r.token;
    document.getElementById('new-token-box').classList.remove('hidden');
    document.getElementById('btn-regen-token').disabled = true;
    state.token = r.token;
    localStorage.setItem('gw_token', r.token);
  } catch (e) {
    if (e.message !== 'unauthorized') alert(`Error: ${e.message}`);
  }
}

async function copyToken() {
  const val = document.getElementById('new-token-value').textContent;
  try {
    await navigator.clipboard.writeText(val);
    document.getElementById('btn-copy-token').textContent = 'Copied ✓';
    setTimeout(() => { document.getElementById('btn-copy-token').textContent = 'Copy to clipboard'; }, 2000);
  } catch {
    alert('Copy failed — select and copy manually.');
  }
}

// ── Header (hostname + version + update badge) ─────────────────────────────

async function setHeaderInfo() {
  try {
    document.getElementById('header-name').textContent = 'BitCryptic™ OS';
    const ver = await api('/api/system/version');
    state.cachedVersion = ver;
    const headerVer = document.getElementById('header-version');
    if (ver.local && ver.local !== 'unknown') {
      headerVer.textContent = fmtVersion(ver.local);
      headerVer.style.display = '';
    } else {
      headerVer.style.display = 'none';
    }
    updateHeaderBadge(ver);
    // Re-render OTA card if settings tab is visible
    const settingsPanel = document.getElementById('tab-settings');
    if (settingsPanel && !settingsPanel.classList.contains('hidden')) {
      renderOtaCard(ver);
    }
  } catch {}
}

const VERSION_POLL_INTERVAL_MS = 3_600_000; // 1 hour

async function startVersionPoll() {
  await setHeaderInfo();
  state.verInterval = setInterval(setHeaderInfo, VERSION_POLL_INTERVAL_MS);
}

// ── Settings — System Power ──────────────────────────────────────────────────

function disarmReboot() {
  state.rebootArmed = false;
  clearTimeout(state.rebootTimer);
  state.rebootTimer = null;
  const btn = document.getElementById('btn-power-reboot');
  btn.textContent = 'Reboot';
}

function handleRebootClick() {
  if (!state.rebootArmed) {
    state.rebootArmed = true;
    const btn = document.getElementById('btn-power-reboot');
    btn.textContent = 'Confirm reboot?';
    state.rebootTimer = setTimeout(disarmReboot, 4000);
  } else {
    clearTimeout(state.rebootTimer);
    state.rebootTimer = null;
    state.rebootArmed = false;
    powerExecute('reboot');
  }
}

function powerReset() {
  disarmReboot();
  clearTimeout(state.powerTimer);
  state.powerTimer = null;
  state.powerAction = null;
  document.getElementById('power-initial').classList.remove('hidden');
  document.getElementById('power-confirm').classList.add('hidden');
  document.getElementById('power-rebooting').classList.add('hidden');
  document.getElementById('power-shutdown-msg').classList.add('hidden');
  document.getElementById('btn-power-reboot').disabled = false;
  document.getElementById('btn-power-shutdown').disabled = false;
}

function powerStartConfirm(action) {
  disarmReboot();
  state.powerAction = action;
  document.getElementById('power-initial').classList.add('hidden');
  const confirmEl = document.getElementById('power-confirm');
  const confirmText = document.getElementById('power-confirm-text');
  confirmText.textContent = action === 'reboot' ? 'Confirm Reboot?' : 'Confirm Shutdown?';
  confirmEl.className = 'action-row';
  document.getElementById('btn-power-reboot').disabled = true;
  document.getElementById('btn-power-shutdown').disabled = true;

  let sec = 10;
  document.getElementById('power-timer').textContent = `(auto-cancel in ${sec}s)`;
  state.powerTimer = setInterval(() => {
    sec--;
    document.getElementById('power-timer').textContent = `(auto-cancel in ${sec}s)`;
    if (sec <= 0) powerReset();
  }, 1000);
}

async function powerExecute(action) {
  powerReset();
  document.getElementById('power-initial').classList.add('hidden');

  try {
    await api(`/api/system/${action}`, 'POST');
  } catch (e) {
    if (e.message !== 'unauthorized') {
      showResult('power-result', `Failed: ${e.message}`, true);
    }
    document.getElementById('power-initial').classList.remove('hidden');
    return;
  }

  if (action === 'reboot') {
    document.getElementById('power-rebooting').classList.remove('hidden');
    const poll = setInterval(async () => {
      try {
        const r = await fetch('/api/sysinfo', {
          headers: { Authorization: `Bearer ${state.token}` },
        });
        if (r.ok) {
          clearInterval(poll);
          location.reload();
        }
      } catch {}
    }, 3000);
  } else {
    document.getElementById('power-shutdown-msg').classList.remove('hidden');
  }
}

// ── Settings — NTFY Notifications ─────────────────────────────────────────────

async function loadNtfyConfig() {
  try {
    const cfg = await api('/api/notifications/config');
    document.getElementById('ntfy-server').value = cfg.server || '';
    document.getElementById('ntfy-topic').value = cfg.topic || '';
    const tokenInput = document.getElementById('ntfy-token');
    if (cfg.token_set) {
      tokenInput.placeholder = '●●●●●●●● (set)';
      tokenInput.value = '';
      document.getElementById('btn-ntfy-clear-token').classList.remove('hidden');
    } else {
      tokenInput.placeholder = 'Leave blank for public topics';
      document.getElementById('btn-ntfy-clear-token').classList.add('hidden');
    }
    // Checkboxes
    document.querySelectorAll('.ntfy-alert-check').forEach(cb => {
      cb.checked = (cfg.enabled_alerts || []).includes(cb.dataset.key);
    });
    // Status pill
    const pill = document.getElementById('ntfy-status-pill');
    if (cfg.server && cfg.topic) {
      pill.textContent = '● Configured';
      pill.className = 'badge badge-green';
      document.getElementById('btn-ntfy-test').disabled = false;
    } else {
      pill.textContent = '○ Not configured';
      pill.className = 'badge badge-dim';
      document.getElementById('btn-ntfy-test').disabled = true;
    }
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('ntfy-save-result', 'Failed to load config', true);
  }
}

async function saveNtfyConfig() {
  const server = document.getElementById('ntfy-server').value.trim();
  const topic = document.getElementById('ntfy-topic').value.trim();
  const token = document.getElementById('ntfy-token').value;
  const enabled_alerts = Array.from(document.querySelectorAll('.ntfy-alert-check:checked')).map(cb => cb.dataset.key);
  try {
    await api('/api/notifications/config', 'POST', { server, topic, token, enabled_alerts });
    showResult('ntfy-save-result', 'Saved ✓', false);
    loadNtfyConfig();
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('ntfy-save-result', e.message, true);
  }
}

async function saveNtfyAlerts() {
  const server = document.getElementById('ntfy-server').value.trim();
  const topic = document.getElementById('ntfy-topic').value.trim();
  const enabled_alerts = Array.from(document.querySelectorAll('.ntfy-alert-check:checked')).map(cb => cb.dataset.key);
  try {
    // Send without touching token — server and topic are already loaded from GET
    await api('/api/notifications/config', 'POST', { server, topic, token: '', enabled_alerts });
    showResult('ntfy-alerts-result', 'Alerts saved ✓', false);
    loadNtfyConfig();
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('ntfy-alerts-result', e.message, true);
  }
}

async function clearNtfyToken() {
  document.getElementById('ntfy-token').value = '';
  document.getElementById('ntfy-token').placeholder = 'Leave blank for public topics';
  document.getElementById('btn-ntfy-clear-token').classList.add('hidden');
}

async function sendNtfyTest() {
  const btn = document.getElementById('btn-ntfy-test');
  btn.disabled = true;
  btn.textContent = 'Sending…';
  try {
    await api('/api/notifications/test', 'POST');
    showResult('ntfy-test-result', '✓ Sent', false);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult('ntfy-test-result', '✗ Failed — check server/topic', true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Send Test Notification';
  }
}

// ── DePIN ─────────────────────────────────────────────────────────────────────

function startDepinRefresh() {
  loadDepin();
  depinState.interval = setInterval(loadDepin, 30_000);
}

async function loadDepin() {
  try {
    const [d, auto] = await Promise.all([
      api('/api/depin/status'),
      api('/api/depin/auto-update').catch(() => ({})),
    ]);
    depinState.autoUpdate = auto.projects || {};
    renderDepin(d);
    depinPreFill();
  } catch (e) {
    if (e.message !== 'unauthorized') {
      document.querySelectorAll('.depin-status-body').forEach(el => {
        if (el.querySelector('.spinner')) el.innerHTML = `<span class="dim">Error loading status</span>`;
      });
    }
  }
}

let _depinPrefilled = false;
async function depinPreFill() {
  if (_depinPrefilled) return;
  try {
    const host = await api('/api/hostname');
    const input = document.getElementById('hg-device');
    if (input && !input.value && host.hostname) {
      input.value = host.hostname;
      _depinPrefilled = true;
    }
  } catch {}
}

function depinBadge(project, s) {
  const d = s || {};
  if (!d.installed) return '<span class="badge badge-dim">Not installed</span>';
  if (!d.enabled) return '<span class="badge badge-yellow">Disabled</span>';
  if (d.service_state === 'active') {
    const cls = d.health === 'unknown' ? 'green' : d.health === 'connected' || d.health === 'active' || d.health === 'healthy' ? 'green' : 'yellow';
    const label = { connected: 'Connected', disconnected: 'Disconnected', active: 'Active', inactive: 'Inactive', healthy: 'Healthy', unhealthy: 'Unhealthy', unknown: 'Running' }[d.health] || 'Running';
    return `<span class="badge badge-${cls}">${label}</span>`;
  }
  return '<span class="badge badge-red">Error</span>';
}

function depinHealthLine(d) {
  if (!d.logs || !d.logs.length) return '';
  const lastLines = d.logs.slice(-3).map(l => l.trim()).filter(l => l).join(' / ');
  if (!lastLines) return '';
  return `<div class="depin-log-snippet dim">${escHtml(lastLines)}</div>`;
}

function depinEnabledToggle(project, d) {
  if (!d.installed) return '';
  const checked = d.enabled ? ' checked' : '';
  const disabled = '';
  return `<div class="toggle-row">
    <span>${d.enabled ? 'Enabled' : 'Disabled'}</span>
    <label class="switch" aria-label="Enable ${project}">
      <input type="checkbox" class="depin-toggle" data-project="${project}"${checked}${disabled}>
      <span class="slider"></span>
    </label>
  </div>`;
}

function depinUpdateControls(project, d) {
  const s = d || {};
  const hasUpdate = s.update_available === true;
  const auto = depinState.autoUpdate ? depinState.autoUpdate[project] || false : false;
  const updateBtn = hasUpdate
    ? `<button class="btn btn-sm depin-update-btn" data-project="${project}">Update</button>`
    : `<button class="btn btn-sm" disabled>Up to date</button>`;
  return `<div class="depin-update-controls">
    ${updateBtn}
    <label class="depin-auto-check"><input type="checkbox" class="depin-auto-toggle" data-project="${project}"${auto ? ' checked' : ''}> Auto-update</label>
  </div>`;
}

function depinMystNodeUI(d, hostname) {
  const s = d || {};
  if (!s.enabled || !hostname) return '';
  const url = `http://${hostname}:4449`;
  if (s.health === 'active') {
    return `<div class="depin-myst-link dim mt" style="font-size:.85rem">
      NodeUI: <a href="${url}" target="_blank" rel="noopener">${url}</a>
      <span class="dim"> — monitor / manage your node</span>
    </div>`;
  }
  return `<div class="depin-myst-link mt">
    <a href="${url}" target="_blank" rel="noopener">Open Mysterium NodeUI →</a>
    <span class="dim" style="font-size:.85rem"> — connect and claim this node to start earning</span>
  </div>`;
}

function depinMystPortForward(d) {
  const s = d || {};
  if (!s.installed || !s.enabled) return '';
  return `<div class="depin-info mt">
    <strong>NAT tip:</strong> Check your NAT type in the Mysterium NodeUI (linked above).
    Most routers with Moderate NAT work fine without changes. If your status shows
    Strict or Symmetric NAT, forward <strong>UDP ports 56000–56100</strong> on your
    router to this device for full peer connectivity.
  </div>`;
}

function renderDepin(d) {
  const projects = d.projects || {};
  const names = {
    honeygain: 'Honeygain', urnetwork: 'URnetwork', myst: 'Mysterium', anyone: 'Anyone Protocol'
  };

  for (const project of Object.keys(names)) {
    const s = projects[project] || {};
    const statusEl = document.querySelector(`.depin-status-body[data-project="${project}"]`);
    const configEl = document.querySelector(`.depin-config-body[data-project="${project}"]`);

    if (statusEl) {
      const parts = [depinBadge(project, s), depinEnabledToggle(project, s)];
      if (!s.configured && s.config_required && s.service_state !== 'active' && s.enabled) {
        parts.push('<div class="depin-config-hint warn-text">Check configuration — configure this project first, then enable it</div>');
      }
      statusEl.innerHTML =
        '<div class="depin-status-row">' + parts.join('') + '</div>' +
        depinHealthLine(s) +
        (project === 'myst' ? depinMystNodeUI(s, d.hostname) : '') +
        (project === 'myst' ? depinMystPortForward(s) : '') +
        depinUpdateControls(project, s);
    }

    if (configEl) {
      if (s.installed && s.configured) {
        configEl.classList.add('hidden');
      } else {
        configEl.classList.remove('hidden');
      }
    }
  }
}

async function depinConfigure(project) {
  const el = document.getElementById(`depin-result-${project}`);
  const btn = document.querySelector(`.depin-configure-btn[data-project="${project}"]`);
  try {
    let body;
    if (project === 'honeygain') {
      body = {
        device_name: document.getElementById('hg-device').value.trim(),
        email: document.getElementById('hg-email').value.trim(),
        password: document.getElementById('hg-password').value,
      };
      if (!body.device_name) { showResult(`depin-result-${project}`, 'Device name is required', true); return; }
      if (!body.email) { showResult(`depin-result-${project}`, 'Email is required', true); return; }
      if (!body.password) { showResult(`depin-result-${project}`, 'Password is required', true); return; }
    } else if (project === 'anyone') {
      body = {
        nickname: document.getElementById('any-nickname').value.trim(),
        contact: document.getElementById('any-contact').value.trim(),
        myfamily: document.getElementById('any-myfamily').value.trim(),
      };
      if (!body.nickname) { showResult(`depin-result-${project}`, 'Nickname is required', true); return; }
      if (!body.contact) { showResult(`depin-result-${project}`, 'Contact info is required', true); return; }
    }
    btn.disabled = true;
    btn.textContent = 'Saving…';
    await api(`/api/depin/${project}/configure`, 'POST', body);
    showResult(`depin-result-${project}`, 'Saved ✓', false);
    document.getElementById(`hg-password`).value = '';
    if (document.getElementById(`any-myfamily`)) document.getElementById(`any-myfamily`).value = '';
    setTimeout(loadDepin, 500);
  } catch (e) {
    if (e.message !== 'unauthorized') showResult(`depin-result-${project}`, e.message, true);
  } finally {
    btn.disabled = false;
    btn.textContent = 'Save & Configure';
  }
}

async function depinToggle(project, enabled) {
  const action = enabled ? 'enable' : 'disable';
  try {
    await api(`/api/depin/${project}/${action}`, 'POST');
    setTimeout(loadDepin, 1000);
  } catch (e) {
    if (e.message !== 'unauthorized') alert(`${project}: ${e.message}`);
    setTimeout(loadDepin, 500);
  }
}

async function depinUpdateProject(project) {
  if (depinState.updating) return;
  depinState.updating = project;
  const btn = document.querySelector(`.depin-update-btn[data-project="${project}"]`);
  if (btn) { btn.disabled = true; btn.textContent = 'Updating\u2026'; }
  try {
    await api(`/api/depin/${project}/update`, 'POST');
    setTimeout(loadDepin, 3000);
  } catch (e) {
    if (e.message !== 'unauthorized') alert(`${project}: ${e.message}`);
    setTimeout(loadDepin, 500);
  } finally {
    depinState.updating = null;
  }
}

async function depinAutoUpdate(project, enabled) {
  try {
    await api(`/api/depin/${project}/auto-update`, 'POST', { enabled });
    depinState.autoUpdate[project] = enabled;
  } catch (e) {
    if (e.message !== 'unauthorized') alert(`${project}: ${e.message}`);
    setTimeout(loadDepin, 500);
  }
}

function depinUninstall(project, label, msg) {
  const section = document.getElementById(`depin-uninstall-${project}`);
  if (!section) return;
  if (depinState.uninstalling === project) { depinState.uninstalling = null; section.querySelector('.depin-confirm-row')?.remove(); return; }
  depinState.uninstalling = project;
  const existing = section.querySelector('.depin-confirm-row');
  if (existing) existing.remove();
  const row = document.createElement('div');
  row.className = 'depin-confirm-row';
  row.innerHTML =
    `<span class="warn-text">${escHtml(msg)}</span>` +
    `<button class="btn btn-sm btn-danger depin-uninstall-exec" data-project="${project}">Yes, Uninstall</button>` +
    `<a href="#" class="confirm-cancel">&nbsp;Cancel</a>`;
  section.appendChild(row);
  row.querySelector('.confirm-cancel').addEventListener('click', e => { e.preventDefault(); depinState.uninstalling = null; row.remove(); });
  row.querySelector('.depin-uninstall-exec').addEventListener('click', async () => {
    const btn = row.querySelector('.depin-uninstall-exec');
    btn.disabled = true;
    btn.textContent = 'Uninstalling…';
    try {
      await api(`/api/depin/${project}/uninstall`, 'POST', { confirm: true });
      showResult(`depin-uninstall-result-${project}`, 'Uninstalled ✓', false);
      row.remove();
      depinState.uninstalling = null;
      setTimeout(loadDepin, 1000);
    } catch (e) {
      if (e.message !== 'unauthorized') showResult(`depin-uninstall-result-${project}`, e.message, true);
      btn.disabled = false;
      btn.textContent = 'Yes, Uninstall';
    }
  });
}

// ── Utilities ─────────────────────────────────────────────────────────────────

function kv(pairs) {
  return pairs.map(([k, v]) =>
    `<div class="kv-row"><span class="kv-label">${k}</span><span class="kv-value">${v}</span></div>`
  ).join('');
}

function abbrev(addr) {
  if (!addr || addr.length <= 16) return addr;
  return addr.slice(0, 8) + '…' + addr.slice(-6);
}

function fmtTimestamp(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts.replace('+0000', 'Z').replace(' ', 'T'));
    if (isNaN(d)) return ts;
    return d.toLocaleString();
  } catch {
    return ts;
  }
}

function fmtVersion(ver) {
  return ver ? ver.replace(/-\d+-g[0-9a-f]+$/, '') : ver;
}

function showResult(id, msg, isError) {
  const el = document.getElementById(id);
  el.textContent = msg;
  el.className = 'result-msg ' + (isError ? 'result-error' : 'result-ok');
  setTimeout(() => { el.textContent = ''; el.className = 'result-msg'; }, 5000);
}

function escHtml(s) {
  const map = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  return s.replace(/[&<>"']/g, c => map[c]);
}

// ── Event Wiring ──────────────────────────────────────────────────────────────

function wireEvents() {
  // Modal
  document.getElementById('modal-submit').addEventListener('click', handleTokenSubmit);
  document.getElementById('modal-token').addEventListener('keydown', e => {
    if (e.key === 'Enter') handleTokenSubmit();
  });

  // Tabs
  document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => switchTab(btn.dataset.tab));
  });

  // Applications — restart buttons (delegated)
  document.getElementById('helium-services-body').addEventListener('click', async e => {
    const btn = e.target.closest('.btn-restart');
    if (!btn) return;
    const service = btn.dataset.service;
    btn.disabled = true;
    btn.textContent = '…';
    try {
      await api(`/api/restart/${service}`, 'POST');
      setTimeout(loadApplications, 2000);
    } catch (err) {
      if (err.message !== 'unauthorized') alert(`Restart failed: ${err.message}`);
    } finally {
      btn.disabled = false;
      btn.textContent = 'Restart';
    }
  });

  // Applications — band
  document.getElementById('btn-apply-band').addEventListener('click', applyBand);

  // Settings — timezone
  document.getElementById('btn-apply-timezone').addEventListener('click', applyTimezone);

  // Settings — hostname
  document.getElementById('btn-apply-hostname').addEventListener('click', applyHostname);

  // Applications — Wingbits setup
  document.getElementById('wingbits-cmd').addEventListener('input', _wingbitsUpdateBtn);
  document.getElementById('btn-wingbits-run').addEventListener('click', runWingbitsSetup);
  document.getElementById('btn-wingbits-clear').addEventListener('click', clearWingbitsOutput);

  // Network — Tailscale auth
  document.getElementById('tailscale-key').addEventListener('input', _tsKeyUpdateBtn);
  document.getElementById('btn-tailscale-connect').addEventListener('click', connectTailscale);

  // Network — Tailscale routing toggle (show/hide fields, immediate disable)
  document.getElementById('ts-routing-toggle').addEventListener('change', async function() {
    const enabled = this.checked;
    const fields = document.getElementById('ts-routing-fields');
    const subnetsInput = document.getElementById('ts-subnets');
    const applyBtn = document.getElementById('btn-ts-routing');
    if (!enabled) {
      showResult('ts-routing-result', 'Disabling…', false);
      try {
        await api('/api/network/tailscale/routes', 'POST', { subnets: '' });
        fields.classList.add('hidden');
        subnetsInput.disabled = true;
        applyBtn.disabled = true;
        showResult('ts-routing-result', 'Subnet routing disabled ✓', false);
        setTimeout(loadNetwork, 2000);
      } catch (e) {
        if (e.message !== 'unauthorized') showResult('ts-routing-result', e.message, true);
        this.checked = true;
      }
    } else {
      fields.classList.remove('hidden');
      subnetsInput.disabled = false;
      applyBtn.disabled = false;
    }
  });

  // Network — Tailscale SSH toggle (immediate)
  document.getElementById('ts-ssh-toggle').addEventListener('change', async function() {
    const enabled = this.checked;
    applyTailscaleSsh(enabled);
  });

  // Network — Tailscale routing apply
  document.getElementById('btn-ts-routing').addEventListener('click', applyTailscaleRouting);

  // Network — WiFi toggle (delegated)
  document.addEventListener('change', async function(e) {
    if (e.target.id !== 'wifi-toggle') return;
    const enabled = e.target.checked;
    e.target.disabled = true;
    const statusEl = document.getElementById('wifi-toggle-status');
    if (statusEl) statusEl.textContent = 'Applying…';
    try {
      const resp = await fetch('/api/network/wifi', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'Authorization': 'Bearer ' + state.token,
        },
        body: JSON.stringify({ enabled }),
      });
      if (!resp.ok) {
        const errData = await resp.json().catch(() => ({}));
        throw new Error(errData.detail || 'HTTP ' + resp.status);
      }
      if (statusEl) statusEl.textContent = 'Done';
      setTimeout(() => { if (statusEl) statusEl.textContent = ''; }, 2000);
      setTimeout(loadNetwork, 2000);
    } catch (err) {
      console.error('WiFi toggle failed:', err);
      e.target.checked = !enabled;
      if (statusEl) statusEl.textContent = 'Error — ' + err.message;
    } finally {
      e.target.disabled = false;
    }
  });

  // Network — Port
  document.getElementById('btn-save-port').addEventListener('click', savePort);

  // Network — WiFi scan refresh
  document.getElementById('btn-wifi-refresh').addEventListener('click', loadNetwork);

  // Logs
  document.getElementById('btn-refresh-logs').addEventListener('click', loadLogs);
  document.querySelectorAll('.log-pill').forEach(pill => {
    pill.addEventListener('click', () => {
      const activePills = document.querySelectorAll('.log-pill.active');
      if (activePills.length === 1 && activePills[0] === pill) return;
      pill.classList.toggle('active');
      loadLogs();
    });
  });

  // Dashboard — group pill expand/collapse (delegated)
  document.getElementById('dash-services-body').addEventListener('click', function(e) {
    const group = e.target.closest('.service-group');
    if (group) group.classList.toggle('collapsed');
  });

  // Settings — OTA
  document.getElementById('btn-ota-check').addEventListener('click', loadOtaStatus);
  document.getElementById('btn-ota-toggle-notes').addEventListener('click', function() {
    const notes = document.getElementById('ota-release-notes');
    notes.classList.toggle('hidden');
    this.textContent = notes.classList.contains('hidden') ? 'Show release notes' : 'Hide release notes';
  });
  document.getElementById('ota-service-checks').addEventListener('change', updateOtaConfirmBtn);
  document.getElementById('btn-ota-update').addEventListener('click', runOtaUpdate);
  document.getElementById('btn-ota-view-log').addEventListener('click', viewOtaLog);

  // Header update badge — click navigates to Settings tab, triggers OTA load
  document.getElementById('header-update-badge').addEventListener('click', function() {
    switchTab('settings');
  });

  // Settings — System Power
  document.getElementById('btn-power-reboot').addEventListener('click', handleRebootClick);
  document.getElementById('btn-power-shutdown').addEventListener('click', () => powerStartConfirm('shutdown'));
  document.getElementById('power-cancel').addEventListener('click', e => { e.preventDefault(); powerReset(); });
  document.getElementById('power-confirm-text').addEventListener('click', () => {
    if (state.powerAction) powerExecute(state.powerAction);
  });

  // Settings — NTFY
  document.getElementById('btn-ntfy-save').addEventListener('click', saveNtfyConfig);
  document.getElementById('btn-ntfy-save-alerts').addEventListener('click', saveNtfyAlerts);
  document.getElementById('btn-ntfy-clear-token').addEventListener('click', clearNtfyToken);
  document.getElementById('btn-ntfy-test').addEventListener('click', sendNtfyTest);

  // Settings — auth
  document.getElementById('btn-reveal-token').addEventListener('click', revealToken);
  document.getElementById('btn-regen-token').addEventListener('click', regenToken);
  document.getElementById('btn-copy-token').addEventListener('click', copyToken);

  // DePIN — configure buttons (delegated)
  document.getElementById('depin-grid').addEventListener('click', function(e) {
    const btn = e.target.closest('.depin-configure-btn');
    if (btn) { depinConfigure(btn.dataset.project); return; }
    const uninstallBtn = e.target.closest('.depin-uninstall-btn');
    if (uninstallBtn) {
      const project = uninstallBtn.dataset.project;
      const labels = { honeygain: 'Honeygain', urnetwork: 'URnetwork', myst: 'Mysterium', anyone: 'Anyone Protocol' };
      const msgs = {
        honeygain: `This will remove the ${labels[project]} container, image, and your saved credentials. You'll need to re-enter your email and password to use Honeygain again.`,
        urnetwork: `This will remove ${labels[project]}'s stored identity and the pulled image. ${labels[project]} will generate a new identity if re-enabled — any accumulated history tied to the current identity will be lost.`,
        myst: `This will remove ${labels[project]}'s wallet, identity, and the pulled image. A new identity will be generated if re-enabled — any accumulated reputation will be lost.`,
        anyone: `This will remove the relay's data and pulled image, but keep your saved nickname/contact settings. The relay will get a new identity if re-enabled — its accumulated reputation will be lost.`,
      };
      depinUninstall(project, labels[project], msgs[project]);
      return;
    }
  });

  // DePIN — enable/disable toggles (delegated)
  document.getElementById('depin-grid').addEventListener('change', function(e) {
    const toggle = e.target.closest('.depin-toggle');
    if (toggle) { depinToggle(toggle.dataset.project, toggle.checked); return; }
    const autoToggle = e.target.closest('.depin-auto-toggle');
    if (autoToggle) { depinAutoUpdate(autoToggle.dataset.project, autoToggle.checked); return; }
  });

  // DePIN — update buttons (delegated)
  document.getElementById('depin-grid').addEventListener('click', function(e) {
    const updateBtn = e.target.closest('.depin-update-btn');
    if (updateBtn) depinUpdateProject(updateBtn.dataset.project);
  });
}

// ── Init ──────────────────────────────────────────────────────────────────────

function initApp() {
  const savedTab = sessionStorage.getItem('activeTab') || 'dashboard';
  switchTab(savedTab);
}

async function init() {
  wireEvents();

  const stored = localStorage.getItem('gw_token');
  if (!stored) {
    showModal(false);
    return;
  }

  state.token = stored;
  depinState.interval = null;
  depinState.uninstalling = null;
  try {
    await api('/api/identity');
    hideModal();
    startVersionPoll();
    initApp();
  } catch (e) {
    if (e.message === 'unauthorized') {
      showModal(false);
    } else {
      hideModal();
      startVersionPoll();
      initApp();
    }
  }
}

document.addEventListener('DOMContentLoaded', init);
