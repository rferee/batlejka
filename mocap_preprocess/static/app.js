// mocap-preprocess UI. Plain ES modules, no build step.
import { loadSetup } from './setup.js';
import { SessionScreen } from './screens/session.js';
import { ProcessScreen } from './screens/process.js';
import { CamerasScreen } from './screens/cameras.js';
import { SyncScreen } from './screens/sync.js';
import { ReviewScreen } from './screens/review.js';
import { ExportScreen } from './screens/export.js';
import { CalibrateScreen } from './screens/calibrate.js';
import { MocapScreen } from './mocap.js';

// ------------------------------------------------------------------ helpers
export function h(tag, props = null, ...kids) {
  const el = document.createElement(tag);
  if (props) {
    for (const [k, v] of Object.entries(props)) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'style' && typeof v === 'object') Object.assign(el.style, v);
      else if (k.startsWith('on') && typeof v === 'function') el.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === 'value') el.value = v;
      else if (k === 'checked' || k === 'disabled' || k === 'selected') el[k] = !!v;
      else el.setAttribute(k, v === true ? '' : v);
    }
  }
  for (const kid of kids.flat(Infinity)) {
    if (kid == null || kid === false) continue;
    el.append(kid instanceof Node ? kid : String(kid));
  }
  return el;
}
export const icon = (name, cls = '') => h('i', { class: `fa-solid fa-${name} ${cls}`.trim() });
/** A setting or legend name with a 1–2 sentence description on hover / focus (dotted underline). */
export const tip = (label, text, props = {}) => h('span', { ...props, class: `tip ${props.class || ''}`.trim(), 'data-tip': text, tabindex: '0' }, label);
export const pad2 = (n) => String(n).padStart(2, '0');

export function fpsFloat(s) {
  if (s == null) return NaN;
  s = String(s);
  if (s.includes('/')) {
    const [a, b] = s.split('/').map(Number);
    return b ? a / b : NaN;
  }
  return Number(s);
}
export function fpsLabel(s) {
  const v = fpsFloat(s);
  if (!isFinite(v)) return '?';
  if (Math.abs(v - Math.round(v)) < 1e-6) return String(Math.round(v));
  return v.toFixed(3).replace(/0+$/, '').replace(/\.$/, '');
}
export function fmtClock(t) {
  const neg = t < 0;
  t = Math.abs(t);
  const s = `${Math.floor(t / 3600)}:${pad2(Math.floor(t / 60) % 60)}:${pad2(Math.floor(t) % 60)}`;
  return neg ? '-' + s : s;
}
export function fmtTC(t, fps) {
  const f = fpsFloat(fps) || 30;
  const neg = t < 0;
  t = Math.abs(t) + 1e-7;
  const s = `${fmtClock(t)}:${pad2(Math.min(Math.ceil(f) - 1, Math.floor((t % 1) * f)))}`;
  return neg ? '-' + s : s;
}
export function fmtDur(s) {
  if (s == null || !isFinite(s)) return '—';
  if (s < 60) return s.toFixed(1) + 's';
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}:${pad2(Math.floor(s - m * 60))}`;
  return fmtClock(s);
}
export const fmtOffset = (s) => (s == null ? '—' : `${s >= 0 ? '+' : '−'}${Math.abs(s).toFixed(3)} s`);
export function fmtBytes(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}
export function fmtDate(epoch) {
  if (!epoch) return '—';
  const d = new Date(epoch * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}
export const basename = (rel) => rel.split('/').pop();

export const prefs = {
  get(k, d) {
    try {
      const v = localStorage.getItem('mocap.' + k);
      return v == null ? d : JSON.parse(v);
    } catch { return d; }
  },
  set(k, v) {
    try { localStorage.setItem('mocap.' + k, JSON.stringify(v)); } catch { /* private mode */ }
  },
};

// ------------------------------------------------------------------ API
export async function api(method, url, body) {
  const opts = { method, headers: {} };
  if (body !== undefined) {
    opts.headers['Content-Type'] = 'application/json';
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  let data = null;
  try { data = await r.json(); } catch { /* empty body */ }
  if (!r.ok) {
    const d = data && data.detail;
    const err = new Error(d ? (typeof d === 'string' ? d : JSON.stringify(d)) : `${r.status} ${r.statusText}`);
    err.status = r.status;
    err.detail = d;
    throw err;
  }
  return data;
}
export const GET = (u) => api('GET', u);
export const POST = (u, b = {}) => api('POST', u, b);
export const PUT = (u, b) => api('PUT', u, b);
const enc = encodeURIComponent;
export const sUrl = (path = '') => `/api/sessions/${enc(S.name)}${path}`;
export const mediaUrl = (kind, f, ext) => `/media/${enc(S.name)}/${kind}/${f.id}.${ext}?v=${Math.round((f.mtime || 0) * 1000)}`;

// ------------------------------------------------------------------ toasts / modals
export function toast(msg, kind = '') {
  const el = h('div', { class: `toast ${kind}` }, msg);
  document.getElementById('toasts').append(el);
  setTimeout(() => el.remove(), kind === 'error' ? 7000 : 4000);
}
export function confirmModal({ title, body, ok = 'OK', cancel = 'Cancel' }) {
  return new Promise((resolve) => {
    const root = document.getElementById('modal-root');
    const close = (v) => { root.innerHTML = ''; document.removeEventListener('keydown', onKey, true); resolve(v); };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); close(false); }
      if (e.key === 'Enter') { e.stopPropagation(); close(true); }
    };
    document.addEventListener('keydown', onKey, true);
    root.append(h('div', { class: 'modal-back', onclick: (e) => e.target === e.currentTarget && close(false) },
      h('div', { class: 'modal' },
        h('h2', null, title),
        body ? h('p', null, body) : null,
        h('div', { class: 'row' },
          h('button', { class: 'btn', onclick: () => close(false) }, cancel),
          h('button', { class: 'btn primary', onclick: () => close(true) }, ok)))));
  });
}

// ------------------------------------------------------------------ state
export const S = {
  health: null,
  sessions: [],
  name: prefs.get('session', null),
  data: null,
  step: prefs.get('step', 'session'),
  jobs: [],
  logOpen: false,
  menuOpen: false,
};
export const isActive = (j) => j && (j.status === 'queued' || j.status === 'running');
export const activeJob = (kind) => S.jobs.find((j) => j.kind === kind && isActive(j));
export const lastJob = (kind) => [...S.jobs].reverse().find((j) => j.kind === kind);
export const cameraById = (id) => (S.data?.cameras || []).find((c) => c.id === id);
export const videoFiles = () => (S.data?.files || []).filter((f) => f.probe && f.probe.video);
export const targetFps = () => S.data?.settings?.target_fps || '30000/1001';

export const METHOD_COLORS = {
  reference: '#97d47d', audio: '#d3d3d3', search: '#E89768', metadata: '#E89768', none: '#E56767', manual: '#8880F0',
};
export const METHOD_LABEL = {
  reference: 'reference', audio: 'audio match', search: 'full search (check)', metadata: 'camera clock (check)', none: 'unplaced', manual: 'manual',
};

// ------------------------------------------------------------------ header
const STEPS = [
  { id: 'session', label: 'Home' },
  { id: 'process', label: 'Process' },
  { id: 'cameras', label: 'Cameras' },
  { id: 'sync', label: 'Sync' },
  { id: 'review', label: 'Cut Takes' },
  { id: 'export', label: 'Export' },
  { id: 'calibrate', label: 'Calibrate' },
  { id: 'mocap', label: 'Mocap' },
];

function stepEnabled(id) {
  if (id === 'session') return true;
  if (!S.data) return false;
  if (id === 'process') return true;
  if (id === 'mocap' || id === 'calibrate') return !!S.data.state.exported_at;
  return !!S.data.state.processed_at && videoFiles().length > 0;
}

function stepDone(id) {
  const st = S.data?.state || {};
  switch (id) {
    case 'session': return !!S.data;
    case 'process': return !!st.processed_at;
    case 'cameras': return !!st.processed_at && (S.data?.cameras || []).length > 0;
    case 'sync': return !!st.synced_at;
    case 'review': return (S.data?.takes || []).length > 0;
    case 'export': return !!st.exported_at;
    case 'calibrate': return (S.data?.mocap?.profiles || []).some((p) => p.status === 'calibrated') || !!S.data?.mocap?.calibration;
    case 'mocap': return Object.values(S.data?.mocap?.takes || {}).some((t) => t.status === 'done');
    default: return false;
  }
}

function renderSteps() {
  const nav = document.getElementById('steps');
  nav.innerHTML = '';
  for (const s of STEPS) {
    const active = S.step === s.id;
    nav.append(h('button', {
      class: `${active ? 'active' : ''} ${stepDone(s.id) && !active ? 'done' : ''}`,
      disabled: !stepEnabled(s.id),
      title: stepEnabled(s.id) ? '' : (s.id === 'mocap' || s.id === 'calibrate') ? 'Export a take first' : 'Process the source files first',
      onclick: () => go(s.id),
    }, s.label, stepDone(s.id) ? icon('check') : null));
  }
}

function renderJobPill() {
  const box = document.getElementById('jobpill');
  box.innerHTML = '';
  const active = S.jobs.filter(isActive);
  const last = S.jobs[S.jobs.length - 1];
  if (!active.length && !last) return;
  const j = active[0] || last;
  const label = active.length > 1 ? `${j.label} (+${active.length - 1})` : j.label;
  let status = '';
  if (isActive(j)) status = `${Math.round(j.progress * 100)}%`;
  else status = j.status;
  const statusCls = j.status === 'error' ? 'err' : j.status === 'done' ? 'ok' : j.status === 'cancelled' ? 'warn' : 'run';
  box.append(h('button', { class: 'jobpill', title: 'Show job log', onclick: toggleLog },
    isActive(j) ? icon('spinner', 'fa-spin') : icon('list-check'),
    h('span', null, 'Queue'),
    h('span', { class: 'vdiv', 'aria-hidden': 'true' }),
    h('span', { class: `sq ${statusCls}`, 'aria-hidden': 'true' }),
    h('span', { class: 'ellipsis' }, label, ' ', h('span', { class: statusCls }, status)),
    isActive(j) ? h('span', { class: 'bar' }, h('div', { style: { width: `${j.progress * 100}%` } })) : null,
    isActive(j) ? h('span', {
      class: 'x', title: 'Cancel', onclick: (e) => { e.stopPropagation(); cancelJob(j.id); },
    }, icon('xmark')) : null));
}

export async function cancelJob(id) {
  try { await POST(`/api/jobs/${id}/cancel`); } catch (e) { toast(e.message, 'error'); }
  pollSoon();
}

export const jobLogs = new Map(); // id -> {seq, lines}
async function renderJobLog() {
  const box = document.getElementById('joblog');
  box.hidden = !S.logOpen;
  if (!S.logOpen) return;
  const recent = [...S.jobs].reverse().slice(0, 8);
  await Promise.all(recent.map(async (j) => {
    const have = jobLogs.get(j.id) || { seq: 0, lines: [] };
    if (have.seq >= j.log_seq && have.status === j.status) return;
    try {
      const full = await GET(`/api/jobs/${j.id}?log_since=${have.seq}`);
      have.lines.push(...full.log);
      have.seq = full.log_seq;
      have.status = full.status;
      jobLogs.set(j.id, have);
    } catch { /* job pruned */ }
  }));
  box.innerHTML = '';
  box.append(h('div', { class: 'section-title' }, 'Jobs',
    h('button', { class: 'mini-btn', title: 'Close', onclick: toggleLog }, icon('xmark'))));
  if (!recent.length) box.append(h('div', { class: 'muted small' }, 'No jobs yet.'));
  for (const j of recent) {
    const lines = (jobLogs.get(j.id)?.lines || []).slice(-200);
    const logEl = h('div', { class: 'job-log' },
      lines.map((l) => h('div', { class: `l-${l.level}` }, l.msg)));
    box.append(h('div', { class: 'job' },
      h('div', { class: 'job-head' },
        h('b', null, j.label),
        h('span', { class: j.status === 'error' ? 'err' : j.status === 'done' ? 'ok' : 'muted' }, j.status),
        h('span', { class: 'muted ellipsis', style: { flex: 1 } }, j.message || ''),
        isActive(j) ? h('button', { class: 'btn xs danger', onclick: () => cancelJob(j.id) }, 'Cancel') : null),
      isActive(j) ? h('div', { class: 'progress thin' }, h('div', { style: { width: `${j.progress * 100}%` } })) : null,
      j.error ? h('div', { class: 'err small' }, j.error) : null,
      lines.length ? logEl : null));
    requestAnimationFrame(() => { logEl.scrollTop = logEl.scrollHeight; });
  }
}
function toggleLog() {
  S.logOpen = !S.logOpen;
  renderJobLog();
}

function renderSessionSwitch() {
  const box = document.getElementById('sesswitch');
  box.innerHTML = '';
  const btn = h('button', { title: 'Switch session', onclick: (e) => { e.stopPropagation(); S.menuOpen = !S.menuOpen; renderSessionSwitch(); } },
    h('span', null, h('span', { class: 'pre' }, 'sessions/'), h('b', null, S.name || '—')), icon('chevron-down', 'dim'));
  box.append(btn);
  if (!S.menuOpen) return;
  const menu = h('div', { class: 'menu', onclick: (e) => e.stopPropagation() });
  for (const s of S.sessions) {
    menu.append(h('button', {
      class: s.name === S.name ? 'sel' : '',
      onclick: () => { S.menuOpen = false; openSession(s.name); },
    }, icon(s.name === S.name ? 'square-check' : 'folder', 'dim'), s.name,
    h('span', { class: 'spacer' }), h('span', { class: 'dim small' }, `${s.n_source_files} files`)));
  }
  menu.append(h('hr'),
    h('button', { onclick: () => { S.menuOpen = false; go('session'); SessionScreen.focusNew = true; render(); } }, icon('plus'), 'New session…'),
    S.name ? h('button', { onclick: () => { S.menuOpen = false; reveal('session'); } }, icon('folder-open'), 'Open session folder') : null,
    h('button', { onclick: () => { S.menuOpen = false; loadSessions(); } }, icon('rotate'), 'Rescan sessions'));
  box.append(menu);
}
document.addEventListener('click', () => {
  if (S.menuOpen) { S.menuOpen = false; renderSessionSwitch(); }
});

function renderBanner() {
  const box = document.getElementById('banner');
  box.innerHTML = '';
  if (S.health && (!S.health.ffmpeg || !S.health.ffprobe)) {
    box.append(h('div', { class: 'banner error' }, icon('triangle-exclamation'),
      'ffmpeg/ffprobe not found on PATH. Install ffmpeg or set MOCAP_FFMPEG / MOCAP_FFPROBE, then restart.'));
  }
}

export async function reveal(what) {
  try { await POST(sUrl('/reveal'), { what }); } catch (e) { toast(e.message, 'error'); }
}

/** Part of what the UI remembers for this session (session.json "ui", so every browser agrees):
 *  applied at once, saved in the background. */
export function putUi(patch) {
  if (!S.data) return;
  S.data.ui = { ...(S.data.ui || {}), ...patch };
  PUT(sUrl('/ui'), patch).catch((e) => toast(`Couldn't save the UI state: ${e.message}`, 'error'));
}

// ------------------------------------------------------------------ navigation / data
const screens = {};
let mounted = null;

export function go(step) {
  if (!stepEnabled(step)) step = S.data ? 'process' : 'session';
  S.step = step;
  prefs.set('step', step);
  updateHash();
  render();
}

function updateHash() {
  const h = S.name ? `#/${enc(S.name)}/${S.step}` : '#/';
  if (location.hash !== h) history.replaceState(null, '', h);
}
function parseHash() {
  const m = /^#\/([^/]+)(?:\/([a-z]+))?/.exec(location.hash || '');
  return m ? { name: decodeURIComponent(m[1]), step: m[2] || null } : null;
}
window.addEventListener('hashchange', async () => {
  const want = parseHash();
  if (!want) return;
  if (want.name !== S.name && S.sessions.some((s) => s.name === want.name)) await openSession(want.name);
  if (want.step && want.step !== S.step && STEPS.some((s) => s.id === want.step)) go(want.step);
});

export function render() {
  document.querySelector('header.top').classList.toggle('full', S.step === 'review');   // Review is full bleed
  renderSteps();
  renderJobPill();
  renderSessionSwitch();
  renderBanner();
  const root = document.getElementById('main');
  const screen = screens[S.step];
  if (mounted !== screen) {
    if (mounted && mounted.unmount) mounted.unmount();
    root.innerHTML = '';
    mounted = screen;
    screen.mount(root);
  } else if (screen && screen.update) {
    screen.update();
  }
}

export async function loadSessions() {
  try {
    S.sessions = await GET('/api/sessions');
  } catch (e) { toast(e.message, 'error'); }
  render();
}

export async function refreshSession() {
  if (!S.name) return;
  try {
    S.data = await GET(sUrl());
  } catch (e) {
    if (e.status === 404 || e.status === 400) {
      S.name = null;
      S.data = null;
      prefs.set('session', null);
    }
    toast(e.message, 'error');
  }
  if (!stepEnabled(S.step)) S.step = S.data ? 'process' : 'session';
  updateHash();
  render();
}

export function applySession(data) {
  S.data = data;
  render();
}

export async function openSession(name, { scan = true } = {}) {
  if (name !== S.name) {
    // flush the old session's pending edits while sUrl() still points at it
    if (mounted && mounted.sessionChanged) mounted.sessionChanged();
    S.data = null;
    S.jobs = [];
    jobLogs.clear();
  }
  S.name = name;
  prefs.set('session', name);
  await refreshSession();
  if (scan) await startJob('/scan');
  pollSoon();
}

export async function startJob(path, body = {}) {
  try {
    const job = await POST(sUrl(path), body);
    S.jobs = S.jobs.filter((j) => j.id !== job.id).concat([job]);
    render();
    pollSoon();
    return job;
  } catch (e) {
    toast(e.message, 'error');
    return null;
  }
}

// ------------------------------------------------------------------ job polling
let pollTimer = null;
let lastProxyRefresh = 0;
export function pollSoon() {
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollJobs, 150);
}
const jobListeners = [];
/** Call ``fn(jobs, finished)`` after every poll (finished = jobs that just stopped running). */
export function onJobs(fn) { jobListeners.push(fn); }

async function pollJobs() {
  clearTimeout(pollTimer);
  let next = 2500;
  try {
    const name = S.name;
    const list = await GET(name ? `/api/jobs?session=${enc(name)}` : '/api/jobs?global_only=1');
    if (name !== S.name) { pollTimer = setTimeout(pollJobs, 150); return; }
    const finished = [];
    const prev = new Map(S.jobs.map((j) => [j.id, j]));
    let refresh = false;
    for (const j of list) {
      const p = prev.get(j.id);
      if (p && isActive(p) && !isActive(j)) {
        refresh = true;
        finished.push(j);
        if (j.status === 'error') toast(`${j.label} failed: ${j.error}`, 'error');
        else if (j.status === 'done' && !['proxies', 'scan', 'resync', 'previews'].includes(j.kind)) toast(`${j.label} done`, 'ok');
      }
      if (j.kind === 'proxies' && isActive(j) && p && p.log_seq !== j.log_seq && Date.now() - lastProxyRefresh > 2000) {
        lastProxyRefresh = Date.now();
        refresh = true;
      }
    }
    S.jobs = list;
    if (list.some(isActive)) next = 600;
    renderJobPill();
    if (S.logOpen) renderJobLog();
    if (mounted && mounted.onJobs) mounted.onJobs();
    for (const fn of jobListeners) {
      try { fn(list, finished); } catch (e) { console.error(e); }
    }
    if (refresh) await refreshSession();
  } catch { next = 4000; }
  pollTimer = setTimeout(pollJobs, next);
}

export const fileById = (id) => (S.data?.files || []).find((f) => f.id === id);
export function nextFree(prefix, used) {
  let k = 1;
  while (used.has(`${prefix}${k}`)) k += 1;
  return `${prefix}${k}`;
}
export const isMounted = (screen) => mounted === screen;

// ------------------------------------------------------------------ screens
Object.assign(screens, {
  session: SessionScreen, process: ProcessScreen, cameras: CamerasScreen, sync: SyncScreen,
  review: ReviewScreen, export: ExportScreen, calibrate: CalibrateScreen, mocap: MocapScreen,
});

// ------------------------------------------------------------------ tooltips
// One floating tooltip for every [data-tip] element: never clipped by scrolling tables or cards.
const tipBox = h('div', { class: 'tooltip', role: 'tooltip', hidden: true });
document.body.append(tipBox);
let tipFor = null;
function showTip(el) {
  tipFor = el;
  tipBox.textContent = el.dataset.tip;
  tipBox.hidden = false;
  const r = el.getBoundingClientRect();
  const w = tipBox.offsetWidth;
  const hgt = tipBox.offsetHeight;
  const left = Math.max(8, Math.min(window.innerWidth - w - 8, r.left + r.width / 2 - w / 2));
  const top = r.bottom + 8 + hgt > window.innerHeight ? r.top - hgt - 8 : r.bottom + 8;
  tipBox.style.left = `${left}px`;
  tipBox.style.top = `${top}px`;
}
function hideTip() { tipFor = null; tipBox.hidden = true; }
document.addEventListener('mouseover', (e) => {
  const el = e.target.closest?.('[data-tip]');
  if (el && el !== tipFor) showTip(el);
  else if (!el && tipFor) hideTip();
});
document.addEventListener('focusin', (e) => { const el = e.target.closest?.('[data-tip]'); if (el) showTip(el); });
document.addEventListener('focusout', hideTip);
document.addEventListener('scroll', hideTip, true);
document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && tipFor) hideTip(); });

// ------------------------------------------------------------------ placeholder screens (filled in later milestones)
for (const id of ['cameras', 'sync', 'review']) {
  if (!screens[id]) {
    screens[id] = {
      mount(root) { root.append(h('main', { class: 'page' }, h('div', { class: 'empty' }, 'Coming up.'))); },
    };
  }
}

// ------------------------------------------------------------------ boot
async function boot() {
  try { S.health = await GET('/api/health'); } catch { S.health = null; }
  const fromHash = parseHash();
  if (fromHash) {
    S.name = fromHash.name;
    if (fromHash.step && STEPS.some((s) => s.id === fromHash.step)) S.step = fromHash.step;
  }
  // nothing is rendered before the session is open: a screen mounted without S.data starts wrong
  try { S.sessions = await GET('/api/sessions'); } catch (e) { toast(e.message, 'error'); }
  if (S.name && S.sessions.some((s) => s.name === S.name)) {
    await openSession(S.name);
  } else {
    S.name = null;
    S.step = 'session';
    render();
  }
  loadSetup();     // not awaited: the Setup card fills in when it's ready
  pollSoon();
}
boot();
