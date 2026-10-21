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

/* The value of the first rule whose condition holds, else the fallback: a readable
 * replacement for chains of "a ? b : c ? d : e". Rules: [[condition, value], ...]. */
function firstOf(rules, fallback) {
  for (const [condition, value] of rules) {
    if (condition) { return value; }
  }
  return fallback;
}
function el(tag, className, content) {
  const n = document.createElement(tag);
  if (className) { n.className = className; }
  if (content !== undefined && content !== null) { n.textContent = String(content); }
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
function joined(parts) { return parts.filter(Boolean).join(' · '); }

/* ---------- formatting ---------- */
function bytes(v) {
  if (!numeric(v) || v < 0) { return '—'; }
  const units = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let i = 0;
  while (v >= 1000 && i < units.length - 1) { v /= 1000; i++; }
  return v.toFixed(v >= 100 || i === 0 ? 0 : 1) + ' ' + units[i]; // never split "812 MB"
}
function rate(v) { return numeric(v) ? bytes(v) + '/s' : '—'; }
function pct(v) {
  if (!numeric(v)) { return '—'; }
  return v.toFixed(v >= 10 ? 0 : 1) + '%';
}
function ago(at) {
  if (!numeric(at) || at <= 0) { return 'never'; }
  const d = nowSeconds() - at;
  if (d < -60) { return 'in the future (check the server clock)'; }
  if (d < 60) { return 'just now'; }
  if (d < 3600) { return Math.floor(d / 60) + ' min ago'; }
  if (d < 86400) { return Math.floor(d / 3600) + ' h ago'; }
  return Math.floor(d / 86400) + ' days ago';
}
function duration(s) {
  if (!numeric(s) || s < 0) { return '—'; }
  if (s >= 86400) { return Math.floor(s / 86400) + 'd ' + Math.floor(s % 86400 / 3600) + 'h'; }
  return Math.floor(s / 3600) + 'h ' + Math.floor(s % 3600 / 60) + 'm';
}
function clamp(v, lo = 0, hi = 100) { return Math.max(lo, Math.min(hi, v)); }
function linkSpeed(mbps) { return mbps >= 1000 ? mbps / 1000 + ' Gb/s' : mbps + ' Mb/s'; }

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
  home: 'Home', apps: 'Your apps', backups: 'Backups', hardware: 'Hardware', sensors: 'Sensors', cooling: 'Fans & lights',
  processes: 'Processes', health: 'Server health', attention: 'Needs attention', help: 'Help'
};
/* Pages that show live hardware readings (polled every few seconds). */
const LIVE_ROUTES = new Set(['home', 'hardware', 'sensors']);
/* Short, stable error codes. E10-E42 come from the server; E01, E02 and E12 are
 * noticed by this page. DASHBOARD.md lists the same table. */
const ERRORS = {
  E01: ['Can’t reach the server', 'Check that the server is on. Away from home, turn on Tailscale on this device.'],
  E02: ['The server is slow to answer', 'It may be busy (a backup or update). The page keeps trying by itself.'],
  E10: ['Readings not ready yet', 'The server takes its first readings within a minute of starting.'],
  E11: ['Readings could not be read', 'Usually fixes itself within a minute. If not, run setup again.'],
  E12: ['Readings are out of date', 'The reading service stopped. Help shows the command to check it.'],
  E20: ['Live readings unavailable', 'Hardware readings come back by themselves. If not, restart the dashboard.'],
  E21: ['Fan and light control unavailable', 'The control service is not running. Help shows the command to check it.'],
  E40: ['Page not found', 'Use the menu to go back.'],
  E41: ['Open the dashboard from its own address', 'Use the address setup gave you (this computer or Tailscale).'],
  E42: ['Request not supported', 'Reload the page.'],
  E43: ['Enter your PIN to change this', 'Phones, tablets and other computers need the PIN chosen during setup.'],
  E44: ['Wrong PIN', 'Check the PIN and try again.'],
  E45: ['Too many tries; wait 5 minutes', 'After five wrong PINs, changes stay locked for 5 minutes.'],
  E46: ['The change could not be saved', 'Nothing was changed. The server’s system drive may be full or failing: Help → “Fans don’t change”.']
};
class CodedError extends Error {
  constructor(code) { super(ERRORS[code]?.[0] || 'Error'); this.code = ERRORS[code] ? code : 'E01'; }
}
function codeLine(code) { return code + ' · ' + ERRORS[code][0]; }

/* ---------- state ---------- */
const state = {
  route: 'home', status: null, statusError: null, live: null, liveError: null,
  processes: null, processError: null, control: null, controlError: null, history: [], issues: [],
  appFilter: 'all', processPage: 0, installPrompt: null
};
const savedFavorites = readStore('favorites', []);
const favorites = new Set((Array.isArray(savedFavorites) ? savedFavorites : []).filter(id => Object.hasOwn(CATALOG, id)));
const notified = new Set();

/* ---------- data ---------- */
async function getJson(path, timeoutMs = 8000, headers = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(path, {cache: 'no-store', signal: controller.signal, credentials: 'same-origin', headers});
    const raw = await response.text();
    if (raw.length > 6_000_000) { throw new CodedError('E11'); }
    let body = null;
    try { body = JSON.parse(raw); } catch { throw new CodedError(response.ok ? 'E11' : 'E01'); }
    if (!response.ok) { throw new CodedError(text(body?.error, 'E01')); }
    return body;
  } catch (error) {
    if (error instanceof CodedError) { throw error; }
    throw new CodedError(controller.signal.aborted ? 'E02' : 'E01');
  } finally { clearTimeout(timer); }
}
/* The collector lists every kind of backup NekoServer can make; one this server
 * doesn't use is only a placeholder. A backup counts once its schedule is installed
 * (even if switched off or broken) or it has ever run. */
function backupInUse(job) {
  return job.timer?.installed === true || numeric(job.last_success) || numeric(job.last_attempt) || ['running', 'pass', 'fail', 'partial', 'stale'].includes(job.status);
}
function validStatus(raw) {
  if (raw?.format !== 'neko-dashboard-v1' || !numeric(raw.generated_at)) { throw new CodedError('E11'); }
  const rows = (v, max = 200) => list(v).slice(0, max);
  return {
    ...raw, version: text(raw.version, 'unknown').slice(0, 40), metrics: object(raw.metrics),
    applications: rows(raw.applications, 20), storage: rows(raw.storage, 20), backups: rows(raw.backups, 30).filter(backupInUse),
    alerts: rows(raw.alerts), integrity: rows(raw.integrity, 20), history: rows(raw.history, 120),
    startup: rows(raw.startup, 40), updates: rows(raw.updates, 20),
    media: {...object(raw.media), checks: rows(raw.media?.checks)}, acceptance: object(raw.acceptance),
    heartbeat: object(raw.heartbeat), notifications: object(raw.notifications), recovery: object(raw.recovery)
  };
}
const LOADERS = {
  status: async () => { state.status = validStatus(await getJson('/api/status')); },
  live: async () => { state.live = await getJson('/api/live'); remember(state.live); },
  processes: async () => { state.processes = await getJson('/api/processes'); },
  control: async () => { state.control = await getJson('/api/control', 8000, tokenHeader()); }
};
const ERROR_FIELD = {status: 'statusError', live: 'liveError', processes: 'processError', control: 'controlError'};
const loading = {};
async function load(kind) {
  if (loading[kind]) { return; }
  loading[kind] = true;
  try {
    await LOADERS[kind]();
    state[ERROR_FIELD[kind]] = null;
  } catch (error) {
    state[ERROR_FIELD[kind]] = error.code || 'E01';
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
  if (state.history.length > 90) { state.history.shift(); }
}
/* Readings are late after two collection intervals plus 30 s (150 s at the default
 * 60 s). Same rule as reading_max_age in neko_common.py, used by the acceptance check. */
function statusMaxAge(s) {
  const interval = Number.isInteger(s?.collect_interval) && s.collect_interval >= 30 && s.collect_interval <= 300 ? s.collect_interval : 60;
  return 2 * interval + 30;
}
function statusStale() {
  const s = state.status;
  return !s || nowSeconds() - s.generated_at > statusMaxAge(s) || s.generated_at > nowSeconds() + 60;
}
/* Live readings are current only while their feed works and the last one is recent. */
function liveFresh() {
  const live = state.live;
  return !!live && !state.liveError && numeric(live.generated_at) && Math.abs(nowSeconds() - live.generated_at) <= 30;
}
/* The live readings to show as current, or null (the page then says what it shows). */
function currentLive() { return liveFresh() ? state.live : null; }
function criticalCount() { return state.issues.filter(i => i.level === 'critical').length; }

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
function spark(series, max, className = '') {
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 100 30');
  svg.setAttribute('preserveAspectRatio', 'none');
  svg.setAttribute('class', 'spark ' + className);
  svg.setAttribute('aria-hidden', 'true');
  const points = series.map((v, i) => [i, v]).filter(p => numeric(p[1]));
  if (points.length < 2) { return svg; }
  const top = max || Math.max(1, ...points.map(p => p[1]));
  const x = i => i / (series.length - 1) * 100;
  const y = v => 28 - clamp(v / top * 26, 0, 26);
  const path = document.createElementNS(svg.namespaceURI, 'path');
  path.setAttribute('d', points.map((p, i) => (i ? 'L' : 'M') + x(p[0]).toFixed(1) + ' ' + y(p[1]).toFixed(1)).join(' '));
  svg.append(path);
  return svg;
}
function stat(label, value, detail, extra) {
  const card = el('article', 'stat');
  card.append(el('small', '', label), el('strong', 'stat-value', value));
  if (detail) { card.append(el('p', '', detail)); }
  if (extra) { card.append(extra); }
  return card;
}
function hint(message) { return el('p', 'hint', message); }
/* A stable name for a control that is rebuilt on refresh, so keyboard and controller
 * focus can return to it (see withFocus). */
function keyed(node, key) { node.dataset.focus = key; return node; }
function withFocus(draw) {
  const key = document.activeElement?.dataset?.focus;
  draw();
  if (!key) { return; }
  let again = null;
  for (const n of document.querySelectorAll('[data-focus]')) {
    if (n.dataset.focus === key) { again = n; break; }
  }
  again?.focus({preventScroll: true});
}
function empty(message) { return el('p', 'empty', message); }
function cardHeading(title, badgeNode) {
  const head = el('div', 'card-heading');
  head.append(el('h3', '', title), badgeNode);
  return head;
}
/* Temperature level: within 5 °C of the sensor's critical limit (or at its maximum,
 * or 85 °C when it reports neither) is hot; 15 °C below that is warm. */
function hotAt(sensor) { return firstOf([[numeric(sensor.crit), sensor.crit - 5], [numeric(sensor.max), sensor.max]], 85); }
function heat(value, sensor = {}) {
  const hot = hotAt(sensor);
  return firstOf([[value >= hot, 'hot'], [value >= hot - 15, 'warm']], '');
}
const HEAT_BADGE = {hot: 'critical', warm: 'warning', '': 'pass'};

/* ---------- issues ---------- */
function integrityBad(check) { return ['damaged', 'both_damaged', 'disagree', 'missing'].reduce((sum, k) => sum + count(check[k]), 0); }
function appState(app) {
  if (app.health === 'unhealthy' || app.health === 'starting') { return app.health; }
  return text(app.state, 'unknown');
}
function serverApps(s) { return list(s?.applications).filter(a => Object.hasOwn(CATALOG, a.id) && !CATALOG[a.id].shortcut); }
const severity = urgent => urgent ? 'critical' : 'warning';
/* Which guide explains an alert, by its id. */
const ALERT_GUIDES = [
  [id => id.startsWith('integrity:'), 'integrity'],
  [id => /^(mount|space|smart):/.test(id), 'storage'],
  [id => id.startsWith('job:'), 'backup'],
  [id => id.startsWith('startup:'), 'startup'],
  [id => id.includes('heartbeat'), 'heartbeat'],
  [id => id.includes('notif'), 'notifications'],
  [id => id.includes('media') || id.startsWith('app:'), 'apps'],
  [id => id.includes('update'), 'updates']
];
function guideFor(id) { return ALERT_GUIDES.find(([matches]) => matches(id))?.[1] || 'general'; }
/* The service an alert is about, for the guide's log commands. */
function unitFor(id) {
  const [kind, name] = id.split(':');
  if (kind === 'job') { return JOB_UNITS[name] || null; }
  return kind === 'startup' ? name : null;
}
function issueCollector() {
  const rows = [], seen = new Set();
  const add = (id, title, detail, guide, level = 'warning', unit = null) => {
    if (seen.has(id)) { return; }
    seen.add(id);
    rows.push({id, title, detail, guide, level, unit});
  };
  return {rows, add};
}
function alertIssues(s, add) {
  const busy = s.backups.some(j => ['applications', 'maintenance'].includes(j.id) && j.status === 'running');
  for (const alert of s.alerts) {
    const raw = text(alert.id), id = raw.startsWith('backup:') ? 'job:' + raw.slice(7) : raw;
    const unit = unitFor(id);
    const note = id.startsWith('app:') && busy ? ' A backup or monthly update is running, so this may be expected.' : '';
    add(id || 'alert-' + s.alerts.indexOf(alert), text(alert.title, 'Server needs attention'), text(alert.detail, 'Open the guide to review it.') + note, guideFor(id), severity(alert.level === 'critical'), unit);
  }
}
function appIssues(s, add) {
  for (const app of serverApps(s)) {
    const st = appState(app);
    if (st !== 'running') {
      add('app:' + app.id, CATALOG[app.id].name + ' · ' + (LABELS[st] || 'Status unknown'), 'Check the app. Running is not the same as working: try opening it.', 'apps', severity(['unhealthy', 'exited', 'dead'].includes(st)));
    }
  }
}
function driveIssues(s, add) {
  for (const disk of s.storage) {
    const name = text(disk.name, 'Drive'), smart = object(disk.smart);
    if (disk.status !== 'mounted') {
      add('mount:' + disk.id, name + ' is not available', 'Check the drive is connected. Never format a drive to make this go away.', 'storage', 'critical');
    } else if (count(disk.used_percent) >= 90) {
      add('space:' + disk.id, name + ' is nearly full', pct(disk.used_percent) + ' used. Free some space before the next big copy.', 'storage');
    }
    if (['fail', 'warning'].includes(smart.status)) {
      add('smart:' + disk.id, name + ' health needs a look', text(smart.reason, 'Review the drive health report.'), 'storage', severity(smart.status === 'fail'));
    }
  }
}
function backupIssues(s, add) {
  for (const job of s.backups) {
    const name = text(job.name, 'Backup'), unit = JOB_UNITS[job.id], timer = object(job.timer);
    if (['fail', 'partial', 'stale'].includes(job.status)) {
      add('job:' + job.id, name + ' · ' + LABELS[job.status], text(job.reason, 'Review the last backup attempt.'), 'backup', severity(job.status === 'fail'), unit);
    } else if (timer.installed && !numeric(job.last_success) && job.status !== 'running') {
      add('job:' + job.id, name + ' has not succeeded yet', 'Wait for (or start) its first run before relying on it.', 'backup', 'warning', unit);
    }
    if ((timer.installed || numeric(job.last_success)) && !timer.active) {
      add('timer:' + job.id, name + ' schedule is off', 'Its schedule is missing or stopped, so no new copies are made.', 'backup', 'warning', unit);
    }
  }
}
function otherIssues(s, add) {
  for (const check of s.integrity.filter(c => integrityBad(c))) {
    add('integrity:' + check.id, text(check.name, 'Backup copy') + ' has damaged or missing files', integrityBad(check) + ' files need a look. Keep every copy and use the repair guide.', 'integrity', 'critical');
  }
  if (s.acceptance?.status !== 'complete') { add('acceptance', 'Finish checking your server', 'A backup is only proven once a restore has been tried. The checklist walks you through it.', 'acceptance'); }
  if (s.media?.status === 'stale') { add('media-report', 'App checks are out of date', 'The last detailed app check is overdue.', 'apps'); }
}
/* Everything to look at, most urgent first: the server's own alerts plus what this
 * page can see in the readings (never dismissing a server alert). */
function collectIssues(s) {
  const {rows, add} = issueCollector();
  for (const source of [alertIssues, appIssues, driveIssues, backupIssues, otherIssues]) { source(s, add); }
  const rank = issue => issue.level === 'critical' ? 0 : 1;
  return rows.sort((a, b) => rank(a) - rank(b));
}

/* ---------- chrome: banner, connection, counts ---------- */
function problem() {
  if (state.statusError) { return state.statusError; }
  if (state.status && statusStale()) { return 'E12'; }
  if (LIVE_ROUTES.has(state.route) && state.liveError) { return state.liveError; }
  if (state.route === 'processes' && state.processError) { return state.processError; }
  if (state.route === 'cooling' && state.controlError) { return state.controlError; }
  return null;
}
function renderProblem(code, s) {
  $('problem').hidden = !code;
  if (!code) { return; }
  const since = s && code !== 'E10' ? ' Showing readings from ' + ago(s.generated_at) + '.' : '';
  $('problem-title').textContent = codeLine(code) + '. ';
  $('problem-detail').textContent = ERRORS[code][1] + since;
}
/* The server's overall state for the sidebar: [dot level, words]. */
function overall(code, s) {
  if (!s) { return ['unknown', code ? codeLine(code) : 'Connecting']; }
  return firstOf([
    [code, ['warning', 'Readings not current']],
    [criticalCount(), ['critical', 'Needs attention']],
    [state.issues.length, ['warning', 'A few things to review']]
  ], ['pass', 'All good']);
}
function connection(code, s) {
  return firstOf([[code, ['warning', code]], [!s, ['unknown', 'Connecting']]], ['pass', 'Connected']);
}
function renderCounts(s) {
  for (const n of document.querySelectorAll('[data-count=issues]')) {
    n.textContent = s ? state.issues.length : '—';
    if (n.classList.contains('tab-count')) { n.hidden = !state.issues.length; }
  }
  for (const n of document.querySelectorAll('[data-count=apps]')) { n.textContent = s ? serverApps(s).length : '—'; }
}
function renderChrome() {
  const code = problem(), s = state.status;
  renderProblem(code, s);
  document.body.classList.toggle('stale', !!s && (statusStale() || !!state.statusError));
  document.body.classList.toggle('live-stale', !!state.live && !liveFresh());
  // Fan and light readings that could not be refreshed: dimmed, and nothing can be changed.
  document.body.classList.toggle('control-stale', !!state.control && !!state.controlError);
  const [level, words] = overall(code, s);
  setBadge('connection', ...connection(code, s));
  $('side-dot').className = 'dot ' + level;
  $('side-state').textContent = words;
  if (s) { $('version').textContent = 'NekoServer v' + s.version; }
  renderCounts(s);
  $('footer-status').textContent = s ? 'Readings from ' + ago(s.generated_at) : 'Waiting for readings';
}

/* ---------- home ---------- */
function heroStatus() {
  const critical = criticalCount(), issues = state.issues.length;
  return firstOf([
    [statusStale() || state.statusError, ['unknown', 'Readings not current']],
    [critical, ['critical', critical + ' urgent']],
    [issues, ['warning', issues + ' to review']]
  ], ['pass', 'Everything looks good']);
}
function backupSummary(s) {
  const bad = s.backups.filter(j => ['fail', 'partial', 'stale'].includes(j.status)).length;
  return firstOf([
    [s.backups.some(j => j.status === 'running'), 'Backup running'],
    [bad, bad + ' need a look'],
    [s.backups.length && s.backups.every(j => j.status === 'pass'), 'All recent copies OK']
  ], 'Not all checked yet');
}
function freeSpace(disk) {
  return disk && numeric(disk.total) && numeric(disk.used) ? bytes(disk.total - disk.used) + ' free' : '—';
}
function renderSummaries(s) {
  const apps = serverApps(s), running = apps.filter(a => appState(a) === 'running').length;
  $('summary-apps').textContent = running + ' of ' + apps.length + ' running';
  $('summary-backups').textContent = backupSummary(s);
  $('summary-storage').textContent = freeSpace(s.storage.find(d => d.id === 'media_hdd') || s.storage[0]);
  $('summary-uptime').textContent = duration(currentLive()?.cpu?.uptime_seconds ?? s.metrics.uptime_seconds);
}
/* Live readings when the page has them, else the collector's latest minute. */
function glanceProcessor(s) {
  const live = currentLive(), h = live ? state.history : [];
  const series = h.length > 1 ? h.map(r => r.cpu) : s.history.map(r => r.cpu);
  return stat('Processor', pct(live?.cpu?.percent ?? s.metrics.cpu_percent), text(live?.cpu?.name ?? s.metrics.cpu_model, 'Waiting for a second reading'), spark(series, 100));
}
function glanceMemory(s) {
  const live = object(currentLive()?.memory), m = s.metrics;
  const percent = live.percent ?? m.memory_percent, used = live.used ?? m.memory_used;
  const detail = numeric(used) ? bytes(used) + ' of ' + bytes(live.total ?? m.memory_total) : 'Waiting for a reading';
  return stat('Memory', pct(percent), detail, meter(percent, percent >= 90 ? 'hot' : ''));
}
function processorTemperature(s) {
  const temps = list(currentLive()?.sensors).filter(x => x.kind === 'temperature' && x.group === 'Processor').map(x => x.value);
  return temps.length ? Math.max(...temps) : s.metrics.temperature;
}
function glanceTemperature(s) {
  const t = processorTemperature(s);
  if (!numeric(t)) { return stat('Processor temperature', '—', 'No sensor reported'); }
  return stat('Processor temperature', Math.round(t) + ' °C', t >= 85 ? 'Running hot: check the fans' : 'Hottest processor sensor', meter(t, heat(t)));
}
function glanceNetwork() {
  const h = state.history, last = h.at(-1);
  if (!last || !liveFresh()) { return stat('Network', '—', state.liveError ? codeLine(state.liveError) : 'Waiting for live readings', spark([], 0, 'alt')); }
  return stat('Network', '↓ ' + rate(last.receive), '↑ ' + rate(last.send) + ' sent', spark(h.map(r => r.receive), 0, 'alt'));
}
function renderHome() {
  const s = state.status;
  if (!s) { return; }
  setBadge('overall', ...heroStatus());
  $('updated').textContent = 'Updated ' + ago(s.generated_at);
  renderSummaries(s);
  put('glance', [glanceProcessor(s), glanceMemory(s), glanceTemperature(s), glanceNetwork()]);
}

/* ---------- apps ---------- */
function appAddress(id) {
  const spec = CATALOG[id];
  if (!spec) { return null; }
  if (id === 'youtube') { return 'https://www.youtube.com/'; }
  if (id === 'steam') { return LOCAL ? 'steam://open/bigpicture' : null; }
  if (LOCAL) { return 'http://127.0.0.1:' + spec.port + '/'; }
  const tailnet = /^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+ts\.net$/i.test(location.hostname);
  if (tailnet && spec.tailnet) { return 'https://' + location.hostname + ':' + spec.tailnet + '/'; }
  return null; // a phone's "localhost" is the phone itself: never guess an address
}
function shownApps(s, onHome) {
  const extras = [{id: 'youtube'}, ...(LOCAL ? [{id: 'steam'}] : [])];
  const filter = onHome ? 'all' : state.appFilter;
  const query = onHome ? '' : $('app-search').value.trim().toLowerCase();
  const wanted = a => {
    const spec = CATALOG[a.id];
    const inFilter = filter === 'all' || (filter === 'favorites' && favorites.has(a.id)) || filter === spec.category;
    return inFilter && (spec.name + ' ' + spec.description + ' ' + a.id).toLowerCase().includes(query);
  };
  const order = Object.keys(CATALOG);
  const shown = [...serverApps(s), ...extras].filter(wanted)
    .sort((a, b) => Number(favorites.has(b.id)) - Number(favorites.has(a.id)) || order.indexOf(a.id) - order.indexOf(b.id));
  return onHome ? shown.filter(a => CATALOG[a.id].category === 'everyday').slice(0, 6) : shown;
}
function favoriteButton(id, name) {
  const on = favorites.has(id);
  const star = button(on ? '★' : '☆', () => {
    if (favorites.has(id)) { favorites.delete(id); } else { favorites.add(id); }
    if (!saveStore('favorites', [...favorites])) { toast('Saved for this visit only: this browser blocks storage.'); }
    render();
  }, 'favorite');
  keyed(star, 'favorite-' + id);
  star.setAttribute('aria-pressed', String(on));
  star.setAttribute('aria-label', on ? 'Remove ' + name + ' from favourites' : 'Add ' + name + ' to favourites');
  return star;
}
function openControl(app, spec) {
  const address = appAddress(app.id);
  if (address) {
    const a = el('a', 'open-link', 'Open ↗');
    a.href = address;
    a.target = '_blank';
    a.rel = 'noopener noreferrer';
    a.setAttribute('aria-label', 'Open ' + spec.name);
    return keyed(a, 'open-' + app.id);
  }
  // Server apps and Steam explain how to reach them; YouTube always has an address.
  return keyed(button('How to open', () => showGuide('remote'), 'text-button'), 'open-' + app.id);
}
function appCard(app, old) {
  const spec = CATALOG[app.id], card = el('article', 'app-card'), top = el('div', 'app-top'), bottom = el('div', 'app-bottom');
  card.dataset.category = spec.category;
  top.append(el('span', 'app-icon', spec.icon), favoriteButton(app.id, spec.name));
  card.append(top, el('div', 'app-name', spec.name), el('div', 'app-description', spec.description));
  if (numeric(app.cpu_percent) || numeric(app.memory_used)) {
    card.append(el('div', 'app-usage', pct(app.cpu_percent) + ' CPU · ' + bytes(app.memory_used)));
  }
  if (!spec.shortcut) {
    const st = appState(app);
    bottom.append(old ? badge('unknown', 'Old reading') : badge(st, LABELS[st]));
  }
  bottom.append(openControl(app, spec));
  card.append(bottom);
  return card;
}
function noAppsMessage(s) {
  return firstOf([
    [!s, 'Apps appear when the server sends its first reading.'],
    [state.appFilter === 'favorites', 'No favourites yet. Tap ☆ on an app to add it.']
  ], 'No matching apps.');
}
function renderApps() {
  const s = state.status, old = statusStale();
  const shown = s ? shownApps(s, state.route === 'home') : [];
  put('apps', shown.length ? shown.map(app => appCard(app, old)) : [empty(noAppsMessage(s))]);
  $('app-access-note').textContent = LOCAL
    ? 'Apps open on this computer. Favourites and settings are saved in this browser.'
    : 'Everyday apps open through Tailscale. Management apps open on the server itself.';
}

/* ---------- attention ---------- */
function issueRow(issue) {
  const row = el('article', 'issue ' + issue.level), body = el('div', 'issue-body'), actions = el('div', 'issue-actions');
  actions.append(keyed(button('What should I do?', () => showGuide(issue.guide, issue.unit)), 'issue-' + issue.id));
  body.append(el('h3', '', issue.title), el('p', '', issue.detail), actions);
  row.append(el('span', 'issue-icon', issue.level === 'critical' ? '!' : '◇'), body, badge(issue.level));
  return row;
}
function renderAttention() {
  if (!state.status) { put('attention-list', [empty('No health snapshot yet.')]); return; }
  const all = state.issues, onHome = state.route === 'home', shown = onHome ? all.slice(0, 3) : all;
  if (!shown.length) {
    const calm = el('div', 'calm');
    calm.append(el('span', '', '✓'), el('div', '', 'Nothing needs your attention right now.'));
    put('attention-list', [calm]);
    return;
  }
  put('attention-list', shown.map(issueRow));
  if (onHome && all.length > 3) {
    const more = el('a', 'text-link', 'See ' + (all.length - 3) + ' more →');
    more.href = '#attention';
    $('attention-list').append(more);
  }
}

/* ---------- backups ---------- */
function protectionItem(label, [st, title], detail) {
  const box = el('div', 'protection-item');
  box.append(el('small', '', label), badge(st, title), el('p', '', detail));
  return box;
}
function recentCopies(jobs) {
  const bad = jobs.filter(j => ['fail', 'partial', 'stale'].includes(j.status)).length;
  const unchecked = !jobs.length || jobs.some(j => !numeric(j.last_success) || !j.timer?.installed || !j.timer?.active);
  return firstOf([[bad, ['warning', bad + ' need a look']], [unchecked, ['unknown', 'Not all checked']]], ['pass', 'All OK']);
}
/* A checksum result counts only with a believable date, and only while it is recent:
 * the checks run with the backups, at least every two weeks. */
const INTEGRITY_MAX_AGE = 14 * 86400;
function checkDated(check) { return numeric(check.at) && check.at > 0 && check.at <= nowSeconds() + 60; }
function checkOverdue(check) { return nowSeconds() - check.at > INTEGRITY_MAX_AGE; }
function filesIntact(checks) {
  const damaged = checks.reduce((n, c) => n + integrityBad(c), 0), unverified = checks.reduce((n, c) => n + count(c.unverified), 0);
  return firstOf([
    [damaged, ['fail', damaged + ' files']],
    [checks.some(c => !checkDated(c)), ['unknown', 'Check not dated']],
    [checks.some(checkOverdue), ['stale', 'Check overdue']],
    [unverified, ['unknown', unverified + ' not yet checked']],
    [checks.length, ['pass', 'Checksums match']]
  ], ['unknown', 'No check yet']);
}
/* Which witnessed restore test covers each kind of backup (acceptance.py manual tests). */
const RESTORE_TESTS = {
  database: 'independent_restore', archive: 'independent_restore', external: 'independent_restore',
  applications: 'app_restore', host: 'host_restore'
};
const RESTORE_NAMES = {independent_restore: 'photos', app_restore: 'apps', host_restore: 'host & user files'};
/* Restore confidence for the backups this server actually makes: each needs its own
 * witnessed restore test. A completed checklist that doesn't include them proves nothing. */
function restoreTried(s) {
  const needed = [...new Set(s.backups.map(j => RESTORE_TESTS[j.id]).filter(Boolean))];
  if (!needed.length) { return {result: ['unknown', 'Nothing to test'], words: 'No backup that can be restore-tested is set up yet.'}; }
  const trusted = !['unknown', 'outdated'].includes(s.acceptance.status);
  const passed = new Set(trusted ? list(s.acceptance.manual_checks).filter(c => c.status === 'pass').map(c => c.id) : []);
  const tested = needed.filter(id => passed.has(id)), untested = needed.filter(id => !passed.has(id));
  const names = ids => ids.map(id => RESTORE_NAMES[id]).join(', ');
  if (!untested.length) { return {result: ['pass', 'Yes'], words: 'Tested and recorded: ' + names(tested) + '.'}; }
  if (!tested.length) { return {result: ['needs_setup', 'Not yet'], words: 'Not tested yet: ' + names(untested) + '. Try each once, so you know it works.'}; }
  return {result: ['warning', tested.length + ' of ' + needed.length], words: 'Tested: ' + names(tested) + '. Not yet: ' + names(untested) + '.'};
}
function renderProtection(s, fresh) {
  const old = ['unknown', 'Old reading'], restore = restoreTried(s);
  put('protection', [
    protectionItem('RECENT COPIES', fresh ? recentCopies(s.backups) : old, 'Each scheduled backup and whether its last run worked.'),
    protectionItem('FILES INTACT', fresh ? filesIntact(s.integrity) : old, 'Every copied file is compared with its SHA-256 checksum.'),
    protectionItem('RESTORE TRIED', fresh ? restore.result : old, restore.words)
  ]);
}
function backupBadge(job, fresh) {
  if (!fresh) { return badge('unknown', 'Old reading'); }
  const notSetUp = !job.timer?.installed && !numeric(job.last_success);
  return badge(job.status, notSetUp ? 'Not set up' : LABELS[job.status]);
}
function nextRun(timer) {
  return firstOf([[timer.next_run, 'Next run: ' + text(timer.next_run)], [timer.installed, 'No next run scheduled']], 'No schedule');
}
function backupCard(job, fresh) {
  const card = el('article', 'card'), timer = object(job.timer);
  card.append(cardHeading(text(job.name, 'Backup'), backupBadge(job, fresh)), el('p', '', 'Last good copy: ' + ago(job.last_success)), el('p', '', nextRun(timer)));
  if (timer.installed && !timer.active) { card.append(el('p', 'reason', 'The schedule is turned off.')); }
  if (job.reason) { card.append(el('p', 'reason', text(job.reason))); }
  card.append(keyed(button('Help & logs', () => showGuide('backup', JOB_UNITS[job.id]), 'text-button'), 'backup-' + job.id));
  return card;
}
function integrityResult(check) {
  const damaged = integrityBad(check), unverified = count(check.unverified);
  return firstOf([
    [damaged, {state: 'fail', label: 'Needs repair', words: damaged + ' damaged or missing files.'}],
    [!checkDated(check), {state: 'unknown', label: 'Not dated', words: 'This check has no valid date, so its result can’t be relied on.'}],
    [checkOverdue(check), {state: 'stale', label: 'Overdue', words: 'A newer check is overdue; this result may no longer hold.'}],
    [unverified, {state: 'warning', label: 'Partly checked', words: unverified + ' files not checked yet.'}]
  ], {state: 'pass', label: 'Matches', words: 'Every recorded file matched.'});
}
function integrityRow(check, fresh) {
  const row = el('div', 'row'), body = el('div'), result = integrityResult(check);
  const repaired = count(check.repaired) ? ' · ' + count(check.repaired) + ' repaired' : '';
  body.append(el('h3', '', text(check.name, 'Checksum check')), el('p', '', result.words + ' Last recorded check: ' + ago(check.at) + repaired + '.'));
  if (integrityBad(check)) { body.append(keyed(button('Repair guide', () => showGuide('integrity'), 'text-button'), 'repair-' + check.id)); }
  row.append(body, fresh ? badge(result.state, result.label) : badge('unknown', 'Old reading'));
  return row;
}
function restoreBox(s) {
  const box = el('div', 'callout');
  box.append(el('strong', '', 'Server check: ' + (LABELS[s.acceptance.status] || 'not run yet')),
    el('p', '', s.acceptance.profile ? 'Last run ' + ago(s.acceptance.generated_at) + '.' : 'The checklist confirms apps, backups and a restore really work.'));
  if (s.recovery?.latest_rehearsal) { box.append(el('p', '', 'Last test restore: ' + ago(s.recovery.latest_rehearsal.at) + '.')); }
  box.append(button('Open the checklist guide', () => showGuide('acceptance'), 'text-button'));
  return box;
}
function renderBackups() {
  const s = state.status;
  if (!s) { return; }
  const fresh = !statusStale();
  renderProtection(s, fresh);
  put('backups', s.backups.length ? s.backups.map(job => backupCard(job, fresh)) : [empty('No backups are set up yet.')]);
  put('integrity', s.integrity.map(check => integrityRow(check, fresh)));
  put('restore-status', [restoreBox(s)]);
}

/* ---------- hardware ---------- */
function processorCard(cpu) {
  const clock = numeric(cpu.clock_mhz) ? (cpu.clock_mhz / 1000).toFixed(1) + ' GHz' : '';
  const threads = numeric(cpu.logical_cpus) ? cpu.logical_cpus + ' threads' : '';
  return stat('Processor', pct(cpu.percent), joined([text(cpu.name, 'Processor'), threads, clock]), spark(state.history.map(r => r.cpu), 100));
}
function memoryCard(mem) {
  const swap = count(mem.swap_used) ? ' · swap ' + bytes(mem.swap_used) : '';
  return stat('Memory', pct(mem.percent), bytes(mem.used) + ' of ' + bytes(mem.total) + swap, spark(state.history.map(r => r.memory), 100, 'alt'));
}
function gpuDetail(g) {
  const total = numeric(g.memory_total) ? ' of ' + bytes(g.memory_total) : '';
  const memory = numeric(g.memory_used) ? bytes(g.memory_used) + total + ' video memory' : '';
  const max = numeric(g.clock_max_mhz) ? ' of ' + g.clock_max_mhz : '';
  const clock = numeric(g.clock_mhz) ? 'clock ' + g.clock_mhz + max + ' MHz' : '';
  const measured = numeric(g.sampled_at) ? 'measured ' + ago(g.sampled_at) : '';
  return joined([text(g.driver), memory, clock, measured]) || 'Graphics';
}
function gpuValue(g) {
  if (numeric(g.percent)) { return pct(g.percent); }
  const clockKnown = numeric(g.clock_mhz) && numeric(g.clock_max_mhz) && g.clock_max_mhz > 0;
  return clockKnown ? Math.round(g.clock_mhz / g.clock_max_mhz * 100) + '% clock' : 'No load reading';
}
function gpuCard(g) {
  const extra = numeric(g.percent)
    ? spark(state.history.map(r => r.gpus?.[g.id]), 100)
    : hint(text(g.source, 'This driver doesn’t report a load.'));
  return stat(text(g.name, 'Graphics'), gpuValue(g), gpuDetail(g), extra);
}
function npuCard(n) {
  const known = numeric(n.percent);
  return stat(text(n.name, 'AI accelerator'), known ? pct(n.percent) : 'No load reading', 'AI chip · ' + text(n.driver, 'no driver'), known ? meter(n.percent) : hint('Its driver doesn’t report a load.'));
}
function waitingWorkCard(cpu) {
  const load = values(cpu.load);
  if (!numeric(load[0])) { return null; }
  return stat('Waiting work', load[0].toFixed(2), 'Average over 1, 5 and 15 minutes: ' + load.map(v => v.toFixed(2)).join(' · '), hint('Below ' + (cpu.logical_cpus || 1) + ' means the processor keeps up.'));
}
function coreCell(value, index) {
  const cell = el('div', 'core');
  cell.append(el('span', '', '#' + (index + 1)), el('strong', '', pct(value)), meter(value));
  return cell;
}
const NETWORK_KINDS = {tailscale: () => 'Tailscale', wifi: n => 'Wi-Fi · ' + n.id};
function networkCard(n) {
  const name = (NETWORK_KINDS[n.kind] || (x => 'Wired · ' + x.id))(n);
  const link = numeric(n.speed_mbps) ? ' · link ' + linkSpeed(n.speed_mbps) : '';
  return stat(name, '↓ ' + rate(n.receive_bps), '↑ ' + rate(n.send_bps) + ' sent' + link + (n.up ? '' : ' · disconnected'), hint(bytes(n.received) + ' received since start'));
}
const DISK_KINDS = {hdd: 'Hard drive', nvme: 'NVMe SSD'};
function diskCard(d) {
  const size = numeric(d.size) ? ' ' + bytes(d.size) : '';
  return stat((DISK_KINDS[d.kind] || 'SSD') + ' · ' + d.id, numeric(d.busy_percent) ? pct(d.busy_percent) + ' busy' : '—', text(d.model) + size, hint('Reading ' + rate(d.read_bps) + ' · writing ' + rate(d.write_bps)));
}
function batteryCard(b) {
  return stat(b.kind === 'UPS' ? 'UPS' : 'Battery', pct(b.percent), joined([text(b.name), text(b.status)]), meter(b.percent, count(b.percent) < 20 ? 'hot' : ''));
}
function liveBadge() {
  const kept = state.live ? ' · last reading ' + ago(state.live.generated_at) : '';
  return firstOf([
    [state.liveError, ['warning', state.liveError + kept]],
    [state.live && !liveFresh(), ['warning', 'Last reading ' + ago(state.live?.generated_at)]],
    [state.live, ['pass', 'Live']]
  ], ['unknown', 'Waiting']);
}
function renderHardware() {
  const live = state.live;
  setBadge('live-state', ...liveBadge());
  if (!live) { put('load-cards', [empty(state.liveError ? codeLine(state.liveError) : 'Waiting for the first live reading…')]); return; }
  const cpu = object(live.cpu);
  const cards = [processorCard(cpu), memoryCard(object(live.memory)), ...list(live.gpus).map(gpuCard), ...list(live.npus).map(npuCard), waitingWorkCard(cpu)];
  put('load-cards', cards.filter(Boolean));
  const cores = values(cpu.cores);
  put('cpu-cores', cores.length ? cores.map((value, index) => coreCell(value, index)) : [empty('Core readings appear after the second sample.')]);
  const networks = list(live.network);
  put('network', networks.length ? networks.map(networkCard) : [empty('No network card reported.')]);
  put('disk-io', list(live.disks).map(diskCard));
  put('batteries', list(live.power).map(batteryCard));
}

/* ---------- sensors ---------- */
function sensorLimit(t) { return firstOf([[numeric(t.crit), t.crit], [numeric(t.max), t.max]], 100); }
function sensorRow(t, group) {
  const row = el('div', 'sensor-row'), level = heat(t.value, t);
  const label = text(t.label) + (group === 'Drives' || group === 'Other' ? ' · ' + text(t.chip) : '');
  row.append(el('span', 'sensor-label', label), el('strong', 'sensor-value ' + level, Math.round(t.value) + ' °C'), meter(t.value / sensorLimit(t) * 100, level));
  return row;
}
function temperatureGroup(group, temps) {
  const rows = temps.filter(t => t.group === group), box = el('article', 'sensor-group');
  const hottest = Math.max(...rows.map(r => r.value));
  box.append(cardHeading(group, el('span', 'badge ' + HEAT_BADGE[heat(hottest)], Math.round(hottest) + ' °C')));
  for (const t of rows) { box.append(sensorRow(t, group)); }
  return box;
}
function fanCard(f) {
  const spinning = f.value > 0;
  return stat(text(f.label).replace(/^fan/, 'Fan '), spinning ? Math.round(f.value) + ' RPM' : 'Stopped', text(f.chip), hint(spinning ? 'Spinning' : 'Not spinning (can be normal at low temperature)'));
}
function dutyCard(d) {
  return stat('Fan output ' + text(d.label).replace(/^pwm/, ''), pct(d.value), text(d.chip) + ' · ' + text(d.mode, 'unknown'), meter(d.value));
}
function voltageRow(v) {
  const row = el('div', 'row');
  row.append(el('span', '', text(v.label) + ' · ' + text(v.chip)), el('strong', '', v.value.toFixed(2) + ' V'));
  return row;
}
function sensorBadge(live, sensors) {
  if (!live) { return ['unknown', state.liveError || 'Waiting']; }
  return liveFresh() ? ['neutral', sensors.length + ' found'] : ['warning', 'Last reading ' + ago(live.generated_at)];
}
/* Hard drive temperatures come from the collector's SMART check (every 30 minutes,
 * never waking a sleeping drive): reading them live could keep a drive awake. */
function driveTemperatures() {
  return list(state.status?.storage).filter(d => numeric(d.smart?.temperature)).map(d => ({
    kind: 'temperature', group: 'Drives', label: text(d.name, 'Drive'), value: d.smart.temperature,
    chip: 'health check ' + ago(d.smart.checked_at)
  }));
}
function renderSensors() {
  const live = state.live, sensors = list(live?.sensors);
  const of = kind => sensors.filter(s => s.kind === kind);
  setBadge('sensor-count', ...sensorBadge(live, sensors));
  if (!live) { put('temperatures', [empty(state.liveError ? codeLine(state.liveError) : 'Looking for sensors…')]); return; }
  const temps = [...of('temperature'), ...driveTemperatures()], groups = [...new Set(temps.map(t => t.group))];
  put('temperatures', groups.length ? groups.map(g => temperatureGroup(g, temps)) : [empty('No temperature sensors reported. A virtual machine or missing driver can hide them (see Help).')]);
  const fans = [...of('fan').map(fanCard), ...of('fan_duty').map(dutyCard)];
  put('fans', fans.length ? fans : [empty('No fan sensors reported. Many cases connect fans so only the motherboard sees them.')]);
  const power = of('power').map(p => stat(text(p.group) + ' · ' + text(p.label), p.value.toFixed(1) + ' W', text(p.chip)));
  put('power', power.length ? power : [empty('No power readings reported.')]);
  const volts = of('voltage');
  $('voltage-count').textContent = volts.length ? '(' + volts.length + ')' : '(none reported)';
  put('voltages', volts.map(voltageRow));
}

/* ---------- fans & lights ----------
 * Changes go to the root control service through the dashboard server. On the server
 * itself they need nothing more; phones and tablets unlock with the setup PIN. The
 * cards are built once and then updated in place, so a curve being dragged or a
 * slider being moved is never replaced by a refresh. */
const SVG_NS = 'http://www.w3.org/2000/svg';
const CURVE = {left: 38, right: 312, top: 12, bottom: 172, tMin: 20, tMax: 100};
const PRESETS = {
  Silent: [[30, 20], [50, 30], [65, 50], [78, 80], [88, 100]],
  Balanced: [[30, 25], [45, 35], [60, 55], [72, 80], [84, 100]],
  Performance: [[30, 40], [45, 55], [58, 75], [68, 90], [78, 100]]
};
const FAN_MODES = [['auto', 'Motherboard'], ['curve', 'Curve'], ['fixed', 'Fixed']];
const cooling = {outputsKey: '', lightsKey: '', outputs: new Map(), lights: new Map()};

function controlToken() {
  try { return sessionStorage.getItem('neko-control') || ''; } catch { return ''; }
}
function setControlToken(value) {
  try {
    if (value) { sessionStorage.setItem('neko-control', value); } else { sessionStorage.removeItem('neko-control'); }
  } catch { /* the page still works; it asks again next time */ }
}
function tokenHeader() {
  const t = controlToken();
  return t ? {'X-Neko-Token': t} : {};
}
async function postControl(op, body = {}) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 10000);
  try {
    const response = await fetch('/api/control/' + op, {
      method: 'POST', cache: 'no-store', credentials: 'same-origin', signal: controller.signal,
      headers: {'Content-Type': 'application/json', ...tokenHeader()}, body: JSON.stringify(body)
    });
    const answer = object(await response.json());
    if (answer.error === 'E43') { setControlToken(''); }
    return answer;
  } catch {
    return {ok: false, error: controller.signal.aborted ? 'E02' : 'E01'};
  } finally { clearTimeout(timer); }
}
function controlMessage(answer) {
  const code = text(answer.error, 'E01');
  return code + ' · ' + text(answer.message, ERRORS[code]?.[0] || 'Something went wrong');
}
/* Send a change; a refused PIN opens the unlock dialog. True when it was accepted. */
async function change(op, body) {
  const answer = await postControl(op, body);
  if (answer.ok) {
    if (answer.note) { toast(answer.note); }
    void load('control');
    return true;
  }
  if (answer.error === 'E43' && state.control?.access?.pin_set) { unlockDialog(); }
  toast(controlMessage(answer));
  return false;
}
function canChange() {
  if (state.controlError) { return false; } // the page may be showing old readings
  const access = object(state.control?.access);
  return access.local === true || access.unlocked === true;
}

/* ---------- access ---------- */
function unlockDialog() {
  const form = el('form', 'unlock'), input = el('input');
  Object.assign(input, {type: 'password', inputMode: 'numeric', autocomplete: 'off', maxLength: 8, required: true, id: 'pin-input'});
  input.pattern = '[0-9]{4,8}';
  const label = el('label', 'field', 'PIN (4 to 8 digits)');
  label.append(input);
  const submit = el('button', 'button', 'Unlock');
  submit.type = 'submit';
  form.append(label, submit);
  form.addEventListener('submit', async e => {
    e.preventDefault();
    submit.disabled = true;
    const answer = await postControl('unlock', {pin: input.value});
    submit.disabled = false;
    if (!answer.ok) { toast(controlMessage(answer)); input.select(); return; }
    setControlToken(text(answer.token));
    $('dialog').close();
    toast('Unlocked on this device until you close it or 12 hours pass.');
    void load('control');
  });
  openDialog('FANS & LIGHTS', 'Unlock changes', [
    el('p', '', 'Enter the PIN chosen during setup. It is only needed on phones, tablets and other computers.'), form
  ]);
  requestAnimationFrame(() => input.focus());
}
function lockAgain() {
  setControlToken('');
  toast('Locked on this device.');
  void load('control');
}
function accessRow(access) {
  if (access.local) { return [hint('You are on the server itself, so changes need no PIN.')]; }
  if (access.unlocked) { return [hint('Unlocked on this device.'), button('Lock', lockAgain)]; }
  if (access.pin_set) {
    return [hint('Looking is open to everyone on your Tailscale. Changes from this device need your PIN.'), button('Unlock changes', unlockDialog, 'button small')];
  }
  return [hint('Changes are only possible on the server itself. Run setup again to choose a PIN for phones and tablets.')];
}

/* ---------- curve editor ---------- */
function svgNode(tag, attrs, parent) {
  const n = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) { n.setAttribute(k, v); }
  parent?.append(n);
  return n;
}
const curveX = t => CURVE.left + (clamp(t, CURVE.tMin, CURVE.tMax) - CURVE.tMin) / (CURVE.tMax - CURVE.tMin) * (CURVE.right - CURVE.left);
const curveY = d => CURVE.bottom - clamp(d) / 100 * (CURVE.bottom - CURVE.top);
/* The same rule the control service follows: flat before the first and after the
 * last point, straight lines between. */
function curveDuty(points, t) {
  if (!points.length) { return 0; }
  if (t <= points[0][0]) { return points[0][1]; }
  for (let i = 1; i < points.length; i++) {
    const [t0, d0] = points[i - 1], [t1, d1] = points[i];
    if (t <= t1) { return d0 + (d1 - d0) * (t - t0) / (t1 - t0); }
  }
  return points.at(-1)[1];
}
function curveGrid(svg) {
  for (const d of [0, 25, 50, 75, 100]) {
    svgNode('line', {x1: CURVE.left, x2: CURVE.right, y1: curveY(d), y2: curveY(d), class: 'grid'}, svg);
    svgNode('text', {x: CURVE.left - 6, y: curveY(d) + 4, class: 'axis', 'text-anchor': 'end'}, svg).textContent = d + '%';
  }
  for (let t = CURVE.tMin; t <= CURVE.tMax; t += 20) {
    svgNode('line', {x1: curveX(t), x2: curveX(t), y1: CURVE.top, y2: CURVE.bottom, class: 'grid'}, svg);
    svgNode('text', {x: curveX(t), y: CURVE.bottom + 16, class: 'axis', 'text-anchor': 'middle'}, svg).textContent = t + '°';
  }
}
/* Where a pointer is, in curve units. */
function curvePoint(svg, event) {
  const point = new DOMPoint(event.clientX, event.clientY).matrixTransform(svg.getScreenCTM().inverse());
  const t = CURVE.tMin + (point.x - CURVE.left) / (CURVE.right - CURVE.left) * (CURVE.tMax - CURVE.tMin);
  return [Math.round(t), Math.round(clamp((CURVE.bottom - point.y) / (CURVE.bottom - CURVE.top) * 100))];
}
/* Keep a moved point between its neighbours, inside the chart. */
function placePoint(points, i, [t, d]) {
  const low = i > 0 ? points[i - 1][0] + 1 : CURVE.tMin;
  const high = i < points.length - 1 ? points[i + 1][0] - 1 : CURVE.tMax;
  points[i] = [clamp(t, low, high), clamp(d)];
}
const ARROWS = {ArrowUp: [0, 5], ArrowDown: [0, -5], ArrowRight: [1, 0], ArrowLeft: [-1, 0]};
function placeHandle(handle, i, [t, d]) {
  handle.setAttribute('cx', curveX(t));
  handle.setAttribute('cy', curveY(d));
  handle.setAttribute('aria-label', 'Point ' + (i + 1) + ': ' + t + ' °C, ' + d + '% (arrow keys move it)');
}
function curveHandle(editor, i) {
  const handle = svgNode('circle', {r: 9, class: 'handle', tabindex: 0, role: 'slider'});
  handle.addEventListener('pointerdown', e => {
    if (!editor.enabled()) { return; }
    e.preventDefault();
    handle.setPointerCapture(e.pointerId);
    editor.dragging = i;
  });
  handle.addEventListener('pointermove', e => {
    if (editor.dragging !== i) { return; }
    placePoint(editor.points(), i, curvePoint(editor.svg, e));
    editor.changed();
  });
  const stop = () => { editor.dragging = -1; };
  handle.addEventListener('pointerup', stop);
  handle.addEventListener('pointercancel', stop);
  handle.addEventListener('keydown', e => {
    const move = ARROWS[e.key];
    if (!move || !editor.enabled()) { return; }
    e.preventDefault();
    const [pt, pd] = editor.points()[i];
    placePoint(editor.points(), i, [pt + move[0], pd + move[1]]);
    editor.changed();
  });
  return handle;
}
/* points(): the draft's points (edited in place); changed(): after an edit. */
function curveEditor(points, enabled, changed) {
  const svg = svgNode('svg', {viewBox: '0 0 330 196', class: 'curve'});
  svg.setAttribute('aria-label', 'Fan curve: speed by temperature');
  curveGrid(svg);
  const area = svgNode('path', {class: 'curve-area'}, svg);
  const line = svgNode('path', {class: 'curve-line'}, svg);
  const now = svgNode('g', {class: 'curve-now'}, svg);
  const handles = svgNode('g', {}, svg);
  const editor = {svg, points, enabled, dragging: -1};
  editor.draw = () => {
    const p = points(), path = p.map(([t, d], i) => (i ? 'L' : 'M') + curveX(t).toFixed(1) + ' ' + curveY(d).toFixed(1)).join(' ');
    line.setAttribute('d', 'M' + CURVE.left + ' ' + curveY(p[0][1]) + ' ' + path.slice(1) + ' L' + CURVE.right + ' ' + curveY(p.at(-1)[1]));
    area.setAttribute('d', line.getAttribute('d') + ' L' + CURVE.right + ' ' + CURVE.bottom + ' L' + CURVE.left + ' ' + CURVE.bottom + ' Z');
    // Handles are moved, not replaced, so a drag or keyboard focus carries on.
    if (handles.children.length !== p.length) { handles.replaceChildren(...p.map((_, i) => curveHandle(editor, i))); }
    p.forEach((point, i) => placeHandle(handles.children[i], i, point));
  };
  editor.changed = () => { changed(); editor.draw(); };
  editor.marker = (temperature, duty) => {
    now.replaceChildren();
    if (!numeric(temperature)) { return; }
    svgNode('line', {x1: curveX(temperature), x2: curveX(temperature), y1: CURVE.top, y2: CURVE.bottom}, now);
    svgNode('circle', {cx: curveX(temperature), cy: curveY(duty), r: 5}, now);
  };
  editor.draw();
  return editor;
}

/* ---------- fan outputs ---------- */
function outputName(out, setting) {
  return text(setting?.name) || 'Output ' + text(out.label).replace(/^pwm/, '') + ' · ' + text(out.chip);
}
function fanName(fan) { return text(fan.label).replace(/^fan/, 'Fan '); }
function temperatureName(t) { return text(t.label) + ' · ' + text(t.chip); }
function draftFrom(setting, temps) {
  const s = object(setting);
  return {
    mode: firstOf([[s.mode === 'curve', 'curve'], [s.mode === 'fixed', 'fixed']], 'auto'),
    fixed: numeric(s.fixed) ? s.fixed : 60,
    sensor: text(s.sensor, temps[0]?.id || ''),
    points: list(s.points).length ? s.points.map(p => [...p]) : PRESETS.Balanced.map(p => [...p]),
    min: numeric(s.min) ? s.min : 20,
    name: text(s.name),
    fans: values(s.fans)
  };
}
function settingFrom(draft) {
  const setting = {mode: draft.mode, min: draft.min, name: draft.name.trim(), fans: draft.fans};
  if (draft.mode === 'fixed') { setting.fixed = draft.fixed; }
  if (draft.mode === 'curve') { Object.assign(setting, {sensor: draft.sensor, points: draft.points}); }
  return setting;
}
function rangeField(label, min, max, value, onInput) {
  const wrap = el('label', 'field range'), input = el('input'), shown = el('output', '', value + '%');
  Object.assign(input, {type: 'range', min, max, step: 1, value});
  input.addEventListener('input', () => { shown.textContent = input.value + '%'; onInput(Number(input.value)); });
  const head = el('span', 'range-head', label);
  head.append(shown);
  wrap.append(head, input);
  return wrap;
}
function modeChips(card) {
  const chips = el('div', 'chips');
  chips.setAttribute('role', 'group');
  chips.setAttribute('aria-label', 'Who sets the speed');
  for (const [mode, label] of FAN_MODES) {
    const b = button(label, () => { card.draft.mode = mode; card.edited(); });
    b.className = '';
    b.dataset.mode = mode;
    chips.append(b);
  }
  return chips;
}
function sensorSelect(card, temps) {
  const wrap = el('label', 'field', 'Follows'), select = el('select');
  for (const t of temps) {
    const option = el('option', '', temperatureName(t));
    option.value = t.id;
    select.append(option);
  }
  select.value = card.draft.sensor;
  select.addEventListener('change', () => { card.draft.sensor = select.value; card.edited(); });
  wrap.append(select);
  return wrap;
}
function presetRow(card) {
  const row = el('div', 'chips presets');
  for (const [name, points] of Object.entries(PRESETS)) {
    row.append(button(name, () => { card.draft.points = points.map(p => [...p]); card.edited(); }, 'button small secondary'));
  }
  row.append(button('+ Point', () => addPoint(card), 'button small secondary'), button('− Point', () => removePoint(card), 'button small secondary'));
  return row;
}
/* A new point halfway along the widest gap; the last point can't go below two. */
function addPoint(card) {
  const p = card.draft.points;
  if (p.length >= 8) { toast('A curve has at most 8 points.'); return; }
  let at = 1;
  for (let i = 1; i < p.length; i++) {
    if (p[i][0] - p[i - 1][0] > p[at][0] - p[at - 1][0]) { at = i; }
  }
  const t = Math.round((p[at][0] + p[at - 1][0]) / 2);
  if (t <= p[at - 1][0] || t >= p[at][0]) { toast('No room for another point.'); return; }
  p.splice(at, 0, [t, Math.round(curveDuty(p, t))]);
  card.edited();
}
function removePoint(card) {
  if (card.draft.points.length <= 2) { toast('A curve needs at least 2 points.'); return; }
  card.draft.points.splice(-2, 1);
  card.edited();
}
function nameField(card) {
  const wrap = el('label', 'field', 'Name (optional)'), input = el('input');
  Object.assign(input, {type: 'text', maxLength: 40, placeholder: 'e.g. Processor fan', value: card.draft.name});
  input.addEventListener('input', () => { card.draft.name = input.value; card.touch(); });
  wrap.append(input);
  return wrap;
}
async function saveOutput(card) {
  const version = card.version, setting = settingFrom(card.draft); // what is sent, as it is now
  card.saving = true;
  card.refreshButtons();
  const ok = await change('fan', {output: card.id, setting: structuredClone(setting)});
  card.saving = false;
  if (ok && card.version === version) {
    card.dirty = false;
    toast('Saved. The fan follows it now.');
  } else if (ok) {
    toast('Saved, but you changed it while saving: press Save again to keep your latest change.');
  }
  card.refreshButtons();
}
function outputCard(out, c) {
  const fans = object(c.fans), temps = list(fans.inventory?.temps);
  const card = {id: out.id, draft: draftFrom(fans.settings?.[out.id], temps), dirty: false, saving: false, version: 0, root: el('article', 'card fan-control')};
  const title = el('h3'), badgeNode = el('span', 'badge neutral'), now = el('p', 'fan-now'), note = el('p', 'hint');
  const head = el('div', 'card-heading');
  head.append(title, badgeNode);
  const chips = modeChips(card);
  const fixed = rangeField('Speed', 0, 100, card.draft.fixed, v => { card.draft.fixed = v; card.touch(); });
  const curve = el('div', 'curve-box');
  const editor = curveEditor(() => card.draft.points, canChange, () => card.touch());
  curve.append(sensorSelect(card, temps), editor.svg, presetRow(card));
  const minimum = rangeField('Never slower than', 20, 100, card.draft.min, v => { card.draft.min = v; card.touch(); });
  const save = button('Save', () => saveOutput(card), 'button small');
  const undo = button('Undo changes', () => { card.draft = draftFrom(state.control?.fans?.settings?.[out.id], temps); card.dirty = false; card.rebuild(); });
  const actions = el('div', 'fan-actions');
  actions.append(save, undo);
  const naming = el('details', 'more');
  naming.append(el('summary', '', 'Name this output'), nameField(card));
  card.root.append(head, now, note, chips, fixed, curve, minimum, naming, actions);
  card.refreshButtons = () => {
    const allowed = canChange();
    save.disabled = !card.dirty || card.saving || !allowed;
    undo.disabled = !card.dirty || card.saving;
    for (const n of card.root.querySelectorAll('input, select, .chips button')) { n.disabled = !allowed; }
  };
  card.rebuild = () => {
    for (const b of chips.children) { b.setAttribute('aria-pressed', String(b.dataset.mode === card.draft.mode)); }
    fixed.hidden = card.draft.mode !== 'fixed';
    curve.hidden = card.draft.mode !== 'curve';
    minimum.hidden = card.draft.mode === 'auto';
    // Every control and label shows the draft (after Undo, or a change elsewhere).
    for (const [field, value] of [[fixed, card.draft.fixed], [minimum, card.draft.min]]) {
      field.querySelector('input').value = value;
      field.querySelector('output').textContent = value + '%';
    }
    naming.querySelector('input').value = card.draft.name;
    curve.querySelector('select').value = card.draft.sensor;
    editor.draw();
    card.refreshButtons();
  };
  // Every edit has a number, so a save only clears the edits it actually sent.
  card.touch = () => { card.dirty = true; card.version += 1; card.refreshButtons(); };
  card.edited = () => { card.touch(); card.rebuild(); };
  card.update = data => updateOutput(card, data, {title, badgeNode, now, note, editor});
  card.rebuild();
  card.update(c);
  return card;
}
const OUTPUT_STATES = {controlled: ['running', 'NekoServer'], auto: ['neutral', 'Motherboard'], stuck: ['warning', 'Not handed back']};
function linkedFans(setting, inventory) {
  const ids = values(setting?.fans);
  return list(inventory.fans).filter(f => ids.includes(f.id));
}
function updateOutput(card, c, parts) {
  const fans = object(c.fans), inventory = object(fans.inventory), saved = fans.settings?.[card.id];
  const out = list(inventory.outputs).find(o => o.id === card.id) || {};
  const live = object(fans.state?.[card.id]);
  const fresh = draftFrom(saved, list(inventory.temps));
  if (!card.dirty && !card.saving && parts.editor.dragging < 0 && JSON.stringify(fresh) !== JSON.stringify(card.draft)) {
    card.draft = fresh; // changed elsewhere (another device)
    card.rebuild();
  }
  parts.title.textContent = outputName(out, saved);
  const [look, label] = OUTPUT_STATES[live.mode] || OUTPUT_STATES.auto;
  parts.badgeNode.className = 'badge ' + look;
  parts.badgeNode.textContent = label;
  const linked = linkedFans(saved, inventory);
  parts.now.textContent = joined([
    'Now ' + pct(firstOf([[numeric(live.percent), live.percent]], out.value)),
    ...linked.map(f => fanName(f) + ' ' + (numeric(f.value) ? Math.round(f.value) + ' RPM' : '—'))
  ]);
  parts.note.textContent = text(live.note, linked.length ? '' : 'Which fan this drives is unknown: use Detect fans.');
  const sensor = list(inventory.temps).find(t => t.id === card.draft.sensor);
  const temperature = sensor?.value;
  parts.editor.marker(temperature, Math.max(card.draft.min, curveDuty(card.draft.points, temperature)));
  card.refreshButtons();
}

/* ---------- detect ---------- */
function startDetect() {
  openDialog('FANS', 'Detect fans', [
    el('p', '', 'NekoServer sets every fan to full speed, then slows one output at a time to see which fan follows. It takes about a minute per output and the computer will be loud.'),
    el('p', '', 'It stops by itself if anything gets hot, and you can stop it at any time. The motherboard takes over again afterwards.'),
    button('Start detecting', async () => { $('dialog').close(); await change('detect', {}); }, 'button')
  ]);
}
function detectRow(fans) {
  const d = object(fans.detect);
  if (d.running) {
    const row = el('div', 'detect running');
    row.append(el('strong', '', text(d.step, 'Detecting fans…')), meter(count(d.done) / Math.max(1, count(d.total)) * 100), button('Stop', () => change('detect-cancel', {})));
    return [row];
  }
  const parts = [button('Detect fans', startDetect, 'button small')];
  if (fans.recovery) { parts.unshift(el('p', 'hint reason', text(fans.recovery))); }
  if (d.error) { parts.push(hint(text(d.error))); }
  if (d.result) { parts.push(hint('Detection finished. Each output below lists the fans it drives.')); }
  return parts;
}
function outputsBadge(c) {
  if (state.controlError) { return ['warning', state.controlError + (c ? ' · old readings' : '')]; }
  if (!c) { return ['unknown', 'Waiting']; }
  const outputs = list(c.fans?.inventory?.outputs);
  return outputs.length ? ['neutral', outputs.length + ' outputs'] : ['unknown', 'None found'];
}
function renderOutputs(c) {
  const outputs = list(c.fans?.inventory?.outputs);
  const key = outputs.map(o => o.id).join('|') + '#' + list(c.fans?.inventory?.temps).map(t => t.id).join('|');
  if (key !== cooling.outputsKey) {
    cooling.outputsKey = key;
    cooling.outputs = new Map(outputs.map(o => [o.id, outputCard(o, c)]));
    put('fan-outputs', outputs.length ? [...cooling.outputs.values()].map(card => card.root) : [empty('No fan outputs found that Linux can control. Many boards need a sensor driver (see Help, “Fans don’t change”).')]);
    return;
  }
  for (const card of cooling.outputs.values()) { card.update(c); }
}
function unlinkedFans(c) {
  const settings = object(c.fans?.settings);
  const linked = new Set(Object.values(settings).flatMap(s => values(s?.fans)));
  return list(c.fans?.inventory?.fans).filter(f => !linked.has(f.id));
}

/* ---------- lighting ---------- */
function lightEffects(d) {
  return list(d.effects).filter(e => !['Direct', 'Custom', 'Static'].includes(text(e.name)));
}
function lightChosen(d) {
  const chosen = object(d.chosen);
  return {color: text(chosen.color, text(d.color, '#ffffff')), brightness: numeric(chosen.brightness) ? chosen.brightness : 100, effect: text(chosen.effect), off: chosen.off === true};
}
function sendLight(key, body) { return change('light', {device: key, ...body}); }
/* Moving a slider sends the last value a moment after it stops. */
function debounced(fn, ms = 250) {
  let timer;
  const call = value => { clearTimeout(timer); timer = setTimeout(() => fn(value), ms); };
  call.cancel = () => clearTimeout(timer); // a later action (Turn off) wins over it
  return call;
}
function effectSelect(d, chosen, key) {
  const wrap = el('label', 'field', 'Effect'), select = el('select');
  for (const [value, name] of [['', 'Steady colour'], ...lightEffects(d).map(e => [e.name, e.name])]) {
    const option = el('option', '', name);
    option.value = value;
    select.append(option);
  }
  select.value = chosen.effect;
  select.addEventListener('change', () => sendLight(key, {effect: select.value, off: false}));
  wrap.append(select);
  return wrap;
}
/* Effects such as a rainbow make their own colours: OpenRGB can't dim them. */
function effectMakesColours(d, effect) {
  return !!effect && list(d.effects).some(e => e.name === effect && e.takes_color === false);
}
/* A control shows the server's value unless someone is using it right now. */
function showIfIdle(input, value, heldUntil) {
  if (document.activeElement === input || Date.now() < heldUntil) { return; }
  if (input.value !== String(value)) { input.value = value; }
}
function lightCard(d) {
  const key = text(d.key), chosen = lightChosen(d), root = el('article', 'card light');
  let held = 0; // until when the person's own edit wins over refreshed values
  const hold = () => { held = Date.now() + 2500; };
  const swatch = el('span', 'swatch');
  const head = el('div', 'card-heading');
  head.append(el('h3', '', text(d.name, 'Lighting')), swatch);
  const colorWrap = el('label', 'field', 'Colour'), color = el('input');
  Object.assign(color, {type: 'color', value: chosen.color});
  const sendColor = debounced(v => sendLight(key, {color: v, off: false}));
  color.addEventListener('input', () => { hold(); swatch.style.setProperty('--swatch', color.value); sendColor(color.value); });
  colorWrap.append(color);
  const sendBrightness = debounced(v => sendLight(key, {brightness: v, off: false}));
  const brightness = rangeField('Brightness', 0, 100, chosen.brightness, v => { hold(); sendBrightness(v); });
  const effect = effectSelect(d, chosen, key);
  effect.addEventListener('change', hold);
  const power = button(chosen.off ? 'Turn on' : 'Turn off', () => {
    sendColor.cancel();
    sendBrightness.cancel();
    void sendLight(key, {off: !lightChosen(cooling.lights.get(key).data).off});
  });
  const own = hint('This effect makes its own colours, so colour and brightness don’t apply to it.');
  root.append(head, el('p', 'hint', joined([text(d.kind), d.leds ? d.leds + ' lights' : '', text(d.location)])), colorWrap, brightness, effect, own, power);
  const card = {root, data: d};
  card.update = next => {
    card.data = next;
    const now = lightChosen(next);
    swatch.style.setProperty('--swatch', now.color);
    swatch.classList.toggle('off', now.off);
    power.textContent = now.off ? 'Turn on' : 'Turn off';
    // After "Apply to all" or a change on another device, the controls follow.
    showIfIdle(color, now.color, held);
    const level = brightness.querySelector('input');
    showIfIdle(level, now.brightness, held);
    if (Date.now() >= held && document.activeElement !== level) { brightness.querySelector('output').textContent = now.brightness + '%'; }
    showIfIdle(effect.querySelector('select'), now.effect, held);
    const ownColours = effectMakesColours(next, now.effect);
    own.hidden = !ownColours;
    for (const n of root.querySelectorAll('input, select, button')) { n.disabled = !canChange(); }
    color.disabled = color.disabled || ownColours;
    brightness.querySelector('input').disabled = !canChange() || ownColours;
  };
  card.update(d);
  return card;
}
function applyAll(color, brightness) {
  for (const card of cooling.lights.values()) { void sendLight(card.data.key, {color, brightness, off: false, effect: ''}); }
}
function allLightsRow() {
  const row = el('div', 'light-all'), color = el('input');
  Object.assign(color, {type: 'color', value: '#c4b3fa'});
  color.setAttribute('aria-label', 'Colour for all lights');
  let level = 100;
  row.append(color, rangeField('Brightness', 0, 100, level, v => { level = v; }),
    button('Apply to all', () => applyAll(color.value, level), 'button small'),
    button('All off', () => { for (const card of cooling.lights.values()) { void sendLight(card.data.key, {off: true}); } }));
  return row;
}
const LIGHT_STATES = {
  detected: ['neutral', null, ''],
  none_reported: ['unknown', 'None found', 'OpenRGB is running but found no lighting. Memory and motherboard lights may need a restart after setup (see Help, “Lights don’t change”).'],
  not_running: ['warning', 'OpenRGB off', 'OpenRGB is not running, so lighting can’t be changed. Help shows the command to check it.']
};
function renderLights(c) {
  const lights = object(c.lights), devices = list(lights.devices);
  const [look, label, message] = LIGHT_STATES[lights.status] || LIGHT_STATES.not_running;
  setBadge('light-state', look, label || devices.length + ' found');
  $('light-all').hidden = devices.length < 2;
  const key = devices.map(d => d.key).join('|');
  if (key !== cooling.lightsKey || !devices.length) {
    cooling.lightsKey = key;
    cooling.lights = new Map(devices.map(d => [d.key, lightCard(d)]));
    put('lights', devices.length ? [...cooling.lights.values()].map(card => card.root) : [empty(message)]);
  }
  for (const d of devices) { cooling.lights.get(d.key)?.update(d); }
  if ($('light-all').childElementCount === 0) { put('light-all', [allLightsRow()]); }
  for (const n of $('light-all').querySelectorAll('input, button')) { n.disabled = !canChange(); }
}

function renderCooling() {
  const c = state.control;
  setBadge('fan-state', ...outputsBadge(c));
  if (!c) {
    const waiting = state.controlError ? codeLine(state.controlError) : 'Looking for fans and lights…';
    put('fan-outputs', [empty(waiting)]);
    put('lights', [empty(waiting)]);
    cooling.outputsKey = cooling.lightsKey = '';
    return;
  }
  put('control-access', accessRow(object(c.access)));
  put('fan-detect', detectRow(object(c.fans)));
  renderOutputs(c);
  const loose = unlinkedFans(c);
  put('fan-loose', loose.length ? [hint('Fans not linked to an output: ' + loose.map(f => fanName(f) + ' (' + (numeric(f.value) ? Math.round(f.value) + ' RPM' : '—') + ')').join(', '))] : []);
  renderLights(c);
}

/* ---------- processes ---------- */
const PROCESS_STATES = {R: 'running', S: 'waiting', D: 'waiting for disk', Z: 'finished (zombie)', T: 'paused', I: 'idle'};
const byPid = (a, b) => count(a.pid) - count(b.pid);
const biggestFirst = key => (a, b) => (numeric(b[key]) ? b[key] : -1) - (numeric(a[key]) ? a[key] : -1) || byPid(a, b);
const PROCESS_SORTS = {name: (a, b) => text(a.name).localeCompare(text(b.name)), pid: byPid, cpu: biggestFirst('cpu'), memory: biggestFirst('memory')};
function appName(row) { return row.app && CATALOG[row.app] ? CATALOG[row.app].name : ''; }
function processRow(r) {
  const tr = el('tr');
  const cells = [['Process', text(r.name, '?'), PROCESS_STATES[r.state]], ['App', appName(r) || '—'], ['Processor', pct(r.cpu)], ['Memory', bytes(r.memory)], ['Number', r.pid]];
  for (const [label, value, sub] of cells) {
    const td = el('td', ['Process', 'App'].includes(label) ? '' : 'num');
    td.dataset.label = label;
    td.append(el('span', '', value));
    if (sub) { td.append(el('small', '', sub)); }
    tr.append(td);
  }
  return tr;
}
function noProcessesMessage() {
  if (state.processes) { return 'No matching processes.'; }
  return state.processError ? codeLine(state.processError) : 'Loading…';
}
function noProcessesRow() {
  const tr = el('tr'), td = el('td', 'empty', noProcessesMessage());
  td.colSpan = 5;
  tr.append(td);
  return tr;
}
function renderProcesses() {
  const p = object(state.processes), rows = list(p.rows);
  const query = $('process-search').value.trim().toLowerCase();
  const shown = rows.filter(r => (text(r.name) + ' ' + appName(r) + ' ' + r.pid).toLowerCase().includes(query));
  shown.sort(PROCESS_SORTS[$('process-sort').value] || PROCESS_SORTS.cpu);
  const pages = Math.max(1, Math.ceil(shown.length / 50));
  state.processPage = Math.min(state.processPage, pages - 1);
  setBadge('process-count', state.processError ? 'warning' : 'neutral', state.processError || (state.processes ? rows.length + ' running' : 'Waiting'));
  const page = shown.slice(state.processPage * 50, (state.processPage + 1) * 50);
  put('process-rows', page.length ? page.map(processRow) : [noProcessesRow()]);
  $('process-page').textContent = 'Page ' + (state.processPage + 1) + ' of ' + pages;
  $('process-prev').disabled = state.processPage === 0;
  $('process-next').disabled = state.processPage >= pages - 1;
  $('process-note').textContent = '100% processor = one full core, so busy programs can go above 100%.' + (p.truncated ? ' Only the first 5000 are listed.' : '');
}

/* ---------- health ---------- */
function driveBadge(disk, old) {
  if (old) { return badge('unknown', 'Old reading'); }
  if (disk.status === 'mounted') { return badge('pass', 'Connected'); }
  return badge('fail', disk.status === 'wrong_drive' ? 'Wrong drive' : 'Missing');
}
function fullness(percent) { return firstOf([[percent >= 90, 'hot'], [percent >= 80, 'warm']], ''); }
function smartLine(smart) {
  const temperature = numeric(smart.temperature) ? ' · ' + smart.temperature + ' °C' : '';
  const checked = smart.checked_at ? ' · checked ' + ago(smart.checked_at) : '';
  const fine = smart.status === 'pass' || smart.status === 'unknown';
  return el('p', fine ? '' : 'reason', 'Health: ' + (LABELS[smart.status] || 'Not checked yet') + temperature + checked);
}
function driveCard(disk, old) {
  const card = el('article', 'card'), smart = object(disk.smart);
  card.append(cardHeading(text(disk.name, 'Drive'), driveBadge(disk, old)),
    el('p', 'big', numeric(disk.total) && numeric(disk.used) ? bytes(disk.total - disk.used) + ' free' : 'Space unknown'),
    meter(disk.used_percent, fullness(count(disk.used_percent))),
    el('p', '', bytes(disk.used) + ' used of ' + bytes(disk.total)),
    smartLine(smart));
  if (smart.reason && smart.status !== 'pass') { card.append(hint(text(smart.reason))); }
  return card;
}
/* Starting at boot and working now are separate: a failed service is never green. */
function startupBadge(u) {
  if (u.active === 'failed') { return badge('fail', u.enabled ? 'Failed (starts at boot)' : 'Failed'); }
  if (!u.enabled) { return badge('warning', 'Won’t start after a restart'); }
  return badge('pass', u.active === 'active' ? 'Starts by itself · on' : 'Starts by itself');
}
function startupRow(u) {
  const row = el('div', 'row'), body = el('div');
  body.append(el('h3', '', text(u.name, u.unit)), el('p', 'mono', text(u.unit)));
  if (u.active === 'failed' || !u.enabled) { body.append(keyed(button('How to fix', () => showGuide('startup', u.unit), 'text-button'), 'startup-' + u.unit)); }
  row.append(body, startupBadge(u));
  return row;
}
const MONITOR_RESULTS = {pass: 'The last check-in worked.', fail: 'The last attempt failed.', stale: 'No recent result.'};
function monitorMessage(v) {
  const timer = object(v.timer);
  return firstOf([[!timer.installed, 'Not set up. This is optional.'], [!timer.active, 'Its schedule is off.']], MONITOR_RESULTS[v.status] || 'Set up; waiting for its first result.');
}
function monitorCard(title, value, guide, old) {
  const card = el('article', 'card'), v = object(value);
  card.append(cardHeading(title, badge(old ? 'unknown' : v.status)), el('p', '', monitorMessage(v)), keyed(button('Set up & check', () => showGuide(guide), 'text-button'), 'monitor-' + guide));
  return card;
}
function readinessNote(media) {
  const notes = {
    disabled: () => 'Detailed app checks are off (the monthly update still checks apps).',
    current: () => 'Detailed app checks · ' + (numeric(media.age_seconds) ? ago(nowSeconds() - media.age_seconds) : 'time unknown'),
    stale: () => 'Detailed app checks are overdue.'
  };
  return (notes[media.status] || (() => 'No detailed app check yet.'))();
}
function readinessRow(check, trusted) {
  const row = el('div', 'row');
  row.append(el('span', '', text(check.name, 'App check')), badge(trusted ? check.status : 'unknown'));
  return row;
}
function updatesBox(updates) {
  const box = el('div', 'callout');
  box.append(el('strong', '', 'Bigger updates waiting for you'));
  for (const u of updates) { box.append(el('p', '', text(u.name, u.app) + ': ' + text(u.current) + ' → ' + text(u.available))); }
  box.append(button('About updates', () => showGuide('updates'), 'text-button'));
  return box;
}
function renderHealth() {
  const s = state.status;
  if (!s) { return; }
  const old = statusStale(), media = s.media, trusted = !old && media.status === 'current';
  put('drives', s.storage.length ? s.storage.map(disk => driveCard(disk, old)) : [empty('No drives reported.')]);
  put('startup', s.startup.length ? s.startup.map(startupRow) : [empty('The startup check runs with the next reading.')]);
  put('monitoring', [monitorCard('Outside heartbeat', s.heartbeat, 'heartbeat', old), monitorCard('Phone notifications', s.notifications, 'notifications', old)]);
  put('readiness', [el('p', 'section-note', readinessNote(media)), ...media.checks.map(c => readinessRow(c, trusted))]);
  put('updates', s.updates.length ? [updatesBox(s.updates)] : []);
}

/* ---------- help ---------- */
function shellQuote(v) { return "'" + v.replaceAll("'", "'\\''") + "'"; }
function installedScript(name) {
  const root = state.status?.storage.find(d => d.id === 'data_ssd')?.mount, version = state.status?.version || '';
  const safeRoot = typeof root === 'string' && /^\/[A-Za-z0-9_./-]+$/.test(root) && !root.split('/').includes('..');
  return safeRoot && /^\d+\.\d+\.\d+$/.test(version) ? root + '/installer/releases/v' + version + '/' + name : null;
}
function installer(recover = false) {
  const script = installedScript('install.sh');
  if (!script) { return null; }
  return 'sudo bash ' + shellQuote(script) + (recover ? ' --recover' : '');
}
const GUIDES = {
  general: {title: 'Find out what needs attention', intro: 'Start with the first urgent item. Keep your files as they are while you look.', steps: ['Check whether a backup or monthly update is running: short app downtime during those is normal.', 'Open Server health for missing drives, full storage or stopped apps.', 'If readings are old, follow “Dashboard readings” in Help.'], unit: 'nekoserver-dashboard-collect.service'},
  collector: {title: 'Dashboard readings are old or missing', intro: 'The page can open even when the reading service has stopped.', steps: ['On the server, open Konsole.', 'Check the reading service and its schedule with the commands below.', 'Make sure the app SSD is connected, then run setup again if it still fails.'], unit: 'nekoserver-dashboard-collect.service', extra: 'systemctl status nekoserver-dashboard-collect.timer --no-pager'},
  dashboard: {title: 'Restart the dashboard', intro: 'Live readings come from the dashboard service itself. Restarting it is safe.', steps: ['On the server, open Konsole.', 'Run the restart command below, then reload this page.'], unit: 'nekoserver-dashboard.service', extra: 'sudo systemctl restart nekoserver-dashboard.service'},
  apps: {title: 'An app is stopped or not responding', intro: 'First check whether a backup or monthly update explains it.', steps: ['Open Backups and look for a running job. Let it finish.', 'Try the app again. On a phone, turn on Tailscale.', 'Still stuck? Look at Docker on the server, or run setup again: it restarts what should be running.'], unit: 'docker.service', extra: 'sudo docker ps -a', setup: true},
  startup: {title: 'Get a service running again', intro: 'A part of the server has stopped with an error, or won’t start when the computer is switched on.', steps: ['Read its log with the commands below: the last lines usually say why.', 'Run setup again: it switches every NekoServer part back on and starts it.', 'Or start just that part with “sudo systemctl enable --now <name>” (the name is shown under it).'], unit: null, setup: true},
  backup: {title: 'Check a backup', intro: 'Keep the copies you have. One failed run doesn’t mean older backups are bad.', steps: ['Check the reason shown, free space, and that the backup drive is connected.', 'Look at the log below. For damaged or missing files, use the repair guide.', 'Fix the cause, then let the next scheduled run go (or run setup again).'], unit: null},
  recovery: {title: 'Get your files back', intro: 'The recovery helper restores into a separate folder first, so nothing is overwritten.', steps: ['Connect the backup drive you want to use.', 'On the server, open Konsole and start the recovery helper below.', 'Pick the backup and a new folder. Check the files before replacing anything.'], recover: true},
  integrity: {title: 'Repair a damaged or missing copy', intro: 'A checksum warning needs a copy that still matches. Never delete the other copies.', steps: ['Connect the USB backup drive if you have one, so both copies are checked.', 'Start the recovery helper below and choose “Repair damaged backup copies”.', 'If no copy matches, keep everything and ask for help rather than guessing.'], recover: true},
  storage: {title: 'Check a drive or free up space', intro: 'Make sure the right drive is connected. A folder at the old place doesn’t mean the drive is there.', steps: ['On the server, list the drives with the commands below.', 'Compare sizes with the drives chosen during setup. Never format a drive to fix this.', 'For low space, remove large files you don’t need. Never delete your only backup.'], extra: 'lsblk -f\nfindmnt\ndf -h'},
  acceptance: {title: 'Check that everything really works', intro: '“Running”, “backed up” and “restore tested” are three different things.', steps: ['Play something in each app, and try your phone if you use one.', 'Run the checker below; it saves a report.', 'Try a test restore with the recovery helper, then record it.'], acceptance: true},
  remote: {title: 'Open your apps on a phone or tablet', intro: 'A phone’s “localhost” is the phone itself, so apps open through Tailscale instead.', steps: ['Install Tailscale on your phone or tablet and sign in to the same account as the server.', 'Open this dashboard at its Tailscale address (setup showed it), then use the app links here.', 'Management apps only open on the server itself.']},
  heartbeat: {title: 'Outside heartbeat', intro: 'An outside service notices when the whole server goes quiet. Optional.', steps: ['Make sure the monitor runs somewhere other than this server.', 'Check the heartbeat service below.', 'Test it once: you should get an alert when it stops, and another when it’s back.'], unit: 'nekoserver-heartbeat.service'},
  notifications: {title: 'Phone notifications', intro: 'Background notifications and this page’s browser alerts are different things.', steps: ['Set up background notifications with setup.', 'Check the service below. Keep private addresses out of screenshots.', 'Browser alerts only work while this page is open.'], unit: 'nekoserver-notifications.service'},
  updates: {title: 'Updates & monthly maintenance', intro: 'Once a month, NekoServer backs up the apps, tests a restore, then updates them.', steps: ['Check its last result below. Apps are briefly offline during the update.', 'If an update was interrupted, let it finish before changing anything else.', 'Big version jumps wait for you to approve them.'], unit: 'nekoserver-maintenance.service'},
  fans: {title: 'Fans not found or not changing', intro: 'NekoServer can only set fans that Linux can control. Laptops and some boards keep fans to themselves.', steps: ['Check the control service with the commands below.', 'No outputs listed? Install the “lm_sensors” package, run “sudo sensors-detect”, accept its suggestions, then restart.', 'Fans plugged into a hub or a case controller may only be visible to that device’s own app.'], unit: 'nekoserver-control.service'},
  lights: {title: 'Lights not found or not changing', intro: 'Lighting goes through OpenRGB, which setup installs and starts by itself.', steps: ['Check the OpenRGB service with the commands below.', 'Memory and motherboard lights need the i2c driver setup switches on: restart the computer once after setup.', 'Some devices aren’t supported by OpenRGB yet: its website lists the ones that are.'], unit: 'nekoserver-openrgb.service'},
  sensors: {title: 'Why a sensor is missing', intro: 'The dashboard finds every sensor Linux reports, automatically.', steps: ['Some motherboards need a driver: for many boards, install the “lm_sensors” package and run “sudo sensors-detect”, then restart.', 'Virtual machines usually have no sensors.', 'NVIDIA graphics need the NVIDIA driver; its readings update once a minute.'], extra: '/usr/local/lib/nekoserver-dashboard/v{version}/nekoserver-runtime hardware'}
};
async function copy(value) {
  try { await navigator.clipboard.writeText(value); toast('Copied. Paste it into Konsole on the server.'); }
  catch { toast('Couldn’t copy: select the command and copy it by hand.'); }
}
function commandBox(command) {
  const box = el('div', 'command-box');
  box.append(el('code', '', command), button('Copy', () => copy(command)));
  return box;
}
function note(message) { return el('p', 'guide-note', message); }
function openDialog(eyebrow, title, children) {
  $('dialog-eyebrow').textContent = eyebrow;
  $('dialog-title').textContent = title;
  put('dialog-content', children);
  if (!$('dialog').open) { $('dialog').showModal(); }
}
/* The commands a guide offers, each only when the installed paths are known. */
function guideCommands(guide, unit) {
  const nodes = [];
  if (unit) { nodes.push(commandBox('systemctl status ' + unit + ' --no-pager\nsudo journalctl -u ' + unit + ' -n 60 --no-pager')); }
  if (guide.extra) { nodes.push(commandBox(guide.extra.replace('{version}', state.status?.version || ''))); }
  if (guide.recover) {
    const cmd = installer(true);
    nodes.push(cmd ? commandBox(cmd) : note('In the saved NekoServer folder on the server, run: sudo bash install.sh --recover'));
  }
  if (guide.acceptance) {
    const script = installedScript('acceptance.py'), config = installedScript('config.json');
    nodes.push(script && config ? commandBox('sudo python3 ' + shellQuote(script) + ' --config ' + shellQuote(config) + ' --profile full --save') : note('In the saved NekoServer folder, run: sudo python3 acceptance.py --profile full --save'));
  }
  const setup = guide.setup ? installer() : null;
  if (setup) { nodes.push(note('Run setup again (it may restart apps):'), commandBox(setup)); }
  return nodes;
}
function showGuide(id, requestedUnit) {
  const guide = GUIDES[id] || GUIDES.general, steps = el('ol');
  for (const step of guide.steps) { steps.append(el('li', '', step)); }
  const listed = list(state.status?.startup).some(u => u.unit === requestedUnit);
  const unit = Object.values(JOB_UNITS).includes(requestedUnit) || listed ? requestedUnit : guide.unit;
  openDialog('GUIDED HELP', guide.title, [el('p', '', guide.intro), steps, ...guideCommands(guide, unit),
    note('Commands only run when you paste them into Konsole on the server. This page never changes anything.')]);
}
const HELP_CARDS = [['collector', 'Readings are old or missing', 'Check the service that collects them.'], ['dashboard', 'Live readings stopped', 'Restart the dashboard safely.'], ['recovery', 'Get files back', 'Restore a backup into a separate folder.'], ['apps', 'An app needs a hand', 'Stopped apps, downtime and setup.'], ['remote', 'Use it on your phone', 'Tailscale, app links and logins.'], ['sensors', 'A sensor is missing', 'Why, and how to add one.'], ['fans', 'Fans don’t change', 'Find your fans and check fan control.'], ['lights', 'Lights don’t change', 'Check OpenRGB and the i2c driver.'], ['acceptance', 'Check it really works', 'Playback, backups and a test restore.'], ['updates', 'About updates', 'Monthly updates and big versions.']];
function helpCard([id, title, description]) {
  const card = el('article', 'card');
  card.append(el('h3', '', title), el('p', '', description), keyed(button('Show me how →', () => showGuide(id), 'text-button'), 'help-' + id));
  return card;
}
function errorRow([code, [title, fix]]) {
  const row = el('div', 'row'), body = el('div');
  body.append(el('h3', '', title), el('p', '', fix));
  row.append(el('span', 'code', code), body);
  return row;
}
function renderHelp() {
  put('help-cards', HELP_CARDS.map(helpCard));
  put('error-codes', Object.entries(ERRORS).map(errorRow));
  put('install-help', [installHelp()]);
}
/* How to install the dashboard as an app on the device being used. */
const INSTALL_STEPS = [
  [ua => /iPhone|iPad|iPod/.test(ua) || (/Macintosh/.test(ua) && navigator.maxTouchPoints > 1), 'On iPhone or iPad: tap Share (□↑) in Safari, then “Add to Home Screen”.'],
  [ua => /Android/.test(ua), 'On Android: open the browser menu (⋮) and choose “Install app” or “Add to Home screen”.'],
  [ua => /Firefox/.test(ua), 'Firefox can’t install sites as apps. On the server, open “NekoServer Dashboard” from the app menu: it opens in its own window. Elsewhere, bookmark this page or use Chrome, Edge or Safari to install it.']
];
async function installNow() {
  state.installPrompt.prompt();
  await state.installPrompt.userChoice.catch(() => null);
  state.installPrompt = null;
  renderHelp();
}
function installHelp() {
  const box = el('div', 'install'), ua = navigator.userAgent;
  if (matchMedia('(display-mode: standalone)').matches || navigator.standalone === true) {
    box.append(el('p', '', 'You’re using the NekoServer app. ✓'));
    return box;
  }
  if (state.installPrompt) {
    box.append(el('p', '', 'Add NekoServer to your home screen or app list. It opens in its own window, without browser bars.'), button('Install the app', installNow, 'button'));
  } else {
    const step = INSTALL_STEPS.find(([matches]) => matches(ua));
    box.append(el('p', '', step ? step[1] : 'In Chrome or Edge, use the install icon in the address bar (or menu → “Install NekoServer”). On the server itself, “NekoServer Dashboard” is in the app menu.'));
  }
  if (!window.isSecureContext) { box.append(hint('Installing needs the secure Tailscale address (https://…) or this computer’s own address.')); }
  return box;
}
function tally(rows) {
  const out = {};
  for (const r of rows) {
    const k = Object.hasOwn(LABELS, r.status) ? r.status : 'unknown';
    out[k] = (out[k] || 0) + 1;
  }
  return out;
}
function hardwareCounts() {
  const live = state.live;
  if (!live) { return null; }
  return {sensors: list(live.sensors).length, gpus: list(live.gpus).length, npus: list(live.npus).length, lights: text(state.control?.lights?.status, 'not checked')};
}
function diagnostics() {
  const s = state.status;
  if (!s) { toast('Wait for the first reading.'); return; }
  // A deliberate allowlist: counts and versions, never the whole snapshot.
  const summary = {
    format: 'neko-dashboard-support-v2', server_version: /^\d+\.\d+\.\d+$/.test(s.version) ? s.version : 'unknown',
    exported_at: new Date().toISOString(), readings_current: !statusStale(), problem: problem(),
    apps: tally(serverApps(s).map(a => ({status: appState(a)}))), backups: tally(s.backups),
    drives: {connected: s.storage.filter(d => d.status === 'mounted').length, other: s.storage.filter(d => d.status !== 'mounted').length},
    integrity: {damaged_or_missing: s.integrity.reduce((n, c) => n + integrityBad(c), 0), unchecked: s.integrity.reduce((n, c) => n + count(c.unverified), 0)},
    startup_off: s.startup.filter(u => !u.enabled).map(u => text(u.unit)),
    hardware: hardwareCounts(),
    note: 'Counts only. Read before sharing.'
  };
  const blob = new Blob([JSON.stringify(summary, null, 2) + '\n'], {type: 'application/json'}), url = URL.createObjectURL(blob), a = el('a');
  a.href = url;
  a.download = 'nekoserver-summary-' + new Date().toISOString().slice(0, 10) + '.json';
  document.body.append(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
  toast('Summary downloaded. Read it before sharing.');
}

/* ---------- settings ---------- */
function systemTheme() { return matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'; }
function applyPreferences() {
  let theme = readStore('theme', 'dark');
  if (!['light', 'dark', 'system'].includes(theme)) { theme = 'dark'; }
  document.documentElement.dataset.theme = theme === 'system' ? systemTheme() : theme;
  const size = readStore('size', 'normal');
  document.body.classList.toggle('large', size === 'large');
  document.body.classList.toggle('tv', size === 'tv');
}
function choice(label, key, options, fallback) {
  const wrap = el('label', 'field', label), select = el('select');
  for (const [value, name] of options) {
    const option = el('option', '', name);
    option.value = value;
    select.append(option);
  }
  select.value = readStore(key, fallback);
  select.addEventListener('change', () => { saveStore(key, select.value); applyPreferences(); });
  wrap.append(select);
  return wrap;
}
async function toggleAlerts() {
  if (readStore('notify', false)) { saveStore('notify', false); preferences(); return; }
  if (!('Notification' in window)) { toast('This browser can’t show alerts.'); return; }
  const permission = await Notification.requestPermission().catch(() => 'denied');
  if (permission !== 'granted') { toast('Alerts were not allowed.'); return; }
  saveStore('notify', true);
  toast('Alerts on while this page is open.');
  preferences();
}
function alertsOn() { return 'Notification' in window && Notification.permission === 'granted' && readStore('notify', false) === true; }
function preferences() {
  openDialog('SETTINGS', 'Make yourself at home', [
    el('p', '', 'Saved in this browser only. Nothing on the server changes.'),
    choice('Appearance', 'theme', [['dark', 'Neko night'], ['light', 'Soft daylight'], ['system', 'Same as this device']], 'dark'),
    choice('Text size', 'size', [['normal', 'Normal'], ['large', 'Large'], ['tv', 'TV (across the room)']], 'normal'),
    el('p', '', 'Browser alerts tell you about urgent problems while this page is open on this device.'),
    button(alertsOn() ? 'Turn off browser alerts' : 'Turn on browser alerts', toggleAlerts),
    el('h3', 'sub-head', 'Use it like an app'), installHelp()
  ]);
}
function notify() {
  if (statusStale() || !alertsOn()) { return; }
  const urgent = state.issues.filter(i => i.level === 'critical');
  for (const id of notified) { // deleting while iterating a Set is safe
    if (!urgent.some(i => i.id === id)) { notified.delete(id); }
  }
  for (const issue of urgent.slice(0, 3).filter(i => !notified.has(i.id))) {
    notified.add(issue.id);
    try { new Notification('NekoServer · ' + issue.title, {body: 'Open the dashboard for what to do.', tag: 'neko-' + issue.id}); } catch { /* optional */ }
  }
}
let toastTimer;
function toast(message) {
  clearTimeout(toastTimer);
  $('toast').hidden = false;
  $('toast').textContent = message;
  toastTimer = setTimeout(() => { $('toast').hidden = true; }, 4500);
}
function moreMenu() {
  const nav = el('nav', 'more-list');
  nav.setAttribute('aria-label', 'All pages');
  for (const [id, name] of Object.entries(ROUTES)) {
    const a = el('a', id === state.route ? 'nav active' : 'nav', name);
    a.href = '#' + id;
    a.addEventListener('click', () => $('dialog').close());
    nav.append(a);
  }
  openDialog('NEKOSERVER', 'All pages', [nav, button('Settings', preferences)]);
}

/* ---------- routing & rendering ---------- */
function markActive() {
  for (const a of document.querySelectorAll('[data-route]')) {
    const active = a.dataset.route === state.route;
    a.classList.toggle('active', active);
    if (active) { a.setAttribute('aria-current', 'page'); } else { a.removeAttribute('aria-current'); }
  }
  for (const n of document.querySelectorAll('[data-page]')) { n.hidden = !n.dataset.page.split(' ').includes(state.route); }
  for (const n of document.querySelectorAll('[data-only]')) { n.hidden = n.dataset.only !== state.route; }
}
function route() {
  const hash = location.hash.slice(1);
  if (hash === 'main') { return; }
  const errors = hash === 'help-errors';
  state.route = errors ? 'help' : hash;
  if (!Object.hasOwn(ROUTES, state.route)) { state.route = 'home'; }
  $('page-title').textContent = ROUTES[state.route];
  document.title = 'NekoServer · ' + ROUTES[state.route];
  markActive();
  if (errors) { requestAnimationFrame(() => $('help-errors').scrollIntoView()); } else { window.scrollTo({top: 0, behavior: 'instant'}); }
  render();
  poll(true);
}
const PAGES = {
  home: () => { renderHome(); renderAttention(); renderApps(); },
  apps: renderApps, attention: renderAttention, backups: renderBackups, hardware: renderHardware,
  sensors: renderSensors, cooling: renderCooling, processes: renderProcesses, health: renderHealth, help: renderHelp
};
function render() {
  if (state.status) { state.issues = collectIssues(state.status); }
  withFocus(() => {
    renderChrome();
    PAGES[state.route]();
  });
  notify();
}

/* ---------- polling ---------- */
const timers = {};
function every(kind, ms, wanted) {
  clearInterval(timers[kind]);
  if (!wanted) { return; }
  void load(kind);
  timers[kind] = setInterval(() => {
    if (!document.hidden) { void load(kind); }
  }, ms);
}
function poll(routeChanged = false) {
  if (!routeChanged && timers.status) { return; }
  every('status', 15000, true);
  every('live', 4000, LIVE_ROUTES.has(state.route));
  every('processes', 5000, state.route === 'processes');
  every('control', 3000, state.route === 'cooling');
}
function refreshNow() {
  void load('status');
  if (LIVE_ROUTES.has(state.route)) { void load('live'); }
  if (state.route === 'processes') { void load('processes'); }
  if (state.route === 'cooling') { void load('control'); }
}

/* ---------- start ---------- */
function chooseFilter(chosen) {
  state.appFilter = chosen.dataset.filter;
  for (const n of document.querySelectorAll('[data-filter]')) { n.setAttribute('aria-pressed', String(n === chosen)); }
  withFocus(renderApps);
}
function turnProcessPage(change) { state.processPage = change(state.processPage); renderProcesses(); }
window.addEventListener('hashchange', route);
$('app-search').addEventListener('input', () => withFocus(renderApps));
for (const b of document.querySelectorAll('[data-filter]')) { b.addEventListener('click', () => chooseFilter(b)); }
for (const b of document.querySelectorAll('[data-guide]')) { b.addEventListener('click', () => showGuide(b.dataset.guide)); }
$('refresh').addEventListener('click', refreshNow);
$('preferences').addEventListener('click', preferences);
$('more').addEventListener('click', moreMenu);
$('export').addEventListener('click', diagnostics);
$('close-dialog').addEventListener('click', () => $('dialog').close());
$('dialog').addEventListener('click', e => {
  if (e.target === $('dialog')) { $('dialog').close(); }
});
$('process-search').addEventListener('input', () => turnProcessPage(() => 0));
$('process-sort').addEventListener('change', () => turnProcessPage(() => 0));
$('process-prev').addEventListener('click', () => turnProcessPage(p => p - 1));
$('process-next').addEventListener('click', () => turnProcessPage(p => p + 1));
$('problem-link').addEventListener('click', e => { e.preventDefault(); location.hash = 'help-errors'; });
matchMedia('(prefers-color-scheme: dark)').addEventListener('change', applyPreferences);
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) { refreshNow(); }
});
window.addEventListener('beforeinstallprompt', e => {
  e.preventDefault();
  state.installPrompt = e;
  if (state.route === 'help') { renderHelp(); }
});
if ('serviceWorker' in navigator && window.isSecureContext) {
  navigator.serviceWorker.register('sw.js').catch(() => { /* the page works without it */ });
}
setInterval(() => {
  if (!document.hidden) { renderChrome(); } // "x min ago" and staleness move on their own
}, 30000);

applyPreferences();
route();
