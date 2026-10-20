'use strict';
/* NekoServer dashboard.
 * Read-only: it shows what the server reports and never changes the server. Commands
 * are only ever shown for you to copy. Settings and favourites stay in this browser.
 * Data: /api/status (collector, every minute), /api/live (hardware, every few
 * seconds) and /api/processes (only while the Processes page is open).
 */

/* ---------- small helpers ---------- */
const $ = id => document.getElementById(id);
const numeric = v => typeof v === 'number' && Number.isFinite(v);
const count = v => numeric(v) && v >= 0 ? v : 0;
const list = v => Array.isArray(v) ? v.filter(r => r && typeof r === 'object') : []; // rows (objects)
const values = v => Array.isArray(v) ? v : []; // plain numbers or words
const object = v => v && typeof v === 'object' && !Array.isArray(v) ? v : {};
const text = (v, fallback = '') => typeof v === 'string' && v ? v : fallback;
const nowSeconds = () => Date.now() / 1000;
const LOCAL = ['127.0.0.1', 'localhost', '[::1]'].includes(location.hostname);

function el(tag, className = '', content) {
  const n = document.createElement(tag);
  if (className) n.className = className;
  if (content !== undefined && content !== null) n.textContent = String(content);
  return n;
}
function put(id, nodes) { $(id).replaceChildren(...nodes); }
function button(label, onClick, className = 'button small secondary') {
  const b = el('button', className, label);
  b.type = 'button';
  b.addEventListener('click', onClick);
  return b;
}
function readStore(key, fallback) {
  try { const raw = localStorage.getItem('neko-' + key); return raw === null ? fallback : JSON.parse(raw); } catch { return fallback; }
}
function saveStore(key, value) {
  try { localStorage.setItem('neko-' + key, JSON.stringify(value)); return true; } catch { return false; }
}

/* ---------- formatting ---------- */
function bytes(v) {
  if (!numeric(v) || v < 0) return '—';
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + '\u00a0' + units[i]; // never split "812 MB"
}
function rate(v) { return numeric(v) ? bytes(v) + '/s' : '—'; }
function pct(v) { return numeric(v) ? v.toFixed(v >= 10 ? 0 : 1) + '%' : '—'; }
function ago(at) {
  if (!numeric(at) || at <= 0) return 'never';
  const d = nowSeconds() - at;
  if (d < -60) return 'in the future (check the server clock)';
  if (d < 60) return 'just now';
  if (d < 3600) return Math.floor(d / 60) + ' min ago';
  if (d < 86400) return Math.floor(d / 3600) + ' h ago';
  return Math.floor(d / 86400) + ' days ago';
}
function duration(s) {
  if (!numeric(s) || s < 0) return '—';
  if (s >= 86400) return Math.floor(s / 86400) + 'd ' + Math.floor(s % 86400 / 3600) + 'h';
  return Math.floor(s / 3600) + 'h ' + Math.floor(s % 3600 / 60) + 'm';
}
function clamp(v, lo = 0, hi = 100) { return Math.max(lo, Math.min(hi, v)); }

/* ---------- vocabulary ---------- */
const CATALOG = {
  jellyfin: {name: 'Jellyfin', description: 'Movies & TV shows', icon: '▶', category: 'everyday', port: 8096, tailnet: 8443},
  immich: {name: 'Immich', description: 'Your photos & memories', icon: '✿', category: 'everyday', port: 2283, tailnet: 8445},
  navidrome: {name: 'Navidrome', description: 'Your music, everywhere', icon: '♫', category: 'everyday', port: 4533, tailnet: 8446},
  nextcloud: {name: 'Nextcloud', description: 'Your files & cloud', icon: '☁', category: 'everyday', port: 8088, tailnet: 8447},
  seerr: {name: 'Seerr', description: 'Find something to watch', icon: '✦', category: 'everyday', port: 5055, tailnet: 8444},
  sonarr: {name: 'Sonarr', description: 'Manage your TV library', icon: '◎', category: 'manage', port: 8989},
  radarr: {name: 'Radarr', description: 'Manage your movie library', icon: '◉', category: 'manage', port: 7878},
  lidarr: {name: 'Lidarr', description: 'Manage your music library', icon: '≋', category: 'manage', port: 8686},
  prowlarr: {name: 'Prowlarr', description: 'Manage search providers', icon: '◇', category: 'manage', port: 9696},
  qbittorrent: {name: 'qBittorrent', description: 'Check your downloads', icon: '↓', category: 'manage', port: 8080},
  steam: {name: 'Steam Big Picture', description: 'Games on the TV', icon: '◈', category: 'everyday', shortcut: true},
  youtube: {name: 'YouTube', description: 'Watch in your browser', icon: '▶', category: 'everyday', shortcut: true}
};
const JOB_UNITS = {
  archive: 'nekoserver-photo-preservation.service', database: 'nekoserver-immich-db-backup.service',
  external: 'nekoserver-photo-backup.service', applications: 'nekoserver-app-backup.service',
  host: 'nekoserver-host-backup.service', music: 'nekoserver-music-backup.service',
  monthly: 'nekoserver-maintenance.service', maintenance: 'nekoserver-maintenance.service'
};
const LABELS = {
  pass: 'Passed', fail: 'Failed', critical: 'Urgent', warning: 'Review', unknown: 'Not checked yet',
  needs_setup: 'Setup needed', stale: 'Overdue', partial: 'Incomplete', running: 'Running', starting: 'Starting',
  unhealthy: 'Not responding', missing: 'Not installed', exited: 'Stopped', paused: 'Paused', created: 'Not started',
  restarting: 'Restarting', dead: 'Stopped', ambiguous: 'Check setup', disabled: 'Checks off',
  complete: 'All checked', pending: 'Not finished', outdated: 'Needs recheck'
};
const ROUTES = {
  home: 'Home', apps: 'Your apps', backups: 'Backups', hardware: 'Hardware', sensors: 'Sensors',
  processes: 'Processes', health: 'Server health', attention: 'Needs attention', help: 'Help'
};
/* Short, stable error codes. E10-E42 come from the server; E01, E02 and E12 are
 * noticed by this page. DASHBOARD.md lists the same table. */
const ERRORS = {
  E01: ['Can’t reach the server', 'Check that the server is on. Away from home, turn on Tailscale on this device.'],
  E02: ['The server is slow to answer', 'It may be busy (a backup or update). The page keeps trying by itself.'],
  E10: ['Readings not ready yet', 'The server takes its first readings within a minute of starting.'],
  E11: ['Readings could not be read', 'Usually fixes itself within a minute. If not, run setup again.'],
  E12: ['Readings are out of date', 'The reading service stopped. Help shows the command to check it.'],
  E20: ['Live readings unavailable', 'Hardware readings come back by themselves. If not, restart the dashboard.'],
  E40: ['Page not found', 'Use the menu to go back.'],
  E41: ['Open the dashboard from its own address', 'Use the address setup gave you (this computer or Tailscale).'],
  E42: ['Request not supported', 'Reload the page.']
};
class CodedError extends Error {
  constructor(code) { super(ERRORS[code]?.[0] || 'Error'); this.code = ERRORS[code] ? code : 'E01'; }
}
function codeLine(code) { return code + ' · ' + ERRORS[code][0]; }

/* ---------- state ---------- */
const state = {
  route: 'home', status: null, statusError: null, live: null, liveError: null,
  processes: null, processError: null, history: [], issues: [],
  appFilter: 'all', processPage: 0, installPrompt: null
};
const savedFavorites = readStore('favorites', []);
let favorites = new Set((Array.isArray(savedFavorites) ? savedFavorites : []).filter(id => Object.hasOwn(CATALOG, id)));
const notified = new Set();

/* ---------- data ---------- */
async function getJson(path, timeoutMs = 8000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {cache: 'no-store', signal: controller.signal, credentials: 'same-origin'});
    const raw = await response.text();
    if (raw.length > 6_000_000) throw new CodedError('E11');
    let body = null;
    try { body = JSON.parse(raw); } catch { throw new CodedError(response.ok ? 'E11' : 'E01'); }
    if (!response.ok) throw new CodedError(text(body?.error, 'E01'));
    return body;
  } catch (error) {
    if (error instanceof CodedError) throw error;
    throw new CodedError(controller.signal.aborted ? 'E02' : 'E01');
  } finally { clearTimeout(timer); }
}
function validStatus(raw) {
  if (!raw || raw.format !== 'neko-dashboard-v1' || !numeric(raw.generated_at)) throw new CodedError('E11');
  const rows = (v, max = 200) => list(v).slice(0, max);
  return {
    ...raw, version: text(raw.version, 'unknown').slice(0, 40), metrics: object(raw.metrics),
    applications: rows(raw.applications, 20), storage: rows(raw.storage, 20), backups: rows(raw.backups, 30),
    alerts: rows(raw.alerts), integrity: rows(raw.integrity, 20), history: rows(raw.history, 120),
    startup: rows(raw.startup, 40), updates: rows(raw.updates, 20),
    media: {...object(raw.media), checks: rows(raw.media?.checks)}, acceptance: object(raw.acceptance),
    heartbeat: object(raw.heartbeat), notifications: object(raw.notifications), recovery: object(raw.recovery)
  };
}
let loading = {};
async function load(kind) {
  if (loading[kind]) return;
  loading[kind] = true;
  try {
    if (kind === 'status') {
      state.status = validStatus(await getJson('/api/status'));
      state.statusError = null;
    } else if (kind === 'live') {
      state.live = await getJson('/api/live');
      state.liveError = null;
      remember(state.live);
    } else {
      state.processes = await getJson('/api/processes');
      state.processError = null;
    }
  } catch (error) {
    state[kind === 'status' ? 'statusError' : kind === 'live' ? 'liveError' : 'processError'] = error.code || 'E01';
  } finally {
    loading[kind] = false;
    render();
  }
}
/* A few minutes of live readings for the small charts. */
function remember(live) {
  const net = list(live.network).reduce((sum, n) => [sum[0] + count(n.receive_bps), sum[1] + count(n.send_bps)], [0, 0]);
  const disk = list(live.disks).reduce((sum, d) => [sum[0] + count(d.read_bps), sum[1] + count(d.write_bps)], [0, 0]);
  state.history.push({
    at: live.generated_at, cpu: live.cpu?.percent, memory: live.memory?.percent, receive: net[0], send: net[1],
    read: disk[0], write: disk[1], gpus: Object.fromEntries(list(live.gpus).map(g => [g.id, g.percent]))
  });
  if (state.history.length > 90) state.history.shift();
}
function statusStale() {
  const s = state.status;
  return !s || nowSeconds() - s.generated_at > 150 || s.generated_at > nowSeconds() + 60;
}
const LIVE_ROUTES = ['home', 'hardware', 'sensors'];

/* ---------- shared pieces ---------- */
function badge(stateName, label) {
  return el('span', 'badge ' + (Object.hasOwn(LABELS, stateName) ? stateName : 'neutral'), label || LABELS[stateName] || 'Not checked yet');
}
function setBadge(id, stateName, label) { const n = $(id); n.className = 'badge ' + stateName; n.textContent = label; }
function meter(value, level = '') {
  const bar = el('div', 'meter ' + level), fill = el('span');
  fill.style.width = (numeric(value) ? clamp(value) : 0) + '%';
  bar.append(fill);
  return bar;
}
function spark(values, max, className = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 100 30');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('class', 'spark ' + className);
  svg.setAttribute('aria-hidden', 'true');
  const points = values.map((v, i) => [i, v]).filter(p => numeric(p[1]));
  const top = max || Math.max(1, ...points.map(p => p[1]));
  if (points.length > 1) {
    const x = i => values.length > 1 ? i / (values.length - 1) * 100 : 0;
    const y = v => 28 - clamp(v / top * 26, 0, 26);
    const path = document.createElementNS(svg.namespaceURI, 'path');
    path.setAttribute('d', points.map((p, i) => (i ? 'L' : 'M') + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1)).join(' '));
    svg.append(path);
  }
  return svg;
}
function stat(label, value, detail, extra) {
  const card = el('article', 'stat');
  card.append(el('small', '', label), el('strong', 'stat-value', value));
  if (detail) card.append(el('p', '', detail));
  if (extra) card.append(extra);
  return card;
}
function empty(message) { return el('p', 'empty', message); }
function heat(value, sensor = {}) {
  const hot = numeric(sensor.crit) ? sensor.crit - 5 : numeric(sensor.max) ? sensor.max : 85;
  return value >= hot ? 'hot' : value >= hot - 15 ? 'warm' : '';
}

/* ---------- issues ---------- */
function integrityBad(check) { return ['damaged', 'both_damaged', 'disagree', 'missing'].reduce((sum, k) => sum + count(check[k]), 0); }
function appState(app) { return app.health === 'unhealthy' ? 'unhealthy' : app.health === 'starting' ? 'starting' : text(app.state, 'unknown'); }
function serverApps(s) { return list(s?.applications).filter(a => Object.hasOwn(CATALOG, a.id) && !CATALOG[a.id].shortcut); }
function collectIssues(s) {
  const rows = [], seen = new Set();
  const add = (id, title, detail, guide, level = 'warning', unit = null) => {
    if (seen.has(id)) return;
    seen.add(id);
    rows.push({id, title, detail, guide, level, unit});
  };
  const busy = list(s.backups).some(j => ['applications', 'maintenance'].includes(j.id) && j.status === 'running');
  for (const alert of s.alerts) {
    const raw = text(alert.id), id = raw.startsWith('backup:') ? 'job:' + raw.slice(7) : raw;
    let guide = 'general', unit = null;
    if (id.startsWith('integrity:')) guide = 'integrity';
    else if (/^(mount|space|smart):/.test(id)) guide = 'storage';
    else if (id.startsWith('job:')) { guide = 'backup'; unit = JOB_UNITS[id.split(':')[1]] || null; }
    else if (id.startsWith('startup:')) guide = 'startup';
    else if (id.includes('heartbeat')) guide = 'heartbeat';
    else if (id.includes('notif')) guide = 'notifications';
    else if (id.includes('media') || id.startsWith('app:')) guide = 'apps';
    else if (id.includes('update')) guide = 'updates';
    const note = id.startsWith('app:') && busy ? ' A backup or monthly update is running, so this may be expected.' : '';
    add(id || 'alert-' + rows.length, text(alert.title, 'Server needs attention'), text(alert.detail, 'Open the guide to review it.') + note, guide, alert.level === 'critical' ? 'critical' : 'warning', unit);
  }
  for (const app of serverApps(s)) {
    const st = appState(app);
    if (st !== 'running') add('app:' + app.id, CATALOG[app.id].name + ' · ' + (LABELS[st] || 'Status unknown'), 'Check the app. Running is not the same as working: try opening it.', 'apps', ['unhealthy', 'exited', 'dead'].includes(st) ? 'critical' : 'warning');
  }
  for (const disk of s.storage) {
    const name = text(disk.name, 'Drive');
    if (disk.status !== 'mounted') add('mount:' + disk.id, name + ' is not available', 'Check the drive is connected. Never format a drive to make this go away.', 'storage', 'critical');
    else if (count(disk.used_percent) >= 90) add('space:' + disk.id, name + ' is nearly full', pct(disk.used_percent) + ' used. Free some space before the next big copy.', 'storage');
    const smart = object(disk.smart);
    if (['fail', 'warning'].includes(smart.status)) add('smart:' + disk.id, name + ' health needs a look', text(smart.reason, 'Review the drive health report.'), 'storage', smart.status === 'fail' ? 'critical' : 'warning');
  }
  for (const job of s.backups) {
    const name = text(job.name, 'Backup');
    if (['fail', 'partial', 'stale'].includes(job.status)) add('job:' + job.id, name + ' · ' + LABELS[job.status], text(job.reason, 'Review the last backup attempt.'), 'backup', job.status === 'fail' ? 'critical' : 'warning', JOB_UNITS[job.id]);
    else if (job.timer?.installed && !numeric(job.last_success) && job.status !== 'running') add('job:' + job.id, name + ' has not succeeded yet', 'Wait for (or start) its first run before relying on it.', 'backup', 'warning', JOB_UNITS[job.id]);
    if ((job.timer?.installed || numeric(job.last_success)) && !job.timer?.active) add('timer:' + job.id, name + ' schedule is off', 'Its schedule is missing or stopped, so no new copies are made.', 'backup', 'warning', JOB_UNITS[job.id]);
  }
  for (const check of s.integrity) { if (integrityBad(check)) add('integrity:' + check.id, text(check.name, 'Backup copy') + ' has damaged or missing files', integrityBad(check) + ' files need a look. Keep every copy and use the repair guide.', 'integrity', 'critical'); }
  if (s.acceptance?.status !== 'complete') add('acceptance', 'Finish checking your server', 'A backup is only proven once a restore has been tried. The checklist walks you through it.', 'acceptance');
  if (s.media?.status === 'stale') add('media-report', 'App checks are out of date', 'The last detailed app check is overdue.', 'apps');
  return rows.sort((a, b) => (a.level === 'critical' ? 0 : 1) - (b.level === 'critical' ? 0 : 1));
}

/* ---------- chrome: banner, connection, counts ---------- */
function problem() {
  if (state.statusError) return state.statusError;
  if (state.status && statusStale()) return 'E12';
  if (LIVE_ROUTES.includes(state.route) && state.route !== 'home' && state.liveError) return state.liveError;
  if (state.route === 'processes' && state.processError) return state.processError;
  return null;
}
function renderChrome() {
  const code = problem(), s = state.status;
  $('problem').hidden = !code;
  if (code) {
    $('problem-title').textContent = codeLine(code) + '. ';
    $('problem-detail').textContent = ERRORS[code][1] + (s && code !== 'E10' ? ' Showing readings from ' + ago(s.generated_at) + '.' : '');
  }
  document.body.classList.toggle('stale', !!s && (statusStale() || !!state.statusError));
  const critical = state.issues.filter(i => i.level === 'critical').length;
  const level = !s ? 'unknown' : code ? 'warning' : critical ? 'critical' : state.issues.length ? 'warning' : 'pass';
  setBadge('connection', !s && !code ? 'unknown' : code ? 'warning' : 'pass', !s && !code ? 'Connecting' : code ? code : 'Connected');
  $('side-dot').className = 'dot ' + level;
  $('side-state').textContent = !s ? (code ? codeLine(code) : 'Connecting') : code ? 'Readings not current' : critical ? 'Needs attention' : state.issues.length ? 'A few things to review' : 'All good';
  if (s) $('version').textContent = 'NekoServer v' + s.version;
  for (const n of document.querySelectorAll('[data-count=issues]')) { n.textContent = s ? state.issues.length : '—'; if (n.classList.contains('tab-count')) n.hidden = !state.issues.length; }
  for (const n of document.querySelectorAll('[data-count=apps]')) { n.textContent = s ? serverApps(s).length : '—'; }
  $('footer-status').textContent = s ? 'Readings from ' + ago(s.generated_at) : 'Waiting for readings';
}

/* ---------- home ---------- */
function renderHome() {
  const s = state.status;
  if (!s) return;
  const critical = state.issues.filter(i => i.level === 'critical').length;
  setBadge('overall', statusStale() ? 'unknown' : critical ? 'critical' : state.issues.length ? 'warning' : 'pass',
    statusStale() ? 'Readings not current' : critical ? critical + ' urgent' : state.issues.length ? state.issues.length + ' to review' : 'Everything looks good');
  $('updated').textContent = 'Updated ' + ago(s.generated_at);
  const apps = serverApps(s), running = apps.filter(a => appState(a) === 'running').length;
  $('summary-apps').textContent = running + ' of ' + apps.length + ' running';
  const bad = s.backups.filter(j => ['fail', 'partial', 'stale'].includes(j.status));
  $('summary-backups').textContent = s.backups.some(j => j.status === 'running') ? 'Backup running' : bad.length ? bad.length + ' need a look' : s.backups.length && s.backups.every(j => j.status === 'pass') ? 'All recent copies OK' : 'Not all checked yet';
  const media = s.storage.find(d => d.id === 'media_hdd') || s.storage[0];
  $('summary-storage').textContent = media && numeric(media.total) && numeric(media.used) ? bytes(media.total - media.used) + ' free' : '—';
  $('summary-uptime').textContent = duration(state.live?.cpu?.uptime_seconds ?? s.metrics.uptime_seconds);
  const live = state.live, m = s.metrics, h = state.history;
  const cpu = live?.cpu?.percent ?? m.cpu_percent, memory = live?.memory?.percent ?? m.memory_percent;
  const temps = list(live?.sensors).filter(x => x.kind === 'temperature' && x.group === 'Processor').map(x => x.value);
  const temperature = temps.length ? Math.max(...temps) : m.temperature;
  const net = h.at(-1);
  put('glance', [
    stat('Processor', pct(cpu), text(live?.cpu?.name ?? m.cpu_model, 'Waiting for a second reading'), spark(h.length > 1 ? h.map(r => r.cpu) : list(s.history).map(r => r.cpu), 100)),
    stat('Memory', pct(memory), numeric(live?.memory?.used ?? m.memory_used) ? bytes(live?.memory?.used ?? m.memory_used) + ' of ' + bytes(live?.memory?.total ?? m.memory_total) : 'Waiting for a reading', meter(memory, memory >= 90 ? 'hot' : '')),
    stat('Processor temperature', numeric(temperature) ? Math.round(temperature) + ' °C' : '—', numeric(temperature) ? temperature >= 85 ? 'Running hot: check the fans' : 'Hottest processor sensor' : 'No sensor reported', numeric(temperature) ? meter(temperature, heat(temperature)) : null),
    stat('Network', net ? '↓ ' + rate(net.receive) : '—', net ? '↑ ' + rate(net.send) + ' sent' : 'Waiting for live readings', spark(h.map(r => r.receive), 0, 'alt'))
  ]);
}

/* ---------- apps ---------- */
function appAddress(id) {
  const spec = CATALOG[id];
  if (!spec) return null;
  if (id === 'youtube') return 'https://www.youtube.com/';
  if (id === 'steam') return LOCAL ? 'steam://open/bigpicture' : null;
  if (LOCAL) return 'http://127.0.0.1:' + spec.port + '/';
  if (/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net$/i.test(location.hostname) && spec.tailnet) return 'https://' + location.hostname + ':' + spec.tailnet + '/';
  return null; // a phone's "localhost" is the phone itself: never guess an address
}
function renderApps() {
  const s = state.status;
  const apps = s ? serverApps(s) : [];
  const onHome = state.route === 'home';
  const query = onHome ? '' : $('app-search').value.trim().toLowerCase();
  const extras = [{id: 'youtube', state: 'browser'}, ...(LOCAL ? [{id: 'steam', state: 'desktop'}] : [])];
  let shown = (s ? [...apps, ...extras] : []).filter(a => {
    const spec = CATALOG[a.id], filter = onHome ? 'all' : state.appFilter;
    return (filter === 'all' || (filter === 'favorites' && favorites.has(a.id)) || filter === spec.category)
      && (spec.name + ' ' + spec.description + ' ' + a.id).toLowerCase().includes(query);
  });
  shown.sort((a, b) => Number(favorites.has(b.id)) - Number(favorites.has(a.id)) || Object.keys(CATALOG).indexOf(a.id) - Object.keys(CATALOG).indexOf(b.id));
  if (onHome) shown = shown.filter(a => CATALOG[a.id].category === 'everyday').slice(0, 6);
  const old = statusStale();
  put('apps', shown.length ? shown.map(app => {
    const spec = CATALOG[app.id], card = el('article', 'app-card');
    card.dataset.category = spec.category;
    const top = el('div', 'app-top'), star = button(favorites.has(app.id) ? '★' : '☆', () => {
      if (favorites.has(app.id)) favorites.delete(app.id); else favorites.add(app.id);
      if (!saveStore('favorites', [...favorites])) toast('Saved for this visit only: this browser blocks storage.');
      render();
    }, 'favorite');
    star.setAttribute('aria-pressed', String(favorites.has(app.id)));
    star.setAttribute('aria-label', (favorites.has(app.id) ? 'Remove ' : 'Add ') + spec.name + (favorites.has(app.id) ? ' from favourites' : ' to favourites'));
    top.append(el('span', 'app-icon', spec.icon), star);
    card.append(top, el('div', 'app-name', spec.name), el('div', 'app-description', spec.description));
    if (numeric(app.cpu_percent) || numeric(app.memory_used)) card.append(el('div', 'app-usage', pct(app.cpu_percent) + '\u00a0CPU · ' + bytes(app.memory_used)));
    const bottom = el('div', 'app-bottom'), st = appState(app);
    if (!spec.shortcut) bottom.append(badge(old ? 'unknown' : st, old ? 'Old reading' : LABELS[st]));
    const address = appAddress(app.id);
    if (address) {
      const a = el('a', 'open-link', 'Open ↗');
      a.href = address; a.target = '_blank'; a.rel = 'noopener noreferrer';
      a.setAttribute('aria-label', 'Open ' + spec.name);
      bottom.append(a);
    } else if (!spec.shortcut || app.id === 'steam') {
      bottom.append(button('How to open', () => showGuide('remote'), 'text-button'));
    }
    card.append(bottom);
    return card;
  }) : [empty(!s ? 'Apps appear when the server sends its first reading.' : state.appFilter === 'favorites' ? 'No favourites yet. Tap ☆ on an app to add it.' : 'No matching apps.')]);
  $('app-access-note').textContent = LOCAL
    ? 'Apps open on this computer. Favourites and settings are saved in this browser.'
    : 'Everyday apps open through Tailscale. Management apps open on the server itself.';
}

/* ---------- attention ---------- */
function renderAttention() {
  const s = state.status;
  if (!s) { put('attention-list', [empty('No health snapshot yet.')]); return; }
  const all = state.issues, shown = state.route === 'home' ? all.slice(0, 3) : all;
  if (!shown.length) {
    const calm = el('div', 'calm');
    calm.append(el('span', '', '✓'), el('div', '', 'Nothing needs your attention right now.'));
    put('attention-list', [calm]);
    return;
  }
  put('attention-list', shown.map(issue => {
    const row = el('article', 'issue ' + issue.level), body = el('div', 'issue-body'), actions = el('div', 'issue-actions');
    body.append(el('h3', '', issue.title), el('p', '', issue.detail));
    actions.append(button('What should I do?', () => showGuide(issue.guide, issue.unit)));
    body.append(actions);
    row.append(el('span', 'issue-icon', issue.level === 'critical' ? '!' : '◇'), body, badge(issue.level));
    return row;
  }));
  if (state.route === 'home' && all.length > 3) {
    const more = el('a', 'text-link', 'See ' + (all.length - 3) + ' more →');
    more.href = '#attention';
    $('attention-list').append(more);
  }
}

/* ---------- backups ---------- */
function renderBackups() {
  const s = state.status;
  if (!s) return;
  const jobs = s.backups, checks = s.integrity, fresh = !statusStale();
  const bad = jobs.filter(j => ['fail', 'partial', 'stale'].includes(j.status));
  const unknown = jobs.filter(j => !numeric(j.last_success) || !j.timer?.installed || !j.timer?.active);
  const totalBad = checks.reduce((n, c) => n + integrityBad(c), 0), unverified = checks.reduce((n, c) => n + count(c.unverified), 0);
  const done = s.acceptance.status === 'complete' && fresh;
  const item = (label, st, title, detail) => { const b = el('div', 'protection-item'); b.append(el('small', '', label), badge(st, title), el('p', '', detail)); return b; };
  put('protection', [
    item('RECENT COPIES', !fresh ? 'unknown' : bad.length ? 'warning' : unknown.length || !jobs.length ? 'unknown' : 'pass', !fresh ? 'Old reading' : bad.length ? bad.length + ' need a look' : unknown.length || !jobs.length ? 'Not all checked' : 'All OK', 'Each scheduled backup and whether its last run worked.'),
    item('FILES INTACT', !fresh ? 'unknown' : totalBad ? 'fail' : unverified ? 'unknown' : checks.length ? 'pass' : 'unknown', !fresh ? 'Old reading' : totalBad ? totalBad + ' files' : unverified ? unverified + ' not yet checked' : checks.length ? 'Checksums match' : 'No check yet', 'Every copied file is compared with its SHA-256 checksum.'),
    item('RESTORE TRIED', done ? 'pass' : 'needs_setup', done ? 'Yes' : 'Not yet', done ? 'A restore was tested and recorded.' : 'Try a restore once, so you know it works.')
  ]);
  put('backups', jobs.length ? jobs.map(job => {
    const card = el('article', 'card'), head = el('div', 'card-heading');
    const label = !fresh ? 'Old reading' : !job.timer?.installed && !numeric(job.last_success) ? 'Not set up' : LABELS[job.status];
    head.append(el('h3', '', text(job.name, 'Backup')), badge(fresh ? job.status : 'unknown', label));
    card.append(head, el('p', '', 'Last good copy: ' + ago(job.last_success)),
      el('p', '', job.timer?.next_run ? 'Next run: ' + text(job.timer.next_run) : job.timer?.installed ? 'No next run scheduled' : 'No schedule'));
    if (job.timer?.installed && !job.timer?.active) card.append(el('p', 'reason', 'The schedule is turned off.'));
    if (job.reason) card.append(el('p', 'reason', text(job.reason)));
    card.append(button('Help & logs', () => showGuide('backup', JOB_UNITS[job.id]), 'text-button'));
    return card;
  }) : [empty('No backups are set up yet.')]);
  put('integrity', checks.map(check => {
    const row = el('div', 'row'), body = el('div'), n = integrityBad(check);
    body.append(el('h3', '', text(check.name, 'Checksum check')),
      el('p', '', (n ? n + ' damaged or missing files.' : count(check.unverified) ? count(check.unverified) + ' files not checked yet.' : 'Every recorded file matches.') + ' Checked ' + ago(check.at) + (count(check.repaired) ? ' · ' + count(check.repaired) + ' repaired' : '') + '.'));
    if (n) body.append(button('Repair guide', () => showGuide('integrity'), 'text-button'));
    row.append(body, badge(!fresh ? 'unknown' : n ? 'fail' : count(check.unverified) ? 'warning' : 'pass', !fresh ? 'Old reading' : n ? 'Needs repair' : count(check.unverified) ? 'Partly checked' : 'Matches'));
    return row;
  }));
  const box = el('div', 'callout');
  box.append(el('strong', '', 'Server check: ' + (LABELS[s.acceptance.status] || 'not run yet')),
    el('p', '', s.acceptance.profile ? 'Last run ' + ago(s.acceptance.generated_at) + '.' : 'The checklist confirms apps, backups and a restore really work.'));
  if (s.recovery?.latest_rehearsal) box.append(el('p', '', 'Last test restore: ' + ago(s.recovery.latest_rehearsal.at) + '.'));
  box.append(button('Open the checklist guide', () => showGuide('acceptance'), 'text-button'));
  put('restore-status', [box]);
}

/* ---------- hardware ---------- */
function renderHardware() {
  const live = state.live, h = state.history;
  setBadge('live-state', state.liveError ? 'warning' : live ? 'pass' : 'unknown', state.liveError ? state.liveError : live ? 'Live' : 'Waiting');
  if (!live) { put('load-cards', [empty(state.liveError ? codeLine(state.liveError) : 'Waiting for the first live reading…')]); return; }
  const cpu = object(live.cpu), mem = object(live.memory);
  const cards = [
    stat('Processor', pct(cpu.percent), [text(cpu.name, 'Processor'), numeric(cpu.logical_cpus) ? cpu.logical_cpus + ' threads' : '', numeric(cpu.clock_mhz) ? (cpu.clock_mhz / 1000).toFixed(1) + ' GHz' : ''].filter(Boolean).join(' · '), spark(h.map(r => r.cpu), 100)),
    stat('Memory', pct(mem.percent), bytes(mem.used) + ' of ' + bytes(mem.total) + (count(mem.swap_used) ? ' · swap ' + bytes(mem.swap_used) : ''), spark(h.map(r => r.memory), 100, 'alt'))
  ];
  for (const g of list(live.gpus)) {
    const detail = [text(g.driver), numeric(g.memory_used) ? bytes(g.memory_used) + (numeric(g.memory_total) ? ' of ' + bytes(g.memory_total) : '') + ' video memory' : '', numeric(g.clock_mhz) ? 'clock ' + g.clock_mhz + (numeric(g.clock_max_mhz) ? ' of ' + g.clock_max_mhz : '') + ' MHz' : ''].filter(Boolean).join(' · ');
    const value = numeric(g.percent) ? pct(g.percent) : numeric(g.clock_mhz) && numeric(g.clock_max_mhz) && g.clock_max_mhz > 0 ? Math.round(g.clock_mhz / g.clock_max_mhz * 100) + '% clock' : 'No load reading';
    cards.push(stat(text(g.name, 'Graphics'), value, detail || 'Graphics', numeric(g.percent) ? spark(h.map(r => r.gpus?.[g.id]), 100) : el('p', 'hint', g.source ? text(g.source) : 'This driver doesn’t report a load.')));
  }
  for (const n of list(live.npus)) { cards.push(stat(text(n.name, 'AI accelerator'), numeric(n.percent) ? pct(n.percent) : 'No load reading', 'AI chip · ' + text(n.driver, 'no driver'), numeric(n.percent) ? meter(n.percent) : el('p', 'hint', 'Its driver doesn’t report a load.'))); }
  if (numeric(values(cpu.load)[0])) cards.push(stat('Waiting work', cpu.load[0].toFixed(2), 'Average over 1, 5 and 15 minutes: ' + cpu.load.map(v => v.toFixed(2)).join(' · '), el('p', 'hint', 'Below ' + (cpu.logical_cpus || 1) + ' means the processor keeps up.')));
  put('load-cards', cards);
  put('cpu-cores', values(cpu.cores).length ? cpu.cores.map((v, i) => { const c = el('div', 'core'); c.append(el('span', '', '#' + (i + 1)), el('strong', '', pct(v)), meter(v)); return c; }) : [empty('Core readings appear after the second sample.')]);
  put('network', list(live.network).length ? live.network.map(n => {
    const name = n.kind === 'tailscale' ? 'Tailscale' : n.kind === 'wifi' ? 'Wi-Fi · ' + n.id : 'Wired · ' + n.id;
    return stat(name, '↓ ' + rate(n.receive_bps), '↑ ' + rate(n.send_bps) + ' sent' + (numeric(n.speed_mbps) ? ' · link ' + (n.speed_mbps >= 1000 ? n.speed_mbps / 1000 + ' Gb/s' : n.speed_mbps + ' Mb/s') : '') + (n.up ? '' : ' · disconnected'), el('p', 'hint', bytes(n.received) + ' received since start'));
  }) : [empty('No network card reported.')]);
  put('disk-io', list(live.disks).map(d => stat((d.kind === 'hdd' ? 'Hard drive' : d.kind === 'nvme' ? 'NVMe SSD' : 'SSD') + ' · ' + d.id, numeric(d.busy_percent) ? pct(d.busy_percent) + ' busy' : '—', text(d.model, '') + (numeric(d.size) ? ' ' + bytes(d.size) : ''), el('p', 'hint', 'Reading ' + rate(d.read_bps) + ' · writing ' + rate(d.write_bps)))));
  put('batteries', list(live.power).map(b => stat(b.kind === 'UPS' ? 'UPS' : 'Battery', pct(b.percent), [text(b.name), text(b.status)].filter(Boolean).join(' · '), meter(b.percent, count(b.percent) < 20 ? 'hot' : ''))));
}

/* ---------- sensors ---------- */
function renderSensors() {
  const live = state.live;
  const sensors = list(live?.sensors);
  const temps = sensors.filter(s => s.kind === 'temperature');
  setBadge('sensor-count', live ? 'neutral' : 'unknown', live ? sensors.length + ' found' : state.liveError || 'Waiting');
  if (!live) { put('temperatures', [empty(state.liveError ? codeLine(state.liveError) : 'Looking for sensors…')]); return; }
  const groups = [...new Set(temps.map(t => t.group))];
  put('temperatures', groups.length ? groups.map(group => {
    const rows = temps.filter(t => t.group === group), box = el('article', 'sensor-group');
    const hottest = Math.max(...rows.map(r => r.value));
    const head = el('div', 'card-heading');
    head.append(el('h3', '', group), el('span', 'badge ' + (heat(hottest) === 'hot' ? 'critical' : heat(hottest) === 'warm' ? 'warning' : 'pass'), Math.round(hottest) + ' °C'));
    box.append(head);
    for (const t of rows) {
      const row = el('div', 'sensor-row'), level = heat(t.value, t);
      const limit = numeric(t.crit) ? t.crit : numeric(t.max) ? t.max : 100;
      row.append(el('span', 'sensor-label', text(t.label) + (group === 'Drives' || group === 'Other' ? ' · ' + text(t.chip) : '')), el('strong', 'sensor-value ' + level, Math.round(t.value) + ' °C'), meter(t.value / limit * 100, level));
      box.append(row);
    }
    return box;
  }) : [empty('No temperature sensors reported. A virtual machine or missing driver can hide them (see Help).')]);
  const fans = sensors.filter(s => s.kind === 'fan'), duty = sensors.filter(s => s.kind === 'fan_duty');
  put('fans', fans.length || duty.length ? [
    ...fans.map(f => stat(text(f.label).replace(/^fan/, 'Fan '), f.value > 0 ? Math.round(f.value) + ' RPM' : 'Stopped', text(f.chip), el('p', 'hint', f.value > 0 ? 'Spinning' : 'Not spinning (can be normal at low temperature)'))),
    ...duty.map(d => stat('Fan output ' + text(d.label).replace(/^pwm/, ''), pct(d.value), text(d.chip) + ' · ' + text(d.mode, 'unknown'), meter(d.value)))
  ] : [empty('No fan sensors reported. Many cases connect fans so only the motherboard sees them.')]);
  const power = sensors.filter(s => s.kind === 'power');
  put('power', power.length ? power.map(p => stat(text(p.group) + ' · ' + text(p.label), p.value.toFixed(1) + ' W', text(p.chip))) : [empty('No power readings reported.')]);
  const volts = sensors.filter(s => s.kind === 'voltage');
  $('voltage-count').textContent = volts.length ? '(' + volts.length + ')' : '(none reported)';
  put('voltages', volts.map(v => { const r = el('div', 'row'); r.append(el('span', '', text(v.label) + ' · ' + text(v.chip)), el('strong', '', v.value.toFixed(2) + ' V')); return r; }));
  const rgb = list(object(live.rgb).devices);
  $('rgb-panel').hidden = !rgb.length;
  put('rgb', rgb.map(d => stat(text(d.name, 'Lighting'), text(d.kind, 'Lighting'), text(d.description), el('p', 'hint', values(d.effects).length ? 'Effects: ' + d.effects.slice(0, 8).join(', ') : 'No effects reported'))));
}

/* ---------- processes ---------- */
const PROCESS_STATES = {R: 'running', S: 'waiting', D: 'waiting for disk', Z: 'finished (zombie)', T: 'paused', I: 'idle'};
function renderProcesses() {
  const p = object(state.processes), rows = list(p.rows);
  const query = $('process-search').value.trim().toLowerCase(), sort = $('process-sort').value;
  const appName = r => r.app && CATALOG[r.app] ? CATALOG[r.app].name : '';
  const shown = rows.filter(r => (text(r.name) + ' ' + appName(r) + ' ' + r.pid).toLowerCase().includes(query));
  shown.sort((a, b) => sort === 'name' ? text(a.name).localeCompare(text(b.name)) : sort === 'pid' ? count(a.pid) - count(b.pid) : (numeric(b[sort]) ? b[sort] : -1) - (numeric(a[sort]) ? a[sort] : -1) || count(a.pid) - count(b.pid));
  const pages = Math.max(1, Math.ceil(shown.length / 50));
  state.processPage = Math.min(state.processPage, pages - 1);
  setBadge('process-count', state.processError ? 'warning' : 'neutral', state.processError || (state.processes ? rows.length + ' running' : 'Waiting'));
  const page = shown.slice(state.processPage * 50, (state.processPage + 1) * 50);
  put('process-rows', page.length ? page.map(r => {
    const tr = el('tr');
    const cells = [['Process', text(r.name, '?'), PROCESS_STATES[r.state]], ['App', appName(r) || '—'], ['Processor', pct(r.cpu)], ['Memory', bytes(r.memory)], ['Number', r.pid]];
    for (const [label, value, sub] of cells) {
      const td = el('td', label === 'Process' || label === 'App' ? '' : 'num');
      td.dataset.label = label;
      td.append(el('span', '', value));
      if (sub) td.append(el('small', '', sub));
      tr.append(td);
    }
    return tr;
  }) : [(() => { const tr = el('tr'), td = el('td', 'empty', !state.processes ? (state.processError ? codeLine(state.processError) : 'Loading…') : 'No matching processes.'); td.colSpan = 5; tr.append(td); return tr; })()]);
  $('process-page').textContent = 'Page ' + (state.processPage + 1) + ' of ' + pages;
  $('process-prev').disabled = state.processPage === 0;
  $('process-next').disabled = state.processPage >= pages - 1;
  $('process-note').textContent = '100% processor = one full core, so busy programs can go above 100%.' + (p.truncated ? ' Only the first 5000 are listed.' : '');
}

/* ---------- health ---------- */
function renderHealth() {
  const s = state.status;
  if (!s) return;
  const old = statusStale();
  put('drives', s.storage.length ? s.storage.map(disk => {
    const card = el('article', 'card'), smart = object(disk.smart), mounted = disk.status === 'mounted', head = el('div', 'card-heading');
    head.append(el('h3', '', text(disk.name, 'Drive')), badge(old ? 'unknown' : mounted ? 'pass' : 'fail', old ? 'Old reading' : mounted ? 'Connected' : disk.status === 'wrong_drive' ? 'Wrong drive' : 'Missing'));
    card.append(head, el('p', 'big', numeric(disk.total) && numeric(disk.used) ? bytes(disk.total - disk.used) + ' free' : 'Space unknown'),
      meter(disk.used_percent, count(disk.used_percent) >= 90 ? 'hot' : count(disk.used_percent) >= 80 ? 'warm' : ''),
      el('p', '', bytes(disk.used) + ' used of ' + bytes(disk.total)),
      el('p', smart.status === 'pass' || smart.status === 'unknown' ? '' : 'reason', 'Health: ' + (LABELS[smart.status] || 'Not checked yet') + (numeric(smart.temperature) ? ' · ' + smart.temperature + ' °C' : '') + (smart.checked_at ? ' · checked ' + ago(smart.checked_at) : '')));
    if (smart.reason && smart.status !== 'pass') card.append(el('p', 'hint', text(smart.reason)));
    return card;
  }) : [empty('No drives reported.')]);
  put('startup', s.startup.length ? s.startup.map(u => {
    const row = el('div', 'row'), body = el('div');
    body.append(el('h3', '', text(u.name, u.unit)), el('p', 'mono', text(u.unit)));
    row.append(body, badge(u.enabled ? 'pass' : 'warning', u.enabled ? (u.active === 'active' ? 'Starts by itself · on' : 'Starts by itself') : 'Won’t start after a restart'));
    if (!u.enabled) body.append(button('How to fix', () => showGuide('startup'), 'text-button'));
    return row;
  }) : [empty('The startup check runs with the next reading.')]);
  const monitor = (title, value, guide) => {
    const card = el('article', 'card'), v = object(value), head = el('div', 'card-heading');
    const message = !v.timer?.installed ? 'Not set up. This is optional.' : !v.timer?.active ? 'Its schedule is off.' : v.status === 'pass' ? 'The last check-in worked.' : v.status === 'fail' ? 'The last attempt failed.' : v.status === 'stale' ? 'No recent result.' : 'Set up; waiting for its first result.';
    head.append(el('h3', '', title), badge(old ? 'unknown' : v.status));
    card.append(head, el('p', '', message), button('Set up & check', () => showGuide(guide), 'text-button'));
    return card;
  };
  put('monitoring', [monitor('Outside heartbeat', s.heartbeat, 'heartbeat'), monitor('Phone notifications', s.notifications, 'notifications')]);
  const media = s.media;
  put('readiness', [el('p', 'section-note', media.status === 'disabled' ? 'Detailed app checks are off (the monthly update still checks apps).' : media.status === 'current' ? 'Detailed app checks · ' + (numeric(media.age_seconds) ? ago(nowSeconds() - media.age_seconds) : 'time unknown') : media.status === 'stale' ? 'Detailed app checks are overdue.' : 'No detailed app check yet.'),
    ...media.checks.map(c => { const r = el('div', 'row'); r.append(el('span', '', text(c.name, 'App check')), badge(old || media.status !== 'current' ? 'unknown' : c.status)); return r; })]);
  put('updates', s.updates.length ? [(() => {
    const box = el('div', 'callout');
    box.append(el('strong', '', 'Bigger updates waiting for you'));
    for (const u of s.updates) { box.append(el('p', '', text(u.name, u.app) + ': ' + text(u.current) + ' → ' + text(u.available))); }
    box.append(button('About updates', () => showGuide('updates'), 'text-button'));
    return box;
  })()] : []);
}

/* ---------- help ---------- */
function shellQuote(v) { return "'" + v.replaceAll("'", "'\\''") + "'"; }
function installedScript(name) {
  const root = state.status?.storage.find(d => d.id === 'data_ssd')?.mount, version = state.status?.version;
  return typeof root === 'string' && /^\/[A-Za-z0-9_./-]+$/.test(root) && !root.split('/').includes('..') && /^\d+\.\d+\.\d+$/.test(version || '')
    ? root + '/installer/releases/v' + version + '/' + name : null;
}
function installer(recover = false) { const script = installedScript('install.sh'); return script ? 'sudo bash ' + shellQuote(script) + (recover ? ' --recover' : '') : null; }
const GUIDES = {
  general: {title: 'Find out what needs attention', intro: 'Start with the first urgent item. Keep your files as they are while you look.', steps: ['Check whether a backup or monthly update is running: short app downtime during those is normal.', 'Open Server health for missing drives, full storage or stopped apps.', 'If readings are old, follow “Dashboard readings” in Help.'], unit: 'nekoserver-dashboard-collect.service'},
  collector: {title: 'Dashboard readings are old or missing', intro: 'The page can open even when the reading service has stopped.', steps: ['On the server, open Konsole.', 'Check the reading service and its schedule with the commands below.', 'Make sure the app SSD is connected, then run setup again if it still fails.'], unit: 'nekoserver-dashboard-collect.service', extra: 'systemctl status nekoserver-dashboard-collect.timer --no-pager'},
  dashboard: {title: 'Restart the dashboard', intro: 'Live readings come from the dashboard service itself. Restarting it is safe.', steps: ['On the server, open Konsole.', 'Run the restart command below, then reload this page.'], unit: 'nekoserver-dashboard.service', extra: 'sudo systemctl restart nekoserver-dashboard.service'},
  apps: {title: 'An app is stopped or not responding', intro: 'First check whether a backup or monthly update explains it.', steps: ['Open Backups and look for a running job. Let it finish.', 'Try the app again. On a phone, turn on Tailscale.', 'Still stuck? Look at Docker on the server, or run setup again: it restarts what should be running.'], unit: 'docker.service', extra: 'sudo docker ps -a'},
  startup: {title: 'Make it start by itself again', intro: 'Something is installed but won’t start when the computer is switched on.', steps: ['Run setup again: it switches every NekoServer part back on.', 'Or switch on just that part with “sudo systemctl enable --now <name>” (the name is shown under it).'], unit: null},
  backup: {title: 'Check a backup', intro: 'Keep the copies you have. One failed run doesn’t mean older backups are bad.', steps: ['Check the reason shown, free space, and that the backup drive is connected.', 'Look at the log below. For damaged or missing files, use the repair guide.', 'Fix the cause, then let the next scheduled run go (or run setup again).'], unit: null},
  recovery: {title: 'Get your files back', intro: 'The recovery helper restores into a separate folder first, so nothing is overwritten.', steps: ['Connect the backup drive you want to use.', 'On the server, open Konsole and start the recovery helper below.', 'Pick the backup and a new folder. Check the files before replacing anything.'], recover: true},
  integrity: {title: 'Repair a damaged or missing copy', intro: 'A checksum warning needs a copy that still matches. Never delete the other copies.', steps: ['Connect the USB backup drive if you have one, so both copies are checked.', 'Start the recovery helper below and choose “Repair damaged backup copies”.', 'If no copy matches, keep everything and ask for help rather than guessing.'], recover: true},
  storage: {title: 'Check a drive or free up space', intro: 'Make sure the right drive is connected. A folder at the old place doesn’t mean the drive is there.', steps: ['On the server, list the drives with the commands below.', 'Compare sizes with the drives chosen during setup. Never format a drive to fix this.', 'For low space, remove large files you don’t need. Never delete your only backup.'], extra: 'lsblk -f\nfindmnt\ndf -h'},
  acceptance: {title: 'Check that everything really works', intro: '“Running”, “backed up” and “restore tested” are three different things.', steps: ['Play something in each app, and try your phone if you use one.', 'Run the checker below; it saves a report.', 'Try a test restore with the recovery helper, then record it.'], acceptance: true},
  remote: {title: 'Open your apps on a phone or tablet', intro: 'A phone’s “localhost” is the phone itself, so apps open through Tailscale instead.', steps: ['Install Tailscale on your phone or tablet and sign in to the same account as the server.', 'Open this dashboard at its Tailscale address (setup showed it), then use the app links here.', 'Management apps only open on the server itself.']},
  heartbeat: {title: 'Outside heartbeat', intro: 'An outside service notices when the whole server goes quiet. Optional.', steps: ['Make sure the monitor runs somewhere other than this server.', 'Check the heartbeat service below.', 'Test it once: you should get an alert when it stops, and another when it’s back.'], unit: 'nekoserver-heartbeat.service'},
  notifications: {title: 'Phone notifications', intro: 'Background notifications and this page’s browser alerts are different things.', steps: ['Set up background notifications with setup.', 'Check the service below. Keep private addresses out of screenshots.', 'Browser alerts only work while this page is open.'], unit: 'nekoserver-notifications.service'},
  updates: {title: 'Updates & monthly maintenance', intro: 'Once a month, NekoServer backs up the apps, tests a restore, then updates them.', steps: ['Check its last result below. Apps are briefly offline during the update.', 'If an update was interrupted, let it finish before changing anything else.', 'Big version jumps wait for you to approve them.'], unit: 'nekoserver-maintenance.service'},
  sensors: {title: 'Why a sensor is missing', intro: 'The dashboard finds every sensor Linux reports, automatically.', steps: ['Some motherboards need a driver: for many boards, install the “lm_sensors” package and run “sudo sensors-detect”, then restart.', 'Virtual machines usually have no sensors.', 'NVIDIA graphics need the NVIDIA driver; its readings update once a minute.'], extra: '/usr/local/lib/nekoserver-dashboard/v' + '{version}' + '/nekoserver-runtime hardware'}
};
async function copy(value) {
  try { await navigator.clipboard.writeText(value); toast('Copied. Paste it into Konsole on the server.'); }
  catch { toast('Couldn’t copy: select the command and copy it by hand.'); }
}
function commandBox(command) { const box = el('div', 'command-box'), code = el('code', '', command); box.append(code, button('Copy', () => copy(command))); return box; }
function openDialog(eyebrow, title, children) {
  $('dialog-eyebrow').textContent = eyebrow;
  $('dialog-title').textContent = title;
  put('dialog-content', children);
  if (!$('dialog').open) $('dialog').showModal();
}
function showGuide(id, requestedUnit) {
  const guide = GUIDES[id] || GUIDES.general, nodes = [el('p', '', guide.intro)], steps = el('ol');
  for (const step of guide.steps) { steps.append(el('li', '', step)); }
  nodes.push(steps);
  const unit = Object.values(JOB_UNITS).includes(requestedUnit) ? requestedUnit : guide.unit;
  if (unit) nodes.push(commandBox('systemctl status ' + unit + ' --no-pager\nsudo journalctl -u ' + unit + ' -n 60 --no-pager'));
  if (guide.extra) nodes.push(commandBox(guide.extra.replace('{version}', state.status?.version || '')));
  if (guide.recover) { const cmd = installer(true); nodes.push(cmd ? commandBox(cmd) : el('p', 'guide-note', 'In the saved NekoServer folder on the server, run: sudo bash install.sh --recover')); }
  if (guide.acceptance) { const script = installedScript('acceptance.py'), config = installedScript('config.json'); nodes.push(script && config ? commandBox('sudo python3 ' + shellQuote(script) + ' --config ' + shellQuote(config) + ' --profile full --save') : el('p', 'guide-note', 'In the saved NekoServer folder, run: sudo python3 acceptance.py --profile full --save')); }
  if (id === 'apps' || id === 'startup') { const cmd = installer(); if (cmd) nodes.push(el('p', 'guide-note', 'Run setup again (it may restart apps):'), commandBox(cmd)); }
  nodes.push(el('p', 'guide-note', 'Commands only run when you paste them into Konsole on the server. This page never changes anything.'));
  openDialog('GUIDED HELP', guide.title, nodes);
}
function renderHelp() {
  const cards = [['collector', 'Readings are old or missing', 'Check the service that collects them.'], ['dashboard', 'Live readings stopped', 'Restart the dashboard safely.'], ['recovery', 'Get files back', 'Restore a backup into a separate folder.'], ['apps', 'An app needs a hand', 'Stopped apps, downtime and setup.'], ['remote', 'Use it on your phone', 'Tailscale, app links and logins.'], ['sensors', 'A sensor is missing', 'Why, and how to add one.'], ['acceptance', 'Check it really works', 'Playback, backups and a test restore.'], ['updates', 'About updates', 'Monthly updates and big versions.']];
  put('help-cards', cards.map(([id, title, description]) => { const c = el('article', 'card'); c.append(el('h3', '', title), el('p', '', description), button('Show me how →', () => showGuide(id), 'text-button')); return c; }));
  put('error-codes', Object.entries(ERRORS).map(([code, [title, fix]]) => { const r = el('div', 'row'), b = el('div'); b.append(el('h3', '', title), el('p', '', fix)); r.append(el('span', 'code', code), b); return r; }));
  put('install-help', [installHelp()]);
}
function installHelp() {
  const box = el('div', 'install'), ua = navigator.userAgent;
  const standalone = matchMedia('(display-mode: standalone)').matches || navigator.standalone === true;
  if (standalone) { box.append(el('p', '', 'You’re using the NekoServer app. ✓')); return box; }
  if (state.installPrompt) {
    box.append(el('p', '', 'Add NekoServer to your home screen or app list. It opens in its own window, without browser bars.'),
      button('Install the app', async () => { state.installPrompt.prompt(); await state.installPrompt.userChoice.catch(() => null); state.installPrompt = null; renderHelp(); }, 'button'));
  } else if (/iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1)) {
    box.append(el('p', '', 'On iPhone or iPad: tap Share (□↑) in Safari, then “Add to Home Screen”.'));
  } else if (/Android/.test(ua)) {
    box.append(el('p', '', 'On Android: open the browser menu (⋮) and choose “Install app” or “Add to Home screen”.'));
  } else if (/Firefox/.test(ua)) {
    box.append(el('p', '', 'Firefox can’t install sites as apps. On the server, open “NekoServer Dashboard” from the app menu: it opens in its own window. Elsewhere, bookmark this page or use Chrome, Edge or Safari to install it.'));
  } else {
    box.append(el('p', '', 'In Chrome or Edge, use the install icon in the address bar (or menu → “Install NekoServer”). On the server itself, “NekoServer Dashboard” is in the app menu.'));
  }
  if (!window.isSecureContext) box.append(el('p', 'hint', 'Installing needs the secure Tailscale address (https://…) or this computer’s own address.'));
  return box;
}
function diagnostics() {
  const s = state.status;
  if (!s) { toast('Wait for the first reading.'); return; }
  const group = rows => rows.reduce((out, r) => { const k = Object.hasOwn(LABELS, r.status) ? r.status : 'unknown'; out[k] = (out[k] || 0) + 1; return out; }, {});
  // A deliberate allowlist: counts and versions, never the whole snapshot.
  const summary = {
    format: 'neko-dashboard-support-v2', server_version: /^\d+\.\d+\.\d+$/.test(s.version) ? s.version : 'unknown',
    exported_at: new Date().toISOString(), readings_current: !statusStale(), problem: problem(),
    apps: group(serverApps(s).map(a => ({status: appState(a)}))), backups: group(s.backups),
    drives: {connected: s.storage.filter(d => d.status === 'mounted').length, other: s.storage.filter(d => d.status !== 'mounted').length},
    integrity: {damaged_or_missing: s.integrity.reduce((n, c) => n + integrityBad(c), 0), unchecked: s.integrity.reduce((n, c) => n + count(c.unverified), 0)},
    startup_off: s.startup.filter(u => !u.enabled).map(u => text(u.unit)),
    hardware: state.live ? {sensors: list(state.live.sensors).length, gpus: list(state.live.gpus).length, npus: list(state.live.npus).length, rgb: object(state.live.rgb).status} : null,
    note: 'Counts only. Read before sharing.'
  };
  const blob = new Blob([JSON.stringify(summary, null, 2) + '\n'], {type: 'application/json'}), url = URL.createObjectURL(blob), a = el('a');
  a.href = url; a.download = 'nekoserver-summary-' + new Date().toISOString().slice(0, 10) + '.json';
  document.body.append(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('Summary downloaded. Read it before sharing.');
}

/* ---------- settings ---------- */
function applyPreferences() {
  let theme = readStore('theme', 'dark');
  if (!['light', 'dark', 'system'].includes(theme)) theme = 'dark';
  document.documentElement.dataset.theme = theme === 'system' ? (matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light') : theme;
  const size = readStore('size', 'normal');
  document.body.classList.toggle('large', size === 'large');
  document.body.classList.toggle('tv', size === 'tv');
}
function choice(label, key, options, fallback) {
  const wrap = el('label', 'field', label), select = el('select');
  for (const [value, name] of options) { const o = el('option', '', name); o.value = value; select.append(o); }
  select.value = readStore(key, fallback);
  select.addEventListener('change', () => { saveStore(key, select.value); applyPreferences(); });
  wrap.append(select);
  return wrap;
}
function preferences() {
  const nodes = [el('p', '', 'Saved in this browser only. Nothing on the server changes.'),
    choice('Appearance', 'theme', [['dark', 'Neko night'], ['light', 'Soft daylight'], ['system', 'Same as this device']], 'dark'),
    choice('Text size', 'size', [['normal', 'Normal'], ['large', 'Large'], ['tv', 'TV (across the room)']], 'normal')];
  const on = 'Notification' in window && Notification.permission === 'granted' && readStore('notify', false) === true;
  nodes.push(el('p', '', 'Browser alerts tell you about urgent problems while this page is open on this device.'),
    button(on ? 'Turn off browser alerts' : 'Turn on browser alerts', async () => {
      if (readStore('notify', false)) { saveStore('notify', false); preferences(); return; }
      if (!('Notification' in window)) { toast('This browser can’t show alerts.'); return; }
      const permission = await Notification.requestPermission().catch(() => 'denied');
      if (permission === 'granted') { saveStore('notify', true); toast('Alerts on while this page is open.'); preferences(); } else toast('Alerts were not allowed.');
    }), el('h3', 'sub-head', 'Use it like an app'), installHelp());
  openDialog('SETTINGS', 'Make yourself at home', nodes);
}
function notify() {
  if (statusStale() || !('Notification' in window) || Notification.permission !== 'granted' || readStore('notify', false) !== true) return;
  const urgent = state.issues.filter(i => i.level === 'critical');
  for (const id of [...notified]) { if (!urgent.some(i => i.id === id)) notified.delete(id); }
  for (const issue of urgent.slice(0, 3)) {
    if (notified.has(issue.id)) continue;
    notified.add(issue.id);
    try { new Notification('NekoServer · ' + issue.title, {body: 'Open the dashboard for what to do.', tag: 'neko-' + issue.id}); } catch { /* optional */ }
  }
}
let toastTimer;
function toast(message) { clearTimeout(toastTimer); $('toast').hidden = false; $('toast').textContent = message; toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500); }
function moreMenu() {
  const nav = el('nav', 'more-list');
  nav.setAttribute('aria-label', 'All pages');
  for (const [id, name] of Object.entries(ROUTES)) {
    const a = el('a', 'nav' + (id === state.route ? ' active' : ''), name);
    a.href = '#' + id;
    a.addEventListener('click', () => $('dialog').close());
    nav.append(a);
  }
  openDialog('NEKOSERVER', 'All pages', [nav, button('Settings', () => preferences())]);
}

/* ---------- routing & rendering ---------- */
function route() {
  const hash = location.hash.slice(1);
  if (hash === 'main') return;
  if (hash === 'help-errors') { state.route = 'help'; } else state.route = Object.hasOwn(ROUTES, hash) ? hash : 'home';
  $('page-title').textContent = ROUTES[state.route];
  document.title = 'NekoServer · ' + ROUTES[state.route];
  for (const a of document.querySelectorAll('[data-route]')) {
    const active = a.dataset.route === state.route;
    a.classList.toggle('active', active);
    if (active) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current');
  }
  for (const n of document.querySelectorAll('[data-page]')) { n.hidden = !n.dataset.page.split(' ').includes(state.route); }
  for (const n of document.querySelectorAll('[data-only]')) { n.hidden = n.dataset.only !== state.route; }
  if (hash === 'help-errors') requestAnimationFrame(() => $('help-errors').scrollIntoView()); else window.scrollTo({top: 0, behavior: 'instant'});
  render();
  poll(true);
}
function render() {
  if (state.status) state.issues = collectIssues(state.status);
  renderChrome();
  const r = state.route;
  if (r === 'home') { renderHome(); renderAttention(); renderApps(); }
  else if (r === 'apps') renderApps();
  else if (r === 'attention') renderAttention();
  else if (r === 'backups') renderBackups();
  else if (r === 'hardware') renderHardware();
  else if (r === 'sensors') renderSensors();
  else if (r === 'processes') renderProcesses();
  else if (r === 'health') renderHealth();
  else if (r === 'help') renderHelp();
  notify();
}

/* ---------- polling ---------- */
const timers = {};
function every(kind, ms, wanted) {
  clearInterval(timers[kind]);
  if (!wanted) return;
  void load(kind);
  timers[kind] = setInterval(() => { if (!document.hidden) void load(kind); }, ms);
}
function poll(routeChanged = false) {
  if (!routeChanged && timers.status) return;
  every('status', 15000, true);
  every('live', 4000, LIVE_ROUTES.includes(state.route));
  every('processes', 5000, state.route === 'processes');
}

/* ---------- start ---------- */
window.addEventListener('hashchange', route);
$('app-search').addEventListener('input', renderApps);
for (const b of document.querySelectorAll('[data-filter]')) b.addEventListener('click', () => {
  state.appFilter = b.dataset.filter;
  for (const n of document.querySelectorAll('[data-filter]')) { n.setAttribute('aria-pressed', String(n === b)); }
  renderApps();
});
for (const b of document.querySelectorAll('[data-guide]')) { b.addEventListener('click', () => showGuide(b.dataset.guide)); }
$('refresh').addEventListener('click', () => { void load('status'); if (LIVE_ROUTES.includes(state.route)) void load('live'); if (state.route === 'processes') void load('processes'); });
$('preferences').addEventListener('click', preferences);
$('more').addEventListener('click', moreMenu);
$('export').addEventListener('click', diagnostics);
$('close-dialog').addEventListener('click', () => $('dialog').close());
$('dialog').addEventListener('click', e => { if (e.target === $('dialog')) $('dialog').close(); });
$('process-search').addEventListener('input', () => { state.processPage = 0; renderProcesses(); });
$('process-sort').addEventListener('change', () => { state.processPage = 0; renderProcesses(); });
$('process-prev').addEventListener('click', () => { state.processPage--; renderProcesses(); });
$('process-next').addEventListener('click', () => { state.processPage++; renderProcesses(); });
$('problem-link').addEventListener('click', e => { e.preventDefault(); location.hash = 'help-errors'; });
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyPreferences);
document.addEventListener('visibilitychange', () => { if (!document.hidden) { void load('status'); if (LIVE_ROUTES.includes(state.route)) void load('live'); } });
window.addEventListener('beforeinstallprompt', e => { e.preventDefault(); state.installPrompt = e; if (state.route === 'help') renderHelp(); });
if ('serviceWorker' in navigator && window.isSecureContext) navigator.serviceWorker.register('sw.js').catch(() => { /* works without it */ });
setInterval(() => { if (!document.hidden) renderChrome(); }, 30000); // "x min ago" and staleness move on their own

applyPreferences();
route();
