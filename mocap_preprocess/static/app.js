// mocap-preprocess UI. Plain ES modules, no build step.
import { applyFit, Grid } from './grid.js';
import { Timeline } from './timeline.js';
import { SyncViz, snapshotFromSession, replayFrames } from './syncviz.js';

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
const pad2 = (n) => String(n).padStart(2, '0');

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
function fmtBytes(n) {
  if (!n) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  const i = Math.min(u.length - 1, Math.floor(Math.log(n) / Math.log(1024)));
  return `${(n / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}
function fmtDate(epoch) {
  if (!epoch) return '—';
  const d = new Date(epoch * 1000);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}
const basename = (rel) => rel.split('/').pop();

const prefs = {
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
async function api(method, url, body) {
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
const isActive = (j) => j && (j.status === 'queued' || j.status === 'running');
export const activeJob = (kind) => S.jobs.find((j) => j.kind === kind && isActive(j));
export const lastJob = (kind) => [...S.jobs].reverse().find((j) => j.kind === kind);
export const cameraById = (id) => (S.data?.cameras || []).find((c) => c.id === id);
export const videoFiles = () => (S.data?.files || []).filter((f) => f.probe && f.probe.video);
export const targetFps = () => S.data?.settings?.target_fps || '30000/1001';

export const METHOD_COLORS = {
  reference: '#97d47d', audio: '#d3d3d3', search: '#E89768', metadata: '#E2B35B', none: '#E56767', manual: '#8880F0',
};

// ------------------------------------------------------------------ header
const STEPS = [
  { id: 'session', label: 'Session' },
  { id: 'process', label: 'Process' },
  { id: 'cameras', label: 'Cameras' },
  { id: 'sync', label: 'Sync' },
  { id: 'review', label: 'Review' },
  { id: 'export', label: 'Export' },
];

function stepEnabled(id) {
  if (id === 'session') return true;
  if (!S.data) return false;
  if (id === 'process') return true;
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
      title: stepEnabled(s.id) ? '' : 'Process the source files first',
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
  const statusCls = j.status === 'error' ? 'err' : j.status === 'done' ? 'ok' : j.status === 'cancelled' ? 'warn' : '';
  box.append(h('button', { class: `jobpill ${isActive(j) ? '' : 'idle'}`, title: 'Show job log', onclick: toggleLog },
    isActive(j) ? icon('spinner', 'fa-spin') : icon('list-check'),
    h('span', { class: 'ellipsis' }, label),
    isActive(j) ? h('span', { class: 'bar' }, h('div', { style: { width: `${j.progress * 100}%` } })) : null,
    h('span', { class: statusCls }, status),
    isActive(j) ? h('span', {
      class: 'x', title: 'Cancel', onclick: (e) => { e.stopPropagation(); cancelJob(j.id); },
    }, icon('xmark')) : null));
}

async function cancelJob(id) {
  try { await POST(`/api/jobs/${id}/cancel`); } catch (e) { toast(e.message, 'error'); }
  pollSoon();
}

const jobLogs = new Map(); // id -> {seq, lines}
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
    h('span', null, 'sessions/', h('b', null, S.name || '—')), icon('chevron-down', 'dim'));
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
async function pollJobs() {
  clearTimeout(pollTimer);
  let next = 2500;
  if (S.name) {
    try {
      const list = await GET(`/api/jobs?session=${enc(S.name)}`);
      const prev = new Map(S.jobs.map((j) => [j.id, j]));
      let refresh = false;
      for (const j of list) {
        const p = prev.get(j.id);
        if (p && isActive(p) && !isActive(j)) {
          refresh = true;
          if (j.status === 'error') toast(`${j.label} failed: ${j.error}`, 'error');
          else if (j.status === 'done' && !['proxies', 'scan', 'resync'].includes(j.kind)) toast(`${j.label} done`, 'ok');
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
      if (refresh) await refreshSession();
    } catch { next = 4000; }
  }
  pollTimer = setTimeout(pollJobs, next);
}

// ------------------------------------------------------------------ Session screen
const SessionScreen = {
  focusNew: false,
  mount(root) {
    this.root = h('main', { class: 'page narrow' });
    root.append(this.root);
    this.update();
  },
  update() {
    const r = this.root;
    const keepNew = r.querySelector('.new-session input')?.value || '';
    r.innerHTML = '';
    r.append(h('div', { class: 'page-head' },
      h('h1', null, 'Sessions'),
      h('p', null, 'Drop footage into ', h('span', { class: 'accent' }, 'sessions/<name>/source/'),
        ', refresh, pick it here. That\'s the whole onboarding.')));

    const list = h('div', { class: 'session-list' });
    if (!S.sessions.length) list.append(h('div', { class: 'empty' }, 'No sessions yet — create one below.'));
    for (const s of S.sessions) {
      const sel = s.name === S.name;
      const [label, color] = !s.n_source_files ? ['empty', '#4f4f4f']
        : s.exported ? ['exported', '#97d47d'] : s.synced ? ['synced', '#8880F0']
          : s.processed ? ['processed', '#d3d3d3'] : ['new', '#E89768'];
      list.append(h('div', {
        class: `session-row ${sel ? 'sel' : ''}`,
        onclick: () => openSession(s.name),
        ondblclick: () => openSession(s.name).then(() => go('process')),
      },
      h('i', { class: `${sel ? 'fa-solid fa-square-check' : 'fa-regular fa-square'} box` }),
      h('div', { class: 'col', style: { gap: '4px', minWidth: 0 } },
        h('span', { class: 'nm ellipsis' }, s.name),
        h('span', { class: 'path ellipsis' }, `sessions/${s.name}/source`)),
      h('span', { class: 'meta' }, `${s.n_source_files} files`),
      h('span', { class: 'meta' }, fmtBytes(s.size)),
      h('span', { class: 'state', style: { color } }, h('span', { class: 'dot sq', style: { background: color } }), label)));
    }
    r.append(list);

    const input = h('input', {
      class: 'input', placeholder: 'new-session-name', value: keepNew,
      onkeydown: (e) => { if (e.key === 'Enter') create(); },
    });
    const create = async () => {
      const name = input.value.trim();
      if (!name) { input.focus(); return; }
      try {
        await POST('/api/sessions', { name });
        input.value = '';
        await loadSessions();
        await openSession(name);
        toast(`Created sessions/${name}/source/ — drop footage there`, 'ok');
      } catch (e) { toast(e.message, 'error'); }
    };
    r.append(h('div', { class: 'row' },
      h('button', { class: 'btn primary big', disabled: !S.name, onclick: () => go('process') },
        icon('gears'), 'Process source files'),
      h('button', { class: 'btn big', onclick: () => loadSessions() }, icon('rotate'), 'Rescan sessions'),
      h('button', { class: 'btn big', disabled: !S.name, onclick: () => reveal('source') }, icon('folder-open'), 'Open folder')));
    r.append(h('div', { class: 'new-session' }, input,
      h('button', { class: 'btn', onclick: create }, icon('plus'), 'New session')));
    if (S.health) r.append(h('p', { class: 'note' }, `sessions folder: ${S.health.sessions_dir}`));
    if (this.focusNew) {
      this.focusNew = false;
      requestAnimationFrame(() => input.focus());
    }
  },
};
screens.session = SessionScreen;

// ------------------------------------------------------------------ Process screen
const SAMPLE_RATES = [8000, 16000, 22050, 44100, 48000];
const PROXY_SIZES = [480, 640, 960, 1280];

async function putSettings(patch) {
  try {
    applySession(await PUT(sUrl('/settings'), patch));
  } catch (e) { toast(e.message, 'error'); render(); }
}

const ProcessScreen = {
  advOpen: false,
  customFps: false,
  mount(root) {
    this.root = h('main', { class: 'page' });
    this.left = h('div', { class: 'col', style: { gap: '24px' } });
    this.right = h('div', { class: 'col', style: { gap: '16px', minWidth: 0 } });
    this.root.append(h('div', { class: 'split' }, this.left, this.right));
    root.append(this.root);
    this.update();
  },
  onJobs() { this.renderActions(); },
  update() {
    if (!S.data) {
      this.left.innerHTML = '';
      this.right.innerHTML = '';
      this.left.append(h('div', { class: 'empty' }, 'Pick a session first.'));
      return;
    }
    if (!this.left.contains(document.activeElement) || !this.form) this.renderLeft();
    else this.renderActions();
    this.renderRight();
  },
  renderLeft() {
    const st = S.data.settings;
    const L = this.left;
    L.innerHTML = '';
    L.append(h('div', { class: 'page-head' },
      h('h1', null, 'Process source files'),
      h('p', null, 'Prefilled from container metadata. Change only if ffprobe is lying to you (it sometimes is).')));

    const vids = videoFiles();
    const atTarget = vids.filter((f) => f.probe.video.nominal_fps === st.target_fps).length;
    const std = S.data.standard_rates.map((r) => r.value);
    const custom = this.customFps || !std.includes(st.target_fps);
    const fpsSel = h('select', {
      class: 'input',
      onchange: (e) => {
        if (e.target.value === 'custom') { this.customFps = true; this.renderLeft(); return; }
        this.customFps = false;
        putSettings({ target_fps: e.target.value });
      },
    },
    S.data.standard_rates.map((r) => h('option', { value: r.value, selected: !custom && r.value === st.target_fps }, `${r.label} fps  (${r.value})`)),
    h('option', { value: 'custom', selected: custom }, 'custom…'));
    const fpsCustom = custom ? h('input', {
      class: 'input', value: st.target_fps, placeholder: 'e.g. 30000/1001',
      onchange: (e) => putSettings({ target_fps: e.target.value }),
    }) : null;

    const form = h('div', { class: 'card' },
      h('label', { class: 'field' }, 'Target framerate', fpsSel, fpsCustom,
        h('span', { class: 'note' },
          `metadata: ${atTarget}/${vids.length} files @ ${fpsLabel(st.target_fps)}`,
          st.target_fps_user_set ? ' · set by you · ' : ' · prefilled',
          st.target_fps_user_set ? h('a', { href: '#', onclick: (e) => { e.preventDefault(); this.customFps = false; putSettings({ target_fps_user_set: false }); } }, 'reset') : null)),
      h('label', { class: 'field' }, 'Sync sample rate',
        h('select', { class: 'input', onchange: (e) => putSettings({ sync_sample_rate: Number(e.target.value) }) },
          SAMPLE_RATES.map((r) => h('option', { value: r, selected: r === st.sync_sample_rate }, `${r} Hz`))),
        h('span', { class: 'note' }, 'audio is resampled to this for cross-correlation; 8 kHz is plenty for claps')),
      h('label', { class: 'field' }, 'Scrub proxy size',
        h('select', { class: 'input', onchange: (e) => putSettings({ proxy_long_side: Number(e.target.value) }) },
          PROXY_SIZES.map((r) => h('option', { value: r, selected: r === st.proxy_long_side }, `${r} px long side`))),
        h('span', { class: 'note' }, 'low-res H.264, 15-frame GOP — preview only; a new size rebuilds proxies on the next Process')),
      h('div', { class: 'toggle', onclick: () => putSettings({ hwaccel: !st.hwaccel }) },
        h('span', { class: `check ${st.hwaccel ? 'on' : ''}` }, icon('check')),
        'GPU decode (-hwaccel cuda) for proxies and export'),
      h('button', { class: 'adv-toggle', onclick: () => { this.advOpen = !this.advOpen; this.renderLeft(); } },
        icon(this.advOpen ? 'chevron-down' : 'chevron-right'), 'Advanced sync thresholds'),
      this.advOpen ? h('div', { class: 'col' },
        h('label', { class: 'kv' }, 'min score',
          h('input', { class: 'input sm', type: 'number', step: '0.01', min: '0', max: '1', value: st.sync_min_score, onchange: (e) => putSettings({ sync_min_score: Number(e.target.value) }) })),
        h('label', { class: 'kv' }, 'min ratio',
          h('input', { class: 'input sm', type: 'number', step: '0.1', min: '1', value: st.sync_min_ratio, onchange: (e) => putSettings({ sync_min_ratio: Number(e.target.value) }) })),
        h('label', { class: 'kv' }, 'window ±W (min, s)',
          h('input', { class: 'input sm', type: 'number', step: '0.1', min: '0.2', max: '30', value: st.sync_window ?? 1.5, onchange: (e) => putSettings({ sync_window: Number(e.target.value) }) })),
        h('span', { class: 'note' }, 'score = normalized correlation peak; ratio = peak vs. best peak >0.1 s away. Each clip is matched only within ±W of its camera-clock prediction (W = this, or 3× the clock spread if larger), so looping music cannot pull it a bar off. Inside the window a quarter of the min score is enough; the ratio does the work.')) : null);
    this.form = form;
    L.append(form);
    this.actions = h('div', { class: 'col', style: { gap: '12px' } });
    L.append(this.actions);
    this.renderActions();
  },
  renderActions() {
    if (!this.actions || !S.data) return;
    const A = this.actions;
    A.innerHTML = '';
    const scanning = activeJob('scan');
    const processing = activeJob('process');
    const proxies = activeJob('proxies');
    const n = videoFiles().length;
    A.append(h('div', { class: 'row' },
      h('button', { class: 'btn', disabled: !!scanning, onclick: () => startJob('/scan') }, icon('magnifying-glass'), 'Re-scan'),
      h('button', { class: 'btn primary', disabled: !!processing || !n, onclick: () => startJob('/process') },
        icon('gears'), S.data.state.processed_at ? 'Process again' : 'Process source files'),
      S.data.state.processed_at ? h('button', { class: 'btn primary', onclick: () => go('cameras') }, 'Sort by camera', icon('arrow-right')) : null));
    for (const j of [scanning, processing, proxies]) {
      if (!j) continue;
      A.append(h('div', { class: 'col', style: { gap: '6px' } },
        h('div', { class: 'progress-label' }, h('span', { class: 'ellipsis' }, `${j.label} · ${j.message || '…'}`), h('span', null, `${Math.round(j.progress * 100)}%`)),
        h('div', { class: 'progress' }, h('div', { style: { width: `${j.progress * 100}%` } }))));
    }
    if (!n && !scanning) A.append(h('p', { class: 'note' }, 'No video files found in source/ yet.'));
  },
  renderRight() {
    const R = this.right;
    R.innerHTML = '';
    const fc = S.data.fps_check;
    const signs = { ok: h('img', { src: '/static/assets/success-sign-green.svg', alt: '' }), warn: h('img', { src: '/static/assets/warning-sign-orange.svg', alt: '' }), error: icon('circle-xmark'), empty: icon('folder-open', 'muted') };
    const cams = S.data.cameras.length;
    R.append(h('div', { class: 'fpsbanner' },
      activeJob('scan') ? icon('spinner', 'fa-spin muted') : signs[fc.level],
      h('div', { class: 'lines' },
        h('span', { class: 'headline' }, fc.summary,
          cams ? h('span', null, ` — ${cams} camera${cams === 1 ? '' : 's'} detected`) : null,
          h('span', null, ` · target ${fc.target_label} fps`)),
        fc.items.map((it) => h('span', { class: 'item' },
          h('span', { class: 'dot', style: { background: it.level === 'error' ? '#E56767' : '#E89768' } }),
          h('span', null, h('b', null, it.title), ' — ', it.detail,
            h('span', { class: 'dim' }, ' · ', it.files.slice(0, 6).map((id) => basename(fileById(id)?.rel || id)).join(', '),
              it.files.length > 6 ? ` +${it.files.length - 6}` : '')))))));

    const files = [...S.data.files].sort((a, b) => a.rel.localeCompare(b.rel));
    if (!files.length) {
      R.append(h('div', { class: 'empty' }, 'source/ is empty. Drop the raw clips there (subfolders per camera are fine) and hit Re-scan.'));
      return;
    }
    const tgt = S.data.settings.target_fps;
    const rows = files.map((f) => {
      const p = f.probe;
      const v = p && p.video;
      const cam = cameraById(f.camera);
      const make = [p?.make, p?.model].filter(Boolean).join(' ');
      return h('tr', null,
        h('td', { class: 'nm', title: f.rel }, f.rel),
        h('td', null, cam ? cam.name : h('span', { class: 'dim' }, '—')),
        h('td', null, p ? fmtDur(p.duration) : '—'),
        h('td', null, v ? `${v.display_width}×${v.display_height}` : h('span', { class: 'tag err' }, 'no video')),
        h('td', null, v ? v.codec : '—'),
        h('td', null, v ? h('span', { class: v.nominal_fps === tgt ? '' : 'warn' }, fpsLabel(v.nominal_fps),
          h('span', { class: 'dim' }, ` (${fpsFloat(v.avg_fps).toFixed(3)})`)) : '—'),
        h('td', null, v && v.vfr_suspect ? h('span', { class: 'tag warn' }, 'VFR') : ''),
        h('td', null, p ? (p.audio ? `${p.audio.codec} ${Math.round((p.audio.sample_rate || 0) / 1000)}k` : h('span', { class: 'tag warn' }, 'none')) : '—'),
        h('td', null, v && v.rotation_meta ? `${v.rotation_meta}°` : h('span', { class: 'dim' }, '0°')),
        h('td', null, p ? fmtDate(p.creation_time) : '—'),
        h('td', { class: 'ellipsis', style: { maxWidth: '160px' }, title: make }, make || h('span', { class: 'dim' }, '—')),
        h('td', null, f.probe_error ? h('span', { class: 'tag err', title: f.probe_error }, 'probe failed') : h('span', { class: 'assets' },
          h('span', { class: f.assets.thumb ? 'on' : '', title: 'thumbnail' }, 'T'),
          h('span', { class: f.assets.audio ? 'on' : '', title: 'sync audio' }, 'A'),
          h('span', { class: f.assets.proxy ? 'on' : '', title: 'scrub proxy' }, 'P'))));
    });
    R.append(h('div', { class: 'tablewrap' }, h('table', { class: 'ftable' },
      h('thead', null, h('tr', null, ['source/', 'camera', 'length', 'resolution', 'codec', 'fps (avg)', 'vfr', 'audio', 'rot', 'created', 'make / model', 'assets'].map((c) => h('th', null, c)))),
      h('tbody', null, rows))));
  },
};
screens.process = ProcessScreen;

export const fileById = (id) => (S.data?.files || []).find((f) => f.id === id);

// ------------------------------------------------------------------ Cameras screen
const PHONE_MAKE = /apple|google|samsung|xiaomi|oneplus|huawei|motorola|oppo|vivo|iphone|ipad|pixel|android/i;

function nextFree(prefix, used) {
  let k = 1;
  while (used.has(`${prefix}${k}`)) k += 1;
  return `${prefix}${k}`;
}

const CamerasScreen = {
  cams: [],
  assign: {},
  dirty: false,
  saving: false,
  fits: [],
  mount(root) {
    this.root = h('main', { class: 'page' });
    root.append(this.root);
    this.ro = new ResizeObserver(() => this.refit());
    this.load();
    this.render();
  },
  unmount() {
    this.flush();
    this.ro.disconnect();
  },
  sessionChanged() {
    this.flush();
    this.cams = [];
    this.assign = {};
  },
  load() {
    this.cams = structuredClone(S.data?.cameras || []);
    this.assign = {};
  },
  update() {
    if (!this.dirty && !this.saving) this.load();
    const a = document.activeElement;
    if (a && this.root.contains(a) && (a.tagName === 'INPUT' || a.tagName === 'SELECT')) return;
    this.render();
  },
  onJobs() {
    const b = this.root.querySelector('[data-regroup]');
    if (b) b.disabled = !!activeJob('regroup');
  },
  camOf(f) { return this.assign[f.id] ?? f.camera; },
  filesOf(cid) {
    return videoFiles().filter((f) => this.camOf(f) === cid)
      .sort((a, b) => (a.offset ?? 1e12) - (b.offset ?? 1e12) || a.rel.localeCompare(b.rel));
  },
  schedule() {
    this.dirty = true;
    clearTimeout(this.timer);
    this.timer = setTimeout(() => this.save(), 500);
  },
  async save() {
    clearTimeout(this.timer);
    if (!this.dirty || !S.name) return;
    const body = { cameras: this.cams, assignments: this.assign };
    const name = S.name;
    this.dirty = false;
    this.saving = true;
    try {
      const d = await PUT(sUrl('/cameras'), body);
      this.saving = false;
      if (!this.dirty && S.name === name) {
        S.data = d;
        render();
      }
    } catch (e) {
      this.saving = false;
      toast(e.message, 'error');
    }
  },
  flush() {
    // keepalive lets the request finish even while the page unloads
    if (!this.dirty || !S.name) return;
    clearTimeout(this.timer);
    this.dirty = false;
    fetch(sUrl('/cameras'), {
      method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ cameras: this.cams, assignments: this.assign }),
    }).catch(() => {});
  },
  async regroup() {
    const ok = await confirmModal({
      title: 'Re-run auto grouping?',
      body: 'This replaces the current camera assignments, names and rotations with a fresh automatic grouping.',
      ok: 'Re-run grouping',
    });
    if (!ok) return;
    clearTimeout(this.timer);
    this.dirty = false;
    await startJob('/regroup');
  },
  move(f, cid) {
    if (cid === '__new') {
      const cam = {
        id: nextFree('c', new Set(this.cams.map((c) => c.id))),
        name: nextFree('cam', new Set(this.cams.map((c) => c.name))),
        rotation: 0, include: true, grouped_by: 'manual',
      };
      this.cams.push(cam);
      cid = cam.id;
    }
    this.assign[f.id] = cid;
    this.cams = this.cams.filter((c) => this.filesOf(c.id).length > 0);
    this.schedule();
    this.render();
  },
  refit() {
    for (const [img, box, dw, dh, getRot] of this.fits) {
      if (img.isConnected) applyFit(img, box, dw, dh, getRot());
    }
  },
  render() {
    const r = this.root;
    r.innerHTML = '';
    this.fits = [];
    this.ro.disconnect();
    if (!S.data) return;
    const vids = videoFiles();
    const n = this.cams.length;
    r.append(h('div', { class: 'head-row' },
      h('div', { class: 'page-head' },
        h('h1', null, 'Cameras'),
        h('p', null, `${n} camera${n === 1 ? '' : 's'}, ${vids.length} files, sorted. Fix orientation against the sample frame `
          + '(person should be standing, not napping) and name them. Rotation is only applied when exporting.')),
      h('div', { class: 'row' },
        h('button', { class: 'btn big', 'data-regroup': '1', disabled: !!activeJob('regroup'), onclick: () => this.regroup() },
          icon('wand-magic-sparkles'), 'Re-run auto grouping'),
        h('button', { class: 'btn primary big', onclick: () => { this.save(); go('sync'); } }, icon('wave-square'), 'Sync cameras'))));
    const grid = h('div', { class: 'camgrid' });
    const names = {};
    for (const c of this.cams) names[c.name] = (names[c.name] || 0) + 1;
    for (const c of this.cams) grid.append(this.card(c, names[c.name] > 1));
    const orphans = vids.filter((f) => !this.cams.some((c) => c.id === this.camOf(f)));
    if (orphans.length) grid.append(this.orphanCard(orphans));
    r.append(grid);
    requestAnimationFrame(() => this.refit());
  },
  moveSelect(f) {
    const cur = this.camOf(f);
    return h('select', { title: 'Move to camera', onchange: (e) => this.move(f, e.target.value) },
      this.cams.map((c) => h('option', { value: c.id, selected: c.id === cur }, c.name)),
      cur && this.cams.some((c) => c.id === cur) ? null : h('option', { value: '', selected: true }, '—'),
      h('option', { value: '__new' }, '+ new camera'));
  },
  fileRow(f, getRot) {
    const mini = h('div', { class: 'mini', style: { position: 'relative' } });
    if (f.assets.thumb) {
      const v = f.probe.video;
      const img = h('img', { src: mediaUrl('thumb', f, 'jpg'), alt: '', style: { position: 'absolute', maxWidth: 'none', maxHeight: 'none' } });
      mini.append(img);
      this.fits.push([img, mini, v.display_width, v.display_height, getRot]);
    }
    return h('div', { class: 'camfile' }, mini,
      h('div', { class: 'meta' },
        h('span', { class: 'ellipsis', title: f.rel, style: { color: 'var(--fg-1)' } }, f.rel),
        h('span', { class: 'dim' }, `${fmtDur(f.probe.duration)} · at ${f.offset != null ? fmtClock(f.offset) : "—"}`)),
      this.moveSelect(f));
  },
  card(c, dupName) {
    const files = this.filesOf(c.id);
    const first = files[0];
    const v = first?.probe.video;
    const p = first?.probe || {};
    const dw = v?.display_width || 16;
    const dh = v?.display_height || 9;
    const rotChip = h('span', { class: 'chip tr' }, `${c.rotation}°`);
    const box = h('div', { class: 'campreview' });
    let img = null;
    if (first && first.assets.thumb) {
      img = h('img', { src: mediaUrl('thumb', first, 'jpg'), alt: '' });
      box.append(img);
      this.fits.push([img, box, dw, dh, () => c.rotation]);
      this.ro.observe(box);
    } else {
      box.append(h('span', { class: 'chip bl' }, 'no preview frame'));
    }
    if (first) box.append(h('span', { class: 'chip tl' }, `sample · ${basename(first.rel)} @ ${fmtClock(0.3 * (p.duration || 0))}`));
    box.append(rotChip);
    if (v && v.rotation_meta) box.append(h('span', { class: 'chip bl' }, `metadata ${v.rotation_meta}° applied`));

    const angles = [0, 90, 180, 270];
    const angleBtns = angles.map((a) => h('button', { class: a === c.rotation ? 'on' : '', onclick: () => setRot(a) }, `${a}°`));
    const setRot = (deg) => {
      c.rotation = ((deg % 360) + 360) % 360;
      this.refit(); // CSS transform only — nothing is re-encoded (R1)
      rotChip.textContent = `${c.rotation}°`;
      angleBtns.forEach((b, i) => b.classList.toggle('on', angles[i] === c.rotation));
      this.schedule();
    };
    const make = [p.make, p.model].filter(Boolean).join(' ') || 'unknown device';
    let rotNote;
    let rotColor = '';
    if (v && v.rotation_meta) rotNote = `prefilled from metadata: rotate=${v.rotation_meta} (already applied)`;
    else if (c.rotation) rotNote = `rotated ${c.rotation}° by you`;
    else if (PHONE_MAKE.test(make)) { rotNote = 'no rotation tag — check this one'; rotColor = 'warn'; }
    else rotNote = 'no rotation metadata';
    const grouped = c.grouped_by === 'subfolder' ? 'subfolder' : c.grouped_by === 'manual' ? 'you' : 'metadata + background';

    return h('div', { class: `camcard ${c.include ? '' : 'excluded'}` },
      box,
      h('div', { class: 'rotbar' },
        h('button', { title: 'Rotate 90° counter-clockwise', onclick: () => setRot(c.rotation - 90) }, icon('rotate-left')),
        angleBtns,
        h('button', { title: 'Rotate 90° clockwise', onclick: () => setRot(c.rotation + 90) }, icon('rotate-right'))),
      h('div', { class: 'cambody' },
        h('input', {
          class: `input camname ${dupName ? 'invalid' : ''}`, value: c.name, spellcheck: 'false',
          oninput: (e) => { c.name = e.target.value; this.schedule(); },
          onblur: () => this.render(),
        }),
        dupName ? h('span', { class: 'note warn' }, 'name used twice — export file names would collide') : null,
        h('div', { class: 'caminfo' },
          h('span', null, `${make} · ${dw}×${dh} · ${v ? fpsLabel(v.nominal_fps) : '?'} fps`),
          h('span', { class: c.grouped_by === 'subfolder' || c.grouped_by === 'manual' ? '' : 'info' }, `grouped by ${grouped}`),
          h('span', { class: rotColor }, rotNote)),
        h('div', { class: 'toggle', onclick: () => { c.include = !c.include; this.schedule(); this.render(); } },
          h('span', { class: `check ${c.include ? 'on' : ''}` }, icon('check')), 'include in export'),
        h('div', { class: 'camfiles' }, files.map((f) => this.fileRow(f, () => c.rotation)))));
  },
  orphanCard(files) {
    return h('div', { class: 'camcard' },
      h('div', { class: 'cambody' },
        h('b', null, 'Unassigned'),
        h('span', { class: 'note' }, 'pick a camera for these files'),
        h('div', { class: 'camfiles' }, files.map((f) => this.fileRow(f, () => 0)))));
  },
};
screens.cameras = CamerasScreen;
window.addEventListener('beforeunload', () => CamerasScreen.flush());

// ------------------------------------------------------------------ Sync screen
const SYNC_PHASES = [
  ['audio', 'timestamps'], ['links', 'camera clocks'], ['align', 'audio within ±W'], ['search', 'full search'], ['check', 'checks'],
];
function fmtAhead(s) {
  if (s == null) return '—';
  const a = Math.abs(s);
  const sign = s >= 0 ? '+' : '−';
  if (a < 60) return `${sign}${a.toFixed(2)} s`;
  if (a < 3600) return `${sign}${Math.floor(a / 60)}:${pad2(Math.floor(a % 60))} min`;
  return `${sign}${Math.floor(a / 3600)}:${pad2(Math.floor(a / 60) % 60)}:${pad2(Math.floor(a % 60))} h`;
}
const SyncScreen = {
  resyncManual: false,
  mount(root) {
    this.root = h('main', { class: 'page' });
    root.append(this.root);
    this.vizBox = h('div', { class: 'viz-canvas' });
    this.viz = new SyncViz(this.vizBox);
    this.shown = false;
    this.render();
  },
  unmount() {
    clearTimeout(this.liveTimer);
    this.liveTimer = null;
    this.stopReplay();
    if (this.viz) this.viz.destroy();
    this.viz = null;
  },
  update() { this.render(); },
  async onJobs() {
    if (activeJob('sync')) this.startLive();
    await this.fetchLog();
    this.renderLeftDynamic();
  },
  job() { return activeJob('sync') || lastJob('sync'); },
  async fetchLog() {
    const j = this.job();
    if (!j) return;
    const have = jobLogs.get(j.id) || { seq: 0, lines: [] };
    if (have.seq >= j.log_seq) return;
    try {
      const full = await GET(`/api/jobs/${j.id}?log_since=${have.seq}`);
      have.lines.push(...full.log);
      have.seq = full.log_seq;
      have.status = full.status;
      jobLogs.set(j.id, have);
    } catch { /* pruned */ }
  },
  // the live picture: the job publishes a snapshot, polled faster than the job list
  startLive() {
    if (this.liveTimer || !this.viz) return;
    this.stopReplay();
    const tick = async () => {
      const j = activeJob('sync');
      if (!j || !this.viz) { this.liveTimer = null; return; }
      const have = jobLogs.get(j.id) || { seq: 0, lines: [] };
      try {
        const full = await GET(`/api/jobs/${j.id}?log_since=${have.seq}&live=true`);
        have.lines.push(...full.log);
        have.seq = full.log_seq;
        have.status = full.status;
        jobLogs.set(j.id, have);
        Object.assign(j, { progress: full.progress, message: full.message, log_seq: full.log_seq });
        if (full.live && this.viz) {
          this.lastLive = full.live;
          this.viz.setData(full.live);
        }
        this.renderLeftDynamic();
        if (!isActive(full)) { this.liveTimer = null; pollSoon(); return; }
      } catch { /* pruned */ }
      this.liveTimer = setTimeout(tick, 300);
    };
    this.liveTimer = setTimeout(tick, 0);
  },
  replay() {
    this.stopReplay();
    const frames = replayFrames(snapshotFromSession(S.data, videoFiles()));
    this.replaying = true;
    this.timers = frames.map(([t, snap]) => setTimeout(() => {
      this.replayPhase = snap.phase;
      if (this.viz) this.viz.setData(snap);
      this.renderVizHead();
    }, t));
    this.timers.push(setTimeout(() => { this.replaying = false; this.renderVizHead(); }, frames[frames.length - 1][0] + 60));
    this.renderVizHead();
  },
  stopReplay() {
    for (const t of this.timers || []) clearTimeout(t);
    this.timers = [];
    this.replaying = false;
  },
  render() {
    const r = this.root;
    r.innerHTML = '';
    if (!S.data) return;
    const vids = videoFiles();
    const st = S.data.settings;
    const manual = vids.filter((f) => f.sync?.method === 'manual').length;
    this.vizHead = h('div', { class: 'viz-head' });
    this.progress = h('div', { class: 'viz-progress' });
    this.log = h('div', { class: 'log' });
    r.append(
      h('div', { class: 'page-head' },
        h('h1', null, 'Syncing'),
        h('p', null, 'Every camera has its own clock. Clips start ', h('span', { class: 'accent' }, 'orange'),
          ' where their timestamps put them, move once their camera\'s clock is worked out from audio links, and turn ',
          h('span', { class: 'ok' }, 'green'), ' when the audio clicks them into place within ±W.')),
      h('div', { class: 'row' },
        this.runBtn = h('button', { class: 'btn primary big', onclick: () => this.run() }),
        manual ? h('div', { class: 'toggle', onclick: () => { this.resyncManual = !this.resyncManual; this.render(); } },
          h('span', { class: `check ${this.resyncManual ? 'on' : ''}` }, icon('check')), `re-sync ${manual} manual file(s) too`) : null,
        h('span', { class: 'note' }, `window ±${st.sync_window ?? 1.5} s or 3× clock spread · accept: score ≥ ${st.sync_min_score} · ratio ≥ ${st.sync_min_ratio} · ${st.sync_sample_rate} Hz (Process › Advanced)`)),
      h('div', { class: 'card viz-card' }, this.vizHead, this.progress, this.vizBox),
      this.summary(vids) || '',
      h('div', { class: 'section-rule' }, h('span', null, 'details')),
      h('div', { class: 'sync-split' },
        h('div', { class: 'col', style: { gap: '16px', minWidth: 0 } }, h('span', { class: 'card-title' }, 'Log'), this.log),
        h('div', { class: 'col', style: { gap: '16px', minWidth: 0 } }, this.clocksCard(), this.offsetsCard(vids))));
    if (!activeJob('sync') && !this.replaying && this.viz) {
      this.viz.setData(snapshotFromSession(S.data, vids), { instant: !this.shown });
      this.shown = true;
    }
    if (activeJob('sync')) this.startLive();
    this.renderLeftDynamic();
    this.fetchLog().then(() => this.renderLeftDynamic());
  },
  renderVizHead() {
    if (!this.vizHead || !this.vizHead.isConnected) return;
    const active = activeJob('sync');
    let phase = S.data.state.synced_at ? 'done' : 'idle';
    if (this.replaying) phase = this.replayPhase || 'audio';
    else if (active) phase = this.lastLive?.phase || 'audio';
    const idx = SYNC_PHASES.findIndex(([id]) => id === phase);
    const n = this.viz ? this.viz.counts() : {};
    this.vizHead.innerHTML = '';
    this.vizHead.append(
      h('div', { class: 'vsteps' }, SYNC_PHASES.map(([id, label], i) => {
        const done = phase === 'done' || (idx >= 0 && i < idx);
        return h('span', { class: `vstep${done ? ' done' : ''}${i === idx ? ' on' : ''}` },
          h('b', null, done ? icon('check') : String(i + 1)), label);
      })),
      h('div', { class: 'vcounts' },
        h('span', null, h('span', { class: 'dot sq', style: { background: '#97d47d' } }), `${n.clicked || 0} clicked`),
        h('span', null, h('span', { class: 'dot sq', style: { background: '#E89768' } }), `${n.rough || 0} rough`),
        n.fallback ? h('span', null, h('span', { class: 'dot sq', style: { background: '#E2B35B' } }), `${n.fallback} by clock`) : null,
        n.manual ? h('span', null, h('span', { class: 'dot sq', style: { background: '#8880F0' } }), `${n.manual} manual`) : null,
        n.none ? h('span', null, h('span', { class: 'dot sq', style: { background: '#E56767' } }), `${n.none} unplaced`) : null,
        !active && S.data.sync?.frame != null
          ? h('button', { class: 'btn xs', title: 'Replay this sync', onclick: () => this.replay(), disabled: this.replaying }, icon('rotate-left'), 'Replay')
          : null));
  },
  renderLeftDynamic() {
    if (!this.log || !this.log.isConnected) return;
    const j = this.job();
    const active = activeJob('sync');
    this.runBtn.innerHTML = '';
    this.runBtn.append(...(active ? [icon('xmark'), 'Cancel'] : [icon('wave-square'), S.data.state.synced_at ? 'Sync again' : 'Run sync']));
    this.progress.innerHTML = '';
    if (j && active) {
      const pct = Math.round(j.progress * 100);
      this.progress.append(
        h('div', { class: 'progress-label' }, h('span', null, j.message || 'starting…'), h('span', null, `${pct}%`)),
        h('div', { class: 'progress' }, h('div', { style: { width: `${pct}%` } })));
    }
    this.renderVizHead();
    const lines = j ? (jobLogs.get(j.id)?.lines || []) : [];
    const stick = this.log.scrollTop + this.log.clientHeight >= this.log.scrollHeight - 8;
    this.log.innerHTML = '';
    if (!lines.length) {
      this.log.append(h('span', { class: 'dim' }, j ? '…' : '$ press Run sync — clocks first (timestamps + audio links between cameras), then every clip is matched by audio only within ±W of where its clock says it is.'));
    }
    for (const l of lines) {
      const cls = l.msg.startsWith('done') ? 'l-done' : `l-${l.level}`;
      this.log.append(h('span', { class: cls }, l.msg));
    }
    if (stick) this.log.scrollTop = this.log.scrollHeight;
  },
  async run() {
    const active = activeJob('sync');
    if (active) { cancelJob(active.id); return; }
    this.stopReplay();
    this.lastLive = null;
    await startJob('/sync', { resync_manual: this.resyncManual });
    this.startLive();
    this.renderLeftDynamic();
  },
  clocksCard() {
    const sync = S.data.sync || {};
    const info = sync.cameras || {};
    const cams = S.data.cameras.filter((c) => info[c.id]);
    if (!cams.length) return null;
    const viaLabel = { reference: 'reference', link: 'audio links', search: 'full search', 'median clock': 'assumed (median clock)', 'manual offset': 'your manual offset' };
    return h('div', { class: 'card', style: { gap: '8px' } },
      h('span', { class: 'card-title' }, 'Camera clocks'),
      h('div', { class: 'clocks' },
        h('span', { class: 'th' }, 'camera'), h('span', { class: 'th' }, 'vs reference'), h('span', { class: 'th' }, 'stamps'),
        h('span', { class: 'th', title: 'matches agreeing within ±1 s / all matches' }, 'agree'), h('span', { class: 'th' }, 'spread'), h('span', { class: 'th' }, '±W'),
        cams.map((c) => {
          const k = info[c.id];
          const isRef = c.id === sync.reference_camera;
          const cls = !k.attached && k.via !== 'manual offset' ? 'warn' : '';
          return [
            h('span', { class: 'ellipsis', title: `${c.name} — ${viaLabel[k.via] || k.via}` }, c.name),
            h('span', { class: cls || (isRef ? 'ok' : ''), title: 'camera clock minus reference clock' }, isRef ? 'reference' : k.attached || k.via === 'manual offset' ? fmtAhead(-k.offset) : 'unknown'),
            h('span', { title: k.mode_basis === 'measured' ? 'told apart by the clock links' : 'not measurable (clips of equal length) — nearest clock assumed' }, `${k.mode}${k.mode_basis === 'measured' ? '' : '?'}`),
            h('span', null, k.votes ? `${k.support}/${k.votes}` : '—'),
            h('span', null, `${k.spread.toFixed(2)} s`),
            h('span', null, `${k.window.toFixed(1)} s`),
          ];
        })),
      h('span', { class: 'note' }, 'vs reference: how far the camera\'s clock is ahead (+) or behind. stamps: whether creation_time marks the start or the end of a recording (? = not measurable, nearest clock assumed).'));
  },
  offsetsCard(vids) {
    const card = h('div', { class: 'card', style: { gap: '4px' } }, h('span', { class: 'card-title' }, 'Offsets'));
    const groups = [...S.data.cameras.map((c) => [c, vids.filter((f) => f.camera === c.id)]),
      [{ name: 'unassigned' }, vids.filter((f) => !cameraById(f.camera))]];
    for (const [cam, files] of groups) {
      if (!files.length) continue;
      card.append(h('div', { class: 'offsets' }, h('span', { class: 'cam-h' }, cam.name),
        files.sort((a, b) => (a.offset ?? 1e12) - (b.offset ?? 1e12)).map((f) => {
          const m = f.offset == null ? 'none' : (f.sync?.method || 'none');
          const flags = f.sync?.flags || [];
          const conf = f.sync?.score != null ? `${f.sync.score.toFixed(2)} · ${f.sync.ratio != null ? f.sync.ratio.toFixed(1) : '–'}×` : m === 'reference' ? 'ref' : m;
          const title = [`${f.rel} — ${METHOD_LABEL[m] || m}${f.sync?.via ? ` (${f.sync.via})` : ''}`, f.sync?.reason, ...flags].filter(Boolean).join('\n');
          return h('div', { class: 'offrow', title },
            h('span', { class: 'dot', style: { background: METHOD_COLORS[m] } }),
            h('span', { class: 'ellipsis' }, flags.length ? h('span', { class: 'warn' }, icon('triangle-exclamation'), ' ') : null, basename(f.rel)),
            h('span', { class: 'val' }, f.offset != null ? fmtOffset(f.offset) : '—'),
            h('span', { class: 'conf' }, conf));
        })));
    }
    card.append(h('div', { class: 'legend', style: { paddingTop: '10px' } },
      Object.entries(METHOD_LABEL).map(([k, v]) => h('span', null, h('span', { class: 'dot', style: { background: METHOD_COLORS[k] } }), v))));
    return card;
  },
  summary(vids) {
    if (activeJob('sync')) return null;
    if (!S.data.state.synced_at) return h('div', { class: 'note' }, 'Offsets are provisional (creation time). Run sync to place clips by audio.');
    const meta = vids.filter((f) => f.sync?.method === 'metadata');
    const search = vids.filter((f) => f.sync?.method === 'search');
    const none = vids.filter((f) => f.offset == null || f.sync?.method === 'none');
    const flagged = vids.filter((f) => (f.sync?.flags || []).length);
    const good = vids.length - meta.length - none.length - search.length;
    const allGood = !meta.length && !none.length && !search.length && !flagged.length;
    return h('div', { class: 'card', style: { flexDirection: 'row', alignItems: 'center', gap: '14px', padding: '16px 20px' } },
      h('img', { src: `/static/assets/${allGood ? 'success-sign-green' : 'warning-sign-orange'}.svg`, alt: '', style: { width: '28px', height: '28px' } }),
      h('span', { style: { flex: 1, fontSize: '13px', lineHeight: 1.5 } },
        allGood ? `All ${vids.length} clips aligned.` : `${good}/${vids.length} clips aligned by audio near their camera clock.`,
        search.length ? h('span', { class: 'warn' }, ` ${search.length} placed by full search only — check them.`) : null,
        meta.length ? h('span', { class: 'warn' }, ` ${meta.length} placed by camera clock only — check them on the timeline.`) : null,
        flagged.length ? h('span', { class: 'warn' }, ` ${flagged.length} flagged (see Offsets).`) : null,
        none.length ? h('span', { class: 'err' }, ` ${none.length} unplaced.`) : null),
      h('button', { class: 'btn primary', onclick: () => go('review') }, 'Open timeline', icon('arrow-right')));
  },
};
screens.sync = SyncScreen;

// ------------------------------------------------------------------ Review & export screen
const SPEEDS = [0.25, 0.5, 1, 2];
const METHOD_LABEL = {
  reference: 'reference', audio: 'audio match', search: 'full search (check)', metadata: 'camera clock (check)', none: 'unplaced', manual: 'manual',
};
const clone = (x) => JSON.parse(JSON.stringify(x));

function sessionSpan() {
  let end = 0;
  for (const f of videoFiles()) if (f.offset != null) end = Math.max(end, f.offset + (f.probe.duration || 0));
  return end || 60;
}

/** Which files cover each camera for a take (all that overlap it, joined on
 *  export), and how much black it needs before, between and after them. */
export function takeCoverage(tk, camIds) {
  const fps = fpsFloat(targetFps()) || 30;
  return camIds.map((cid) => {
    const cam = cameraById(cid);
    const files = videoFiles()
      .filter((f) => f.camera === cid && f.offset != null
        && Math.min(tk.end, f.offset + f.probe.duration) - Math.max(tk.start, f.offset) >= 1 / fps)
      .sort((a, b) => a.offset - b.offset);
    if (!files.length) return { cam, missing: true };
    let reach = tk.start;
    let gap = 0;
    for (const f of files) {
      if (reach > tk.start) gap += Math.max(0, f.offset - reach);
      reach = Math.max(reach, Math.min(tk.end, f.offset + f.probe.duration));
    }
    const pre = Math.max(0, files[0].offset - tk.start);
    const post = Math.max(0, tk.end - reach);
    return { cam, files, pre, gap, post, padded: pre + gap + post > 0.5 / fps };
  });
}

function coverageText(cov) {
  const missing = cov.filter((c) => c.missing).map((c) => c.cam?.name || '?');
  const padded = cov.filter((c) => !c.missing && c.padded).map((c) => `${c.cam?.name} +${(c.pre + c.gap + c.post).toFixed(1)}s pad`);
  const ok = cov.length - missing.length;
  if (!missing.length && !padded.length) return { ok: true, text: `${ok}/${cov.length} cams` };
  return { ok: false, text: [`${ok}/${cov.length} cams`, ...missing.map((m) => `${m} missing`), ...padded].join(' · ') };
}

const padText = (c) => [
  c.pre > 0.0005 ? `${c.pre.toFixed(2)} s black before` : '',
  c.gap > 0.0005 ? `${c.gap.toFixed(2)} s black between files` : '',
  c.post > 0.0005 ? `${c.post.toFixed(2)} s black after` : ''].filter(Boolean).join(' · ');

// Takes and export options are edited on both Review and Export, so they live
// here rather than in either screen, and are saved shortly after each change.
const RV = {
  t: 0, playing: false, speed: 1, sel: null, pendingIn: null, pendingOut: null,
  takes: [], committed: [], undo: [], takesDirty: false, takesSaving: false,
  pendingOffsets: new Map(), opts: null, optsDirty: false,
};
const saveTimers = {};
const exportCams = () => RV.opts.camera_ids.filter((id) => cameraById(id));

/** Take the session's takes and export options, unless edits to them aren't saved yet. */
function loadTakes() {
  if (!RV.takesDirty && !RV.takesSaving) {
    RV.takes = clone(S.data.takes || []);
    RV.committed = clone(RV.takes);
  }
  // a sync shifts takes on the server; undo entries from before it are in the old frame
  if (RV.syncedAt !== undefined && RV.syncedAt !== S.data.state.synced_at) RV.undo = [];
  RV.syncedAt = S.data.state.synced_at;
  if (!RV.optsDirty) {
    RV.opts = clone(S.data.export_options || {});
    if (!Array.isArray(RV.opts.camera_ids)) RV.opts.camera_ids = S.data.cameras.filter((c) => c.include).map((c) => c.id);
  }
}
/** Keep an edit of RV.takes for undo and save it shortly. */
function commitTakeEdit() {
  RV.takes.sort((x, y) => x.start - y.start);
  RV.undo.push(RV.committed);
  if (RV.undo.length > 60) RV.undo.shift();
  RV.committed = clone(RV.takes);
  queueTakesSave();
}
function queueTakesSave() {
  RV.takesDirty = true;
  clearTimeout(saveTimers.takes);
  saveTimers.takes = setTimeout(() => saveTakes(), 400);
}
async function saveTakes(beacon = false) {
  clearTimeout(saveTimers.takes);
  if (!RV.takesDirty || !S.name) return;
  RV.takesDirty = false;
  const body = JSON.stringify(RV.takes);
  if (beacon) {
    fetch(sUrl('/takes'), { method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json' }, body }).catch(() => {});
    return;
  }
  RV.takesSaving = true;
  const name = S.name;
  try {
    const d = await api('PUT', sUrl('/takes'), JSON.parse(body));
    if (S.name === name && S.data) {
      S.data.takes = d.takes;
      S.data.state = d.state;
    }
  } catch (e) {
    if (S.name === name) {
      RV.takesDirty = true;
      clearTimeout(saveTimers.takes);
      saveTimers.takes = setTimeout(() => saveTakes(), 3000); // retry
    }
    toast(`Couldn't save takes: ${e.message}`, 'error');
  } finally {
    RV.takesSaving = false;
  }
}
function queueOptsSave() {
  RV.optsDirty = true;
  clearTimeout(saveTimers.opts);
  saveTimers.opts = setTimeout(() => saveOpts(), 500);
}
async function saveOpts(beacon = false) {
  clearTimeout(saveTimers.opts);
  if (!RV.optsDirty || !S.name) return;
  RV.optsDirty = false;
  const body = JSON.stringify(RV.opts);
  if (beacon) {
    fetch(sUrl('/export_options'), { method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json' }, body }).catch(() => {});
    return;
  }
  const name = S.name;
  try {
    const d = await api('PUT', sUrl('/export_options'), JSON.parse(body));
    if (S.name === name && S.data) S.data.export_options = d.export_options;
  } catch (e) { toast(e.message, 'error'); }
}
/** Switching sessions: send the old one's edits while sUrl() still points at it, then drop them. */
function leaveTakes() {
  saveTakes(true);
  saveOpts(true);
  Object.assign(RV, {
    t: 0, sel: null, pendingIn: null, pendingOut: null, takes: [], committed: [], undo: [],
    takesDirty: false, takesSaving: false, opts: null, optsDirty: false, syncedAt: undefined,
  });
}

const ReviewScreen = {
  mount(root) {
    this.side = h('aside', { class: 'side' });
    this.gridEl = h('div', { class: 'grid' });
    this.insp = h('aside', { class: 'inspector' });
    this.transport = h('div', { class: 'transport' });
    this.tlEl = h('div', { class: 'timeline' });
    root.append(this.rootEl = h('div', { class: 'review' },
      this.side,
      h('div', { class: 'rv-main' },
        h('div', { class: 'stage' }, this.gridEl, this.insp),
        this.transport,
        h('div', { style: { overflowY: 'auto', maxHeight: '46vh', flexShrink: '0' } }, this.tlEl))));
    this.grid = new Grid(this.gridEl, { files: (cid) => (this.gridFiles && this.gridFiles.get(cid)) || [] });
    this.timeline = new Timeline(this.tlEl, {
      onScrub: (t) => { if (RV.playing) this.stop(); this.seek(t); },
      onSelect: (sel) => this.select(sel),
      onTakesChanged: (final) => (final ? this.commitTakes() : this.renderSideSoon()),
      onCreateTake: (a, b) => this.createTake(a, b),
      onRenameTake: (id, name) => this.renameTake(id, name),
      loadWave: (entry) => {
        const f = fileById(entry.id);
        return f ? fetch(mediaUrl('wave', f, 'json')).then((r) => (r.ok ? r.json() : null)) : Promise.resolve(null);
      },
    });
    this.onKey = (e) => this.key(e);
    window.addEventListener('keydown', this.onKey);
    this.sideSel = null;    // reveal the selected take (e.g. picked on Export) once
    this.adopt();
    this.renderAll();
  },
  unmount() {
    this.stop();
    this.sideTop = this.side.querySelector('.side-scroll')?.scrollTop || 0;
    saveTakes();
    this.flushOffsets(true);
    window.removeEventListener('keydown', this.onKey);
    this.grid.destroy();
    this.timeline.destroy();
  },
  sessionChanged() {
    this.stop();
    this.flushOffsets(true);
    clearTimeout(this.deferTimer);
    this.sideTop = 0;
    leaveTakes();
  },
  /** True while the user is mid-gesture or typing — a re-render would lose it. */
  busy() {
    if (this.timeline && this.timeline.drag) return true;
    const a = document.activeElement;
    return !!(a && this.rootEl && this.rootEl.contains(a) && (a.tagName === 'INPUT' || a.tagName === 'TEXTAREA'));
  },
  update() {
    clearTimeout(this.deferTimer);
    if (this.busy()) {
      this.deferTimer = setTimeout(() => { if (mounted === this) this.update(); }, 400);
      return;
    }
    this.adopt();
    this.renderAll();
  },
  onJobs() {
    this.renderTransportButtons();
  },

  // ---------------------------------------------------------------- data
  adopt() {
    if (!S.data) return;
    loadTakes();
    for (const [fid, off] of RV.pendingOffsets) {
      const f = fileById(fid);
      if (f) { f.offset = off; f.sync = { ...(f.sync || {}), method: 'manual' }; }
    }
    if (RV.sel && RV.sel.kind === 'take' && !RV.takes.some((t) => t.id === RV.sel.id)) RV.sel = null;
    if (RV.sel && RV.sel.kind === 'file' && !fileById(RV.sel.id)) RV.sel = null;
    const key = `audio.${S.name}`;
    if (RV.audioCam === undefined || RV.audioKey !== key) {
      RV.audioKey = key;
      const ref = videoFiles().find((f) => f.sync?.method === 'reference');
      RV.audioCam = prefs.get(key, ref ? ref.camera : (S.data.cameras[0] || {}).id || null);
    }
    RV.t = Math.min(RV.t, sessionSpan());
  },
  buildGridFiles() {
    this.gridFiles = new Map();
    for (const f of videoFiles()) {
      if (f.offset == null) continue;
      const v = f.probe.video;
      const entry = {
        id: f.id, rel: f.rel, start: f.offset, dur: f.probe.duration, fps: fpsFloat(v.nominal_fps),
        dw: v.display_width, dh: v.display_height,
        proxyUrl: f.assets.proxy ? `${mediaUrl('proxy', f, 'mp4')}&s=${f.assets.proxy_size || ''}` : null,
        thumbUrl: f.assets.thumb ? mediaUrl('thumb', f, 'jpg') : null,
      };
      if (!this.gridFiles.has(f.camera)) this.gridFiles.set(f.camera, []);
      this.gridFiles.get(f.camera).push(entry);
    }
  },
  model() {
    const files = videoFiles().filter((f) => f.offset != null);
    const entry = (f) => ({
      id: f.id, start: f.offset, dur: f.probe.duration, method: f.sync?.method || 'none',
      label: basename(f.rel), wave: !!f.assets.wave, waveKey: `${f.id}:${f.mtime}`,
    });
    const lanes = S.data.cameras.map((c) => ({
      id: c.id, name: c.name, excluded: !c.include, files: files.filter((f) => f.camera === c.id).map(entry),
    }));
    const orphans = files.filter((f) => !cameraById(f.camera));
    if (orphans.length) lanes.push({ id: '_', name: 'unassigned', files: orphans.map(entry) });
    return {
      lanes, takes: RV.takes, span: sessionSpan(), fps: fpsFloat(targetFps()) || 30,
      selected: RV.sel, pendingIn: RV.pendingIn, pendingOut: RV.pendingOut,
    };
  },
  refreshTimeline() {
    this.timeline.setModel(this.model());
  },
  renderAll() {
    if (!S.data) return;
    this.buildGridFiles();
    this.grid.setCameras(S.data.cameras.filter((c) => c.include));
    this.grid.setAudio(RV.audioCam);
    if (RV.playing) this.grid.tick(RV.t);
    else this.grid.show(RV.t);
    this.refreshTimeline();
    this.timeline.setPlayhead(RV.t);
    this.renderSide();
    this.renderInspector();
    this.renderTransport();
  },

  // ---------------------------------------------------------------- transport
  seek(t) {
    RV.t = Math.max(0, Math.min(sessionSpan(), t));
    if (RV.playing) this.grid.tick(RV.t);
    else this.grid.show(RV.t);
    this.timeline.setPlayhead(RV.t);
    this.updateClock();
  },
  play() {
    if (RV.playing) return;
    const span = sessionSpan();
    if (RV.t >= span - 0.01) RV.t = 0;
    RV.playing = true;
    this.grid.play(RV.t, RV.speed);
    this.last = performance.now();
    const loop = (now) => {
      if (!RV.playing) return;
      RV.t = Math.min(span, RV.t + ((now - this.last) / 1000) * RV.speed);
      this.last = now;
      this.grid.tick(RV.t);
      this.timeline.setPlayhead(RV.t);
      this.updateClock();
      if (RV.t >= span) this.stop();
      else this.raf = requestAnimationFrame(loop);
    };
    this.raf = requestAnimationFrame(loop);
    this.renderTransportButtons();
  },
  stop() {
    if (!RV.playing) return;
    RV.playing = false;
    cancelAnimationFrame(this.raf);
    this.grid.pause(RV.t);
    this.renderTransportButtons();
  },
  toggle() { if (RV.playing) this.stop(); else this.play(); },
  step(d) {
    if (RV.playing) this.stop();
    const fps = fpsFloat(targetFps()) || 30;
    this.seek(Math.round((RV.t + d) * fps) / fps);
  },
  jumpTake(dir) {
    const sorted = [...RV.takes].sort((a, b) => a.start - b.start);
    const c = dir > 0 ? sorted.find((t) => t.start > RV.t + 0.01) : [...sorted].reverse().find((t) => t.start < RV.t - 0.5);
    if (c) { this.select({ kind: 'take', id: c.id }); this.seek(c.start); }
  },
  updateClock() {
    if (!this.tcEl) return;
    const fps = targetFps();
    this.tcEl.textContent = fmtTC(RV.t, fps);
    this.frEl.textContent = `frame ${Math.floor(RV.t * fpsFloat(fps) + 1e-6)} @ ${fpsLabel(fps)}`;
  },
  renderTransport() {
    const T = this.transport;
    T.innerHTML = '';
    this.playBtn = h('button', { class: 'tb play', title: 'Play / pause [space]', onclick: () => this.toggle() });
    this.tcEl = h('span', { class: 'tc' });
    this.frEl = h('span', { class: 'fr' });
    this.syncBtn = h('button', { class: 'tb', title: 'Place every clip on the timeline by audio', onclick: () => this.runSync() }, icon('wave-square'), 'Sync');
    this.autoBtn = h('button', { class: 'tb', title: 'Propose takes from clip clusters', onclick: () => this.autoTakes() }, icon('wand-magic-sparkles'), 'Auto-detect takes');
    this.addBtn = h('button', { class: 'tb', title: 'Create a take from the I/O marks [N]', onclick: () => this.createFromPending() }, icon('plus'), 'Add take', h('kbd', null, 'N'));
    const cams = S.data.cameras.filter((c) => c.include);
    T.append(
      h('div', { class: 'grp' },
        h('button', { class: 'tb sq', title: 'Previous take', onclick: () => this.jumpTake(-1) }, icon('backward-step')),
        this.playBtn,
        h('button', { class: 'tb sq', title: 'Next take', onclick: () => this.jumpTake(1) }, icon('forward-step'))),
      this.tcEl, this.frEl,
      h('div', { class: 'grp' }, this.syncBtn, this.autoBtn,
        h('button', { class: 'tb', title: 'Fit the whole session [F]', onclick: () => this.timeline.fit() }, icon('arrows-left-right-to-line'), 'Fit')),
      h('span', { class: 'spacer' }),
      h('select', {
        title: 'Audio from', onchange: (e) => {
          RV.audioCam = e.target.value || null;
          prefs.set(RV.audioKey, RV.audioCam);
          this.grid.setAudio(RV.audioCam);
        },
      }, h('option', { value: '' }, 'audio: muted'),
      cams.map((c) => h('option', { value: c.id, selected: c.id === RV.audioCam }, `audio: ${c.name}`))),
      h('div', { class: 'grp' },
        h('button', { class: `tb ${RV.pendingIn != null ? 'on' : ''}`, title: 'Mark in [I]', onclick: () => this.markIn() }, icon('arrow-right-to-bracket'), 'Mark in', h('kbd', null, 'I')),
        h('button', { class: `tb ${RV.pendingOut != null ? 'on' : ''}`, title: 'Mark out [O]', onclick: () => this.markOut() }, 'Mark out', h('kbd', null, 'O'), icon('arrow-right-from-bracket')),
        this.addBtn),
      h('button', {
        class: 'tb', title: 'Playback speed', style: { minWidth: '56px', justifyContent: 'center' },
        onclick: () => {
          RV.speed = SPEEDS[(SPEEDS.indexOf(RV.speed) + 1) % SPEEDS.length];
          this.grid.setSpeed(RV.speed);
          this.renderTransport();
        },
      }, `${RV.speed}×`));
    this.renderTransportButtons();
    this.updateClock();
  },
  renderTransportButtons() {
    if (!this.playBtn) return;
    this.playBtn.innerHTML = '';
    this.playBtn.append(icon(RV.playing ? 'pause' : 'play'));
    this.syncBtn.disabled = !!activeJob('sync');
    this.addBtn.hidden = !(RV.pendingIn != null && RV.pendingOut != null && !RV.sel);
  },

  // ---------------------------------------------------------------- selection
  select(sel) {
    RV.sel = sel;
    if (sel) RV.pendingIn = RV.pendingOut = null;
    this.refreshTimeline();
    this.renderSide();
    this.renderInspector();
    this.renderTransport();
  },

  // ---------------------------------------------------------------- keys
  key(e) {
    if (S.step !== 'review') return;
    const tag = e.target.tagName;
    if (tag === 'INPUT' || tag === 'SELECT' || tag === 'TEXTAREA' || e.target.isContentEditable) return;
    if (document.getElementById('modal-root').childElementCount) return;
    const fps = fpsFloat(targetFps()) || 30;
    const k = e.key;
    if (k === ' ') { e.preventDefault(); this.toggle(); }
    else if (k === 'ArrowLeft' || k === 'ArrowRight') {
      e.preventDefault();
      this.step((e.shiftKey ? 1 : 1 / fps) * (k === 'ArrowLeft' ? -1 : 1));
    } else if (k === 'Home') { e.preventDefault(); this.step(-RV.t); }
    else if (k === 'End') { e.preventDefault(); if (RV.playing) this.stop(); this.seek(sessionSpan()); }
    else if ((e.ctrlKey || e.metaKey) && k.toLowerCase() === 'z') { e.preventDefault(); this.undoTakes(); }
    else if (e.ctrlKey || e.metaKey || e.altKey) return;
    else if (k === 'i' || k === 'I') this.markIn();
    else if (k === 'o' || k === 'O') this.markOut();
    else if (k === 'n' || k === 'N') this.createFromPending();
    else if (k === 'f' || k === 'F') this.timeline.fit();
    else if (k === 'j' || k === 'J') this.stepTake(-1);
    else if (k === 'l' || k === 'L') this.stepTake(1);
    else if (k === 'k' || k === 'K') { if (RV.sel) this.select(null); }
    else if (k === 'Enter') {
      const tk = this.selectedTake();
      if (tk && tag !== 'BUTTON' && tag !== 'A') { e.preventDefault(); this.timeline.startRename(tk); }
    }
    else if (k === 'Delete' || k === 'Backspace') {
      if (RV.sel && RV.sel.kind === 'take') { e.preventDefault(); this.deleteTake(RV.sel.id); }
    } else if (k === 'Escape') {
      RV.pendingIn = RV.pendingOut = null;
      this.select(null);
    }
  },

  // ---------------------------------------------------------------- takes
  selectedTake() { return RV.sel && RV.sel.kind === 'take' ? RV.takes.find((t) => t.id === RV.sel.id) : null; },
  /** J / L: the previous / next take — from the selected one, else from the playhead. */
  stepTake(dir) {
    const list = [...RV.takes].sort((a, b) => a.start - b.start);
    const cur = this.selectedTake();
    let tk;
    if (cur) tk = list[list.indexOf(cur) + dir];
    else if (dir > 0) tk = list.find((t) => t.start > RV.t + 1e-6);
    else tk = [...list].reverse().find((t) => t.start < RV.t - 1e-6);
    if (!tk) return;
    this.select({ kind: 'take', id: tk.id });
    if (RV.playing) this.stop();
    this.seek(tk.start);
  },
  markIn() {
    const fps = fpsFloat(targetFps()) || 30;
    const tk = this.selectedTake();
    if (tk) {
      tk.start = Math.min(RV.t, tk.end - 2 / fps);
      this.commitTakes();
      return;
    }
    RV.pendingIn = RV.t;
    if (RV.pendingOut != null && RV.pendingOut <= RV.pendingIn) RV.pendingOut = null;
    this.refreshTimeline();
    this.renderTransport();
  },
  markOut() {
    const fps = fpsFloat(targetFps()) || 30;
    const tk = this.selectedTake();
    if (tk) {
      tk.end = Math.max(RV.t, tk.start + 2 / fps);
      this.commitTakes();
      return;
    }
    if (RV.pendingIn != null && RV.t <= RV.pendingIn) { toast('Out mark must be after the in mark'); return; }
    RV.pendingOut = RV.t;
    this.refreshTimeline();
    this.renderTransport();
  },
  createFromPending() {
    if (RV.pendingIn == null || RV.pendingOut == null || RV.pendingOut <= RV.pendingIn) {
      toast('Set an in mark (I) and an out mark (O) first');
      return;
    }
    const a = RV.pendingIn;
    const b = RV.pendingOut;
    RV.pendingIn = RV.pendingOut = null;
    this.createTake(a, b);
  },
  nextTakeName() {
    let n = 0;
    for (const t of RV.takes) {
      const m = /^take_(\d+)$/.exec(t.name);
      if (m) n = Math.max(n, Number(m[1]));
    }
    return `take_${pad2(Math.max(n, 0) + 1)}`;
  },
  createTake(a, b) {
    const id = nextFree('t', new Set(RV.takes.map((t) => t.id)));
    RV.takes.push({ id, name: this.nextTakeName(), start: a, end: b, export: true });
    RV.takes.sort((x, y) => x.start - y.start);
    RV.sel = { kind: 'take', id };
    this.commitTakes();
  },
  renameTake(id, name) {
    const tk = RV.takes.find((t) => t.id === id);
    if (!tk) return;
    tk.name = name.trim().replace(/\s+/g, '_').slice(0, 80) || tk.name;
    this.commitTakes();
  },
  deleteTake(id) {
    RV.takes = RV.takes.filter((t) => t.id !== id);
    if (RV.sel && RV.sel.id === id) RV.sel = null;
    this.commitTakes();
    toast('Take deleted — Ctrl+Z to undo');
  },
  undoTakes() {
    if (!RV.undo.length) { toast('Nothing to undo'); return; }
    RV.takes = RV.undo.pop();
    RV.committed = clone(RV.takes);
    if (RV.sel && RV.sel.kind === 'take' && !RV.takes.some((t) => t.id === RV.sel.id)) RV.sel = null;
    queueTakesSave();
    this.takesChanged();
  },
  commitTakes() {
    commitTakeEdit();
    this.takesChanged();
  },
  takesChanged() {
    this.refreshTimeline();
    this.renderSide();
    this.renderInspector();
    this.renderTransport();
  },
  renderSideSoon() {
    if (this.sideRaf) return;
    this.sideRaf = requestAnimationFrame(() => {
      this.sideRaf = null;
      this.renderSide();
      this.renderInspector();
    });
  },
  async autoTakes() {
    if (RV.takes.length) {
      const ok = await confirmModal({
        title: 'Replace takes?',
        body: `Auto-detect proposes takes from clusters of clips. This replaces your ${RV.takes.length} existing take(s) (Ctrl+Z restores them).`,
        ok: 'Replace takes',
      });
      if (!ok) return;
    }
    try {
      await saveTakes();
      const d = await POST(sUrl('/takes/auto'), { replace: true });
      RV.undo.push(clone(RV.takes));
      S.data = d;
      RV.takes = clone(d.takes);
      RV.committed = clone(RV.takes);
      RV.sel = null;
      toast(d.takes.length ? `Found ${d.takes.length} take(s)` : 'No takes found — are clips placed on the timeline?', d.takes.length ? 'ok' : '');
      this.renderAll();
    } catch (e) { toast(e.message, 'error'); }
  },

  // ---------------------------------------------------------------- offsets
  setOffset(f, off) {
    off = Math.round(off * 1e6) / 1e6;
    f.offset = off;
    if (f.sync?.method !== 'manual') f.sync = { method: 'manual', was: f.sync?.method || 'none' };
    RV.pendingOffsets.set(f.id, off);
    clearTimeout(this.offTimer);
    this.offTimer = setTimeout(() => this.flushOffsets(), 350);
    this.buildGridFiles();
    if (RV.playing) this.grid.tick(RV.t); else this.grid.show(RV.t);
    this.refreshTimeline();
    this.renderInspector();
    this.renderSide();
  },
  async flushOffsets(beacon = false) {
    clearTimeout(this.offTimer);
    const items = [...RV.pendingOffsets.entries()];
    RV.pendingOffsets.clear();
    for (const [fid, offset] of items) {
      const url = sUrl(`/files/${fid}/offset`);
      if (beacon) {
        fetch(url, { method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ offset }) }).catch(() => {});
        continue;
      }
      try {
        await PUT(url, { offset });
      } catch (e) {
        toast(`Couldn't save offset: ${e.message}`, 'error');
      }
    }
  },
  async resync(f, refId) {
    if (!refId) return;
    const name = S.name;
    try {
      await this.flushOffsets();
      let job = await POST(sUrl(`/files/${f.id}/resync`), { ref_fid: refId });
      pollSoon();
      while (job.status === 'queued' || job.status === 'running') {
        await new Promise((r) => setTimeout(r, 300));
        job = await GET(`/api/jobs/${job.id}`);
      }
      if (S.name !== name) return;
      if (job.status !== 'done') { toast(`Re-sync failed: ${job.error || job.status}`, 'error'); return; }
      const r = job.result;
      toast(`${basename(f.rel)} → ${fmtOffset(r.offset)} (score ${r.score.toFixed(3)}, ratio ${r.ratio.toFixed(2)})${r.accepted ? '' : ' — weak match, check it'}`, r.accepted ? 'ok' : '');
      await refreshSession();
    } catch (e) { toast(e.message, 'error'); }
  },
  async runSync() {
    await this.flushOffsets();
    await saveTakes();
    await startJob('/sync', { resync_manual: false });
  },

  // ---------------------------------------------------------------- takes sidebar (left)
  renderSide() {
    const A = this.side;
    const top = A.querySelector('.side-scroll')?.scrollTop ?? this.sideTop ?? 0;   // re-rendering must not jump back up
    A.innerHTML = '';
    const open = prefs.get('side', true);
    A.className = `side ${open ? '' : 'collapsed'}`;
    if (!open) {
      A.append(h('button', { class: 'side-expand', title: 'Expand sidebar', onclick: () => { prefs.set('side', true); this.renderSide(); } },
        icon('angles-right'), h('span', null, 'Takes')));
      return;
    }
    const fps = targetFps();
    const camIds = exportCams();
    const takes = h('section', null,
      h('div', { class: 'section-title' }, 'Takes',
        h('div', { class: 'row', style: { gap: '10px' } },
          h('span', { class: 'note' }, 'I / O to mark'),
          h('button', { class: 'mini-btn', title: 'Collapse sidebar', onclick: () => { prefs.set('side', false); this.renderSide(); } }, icon('angles-left')))));
    if (!RV.takes.length) {
      takes.append(h('div', { class: 'note', style: { lineHeight: '1.5' } },
        'No takes yet. Drag in the takes row, mark I / O and press N, or use Auto-detect takes.'));
    } else {
      const nOn = RV.takes.filter((t) => t.export).length;
      const setAll = (on) => { RV.takes.forEach((t) => { t.export = on; }); this.commitTakes(); };
      takes.append(h('div', { class: 'row', style: { gap: '6px' } },
        h('button', { class: 'btn xs', disabled: nOn === RV.takes.length, onclick: () => setAll(true) }, 'Select all'),
        h('button', { class: 'btn xs', disabled: !nOn, onclick: () => setAll(false) }, 'Deselect all'),
        h('span', { class: 'spacer' }),
        h('span', { class: 'note' }, `${nOn}/${RV.takes.length} to export`)));
    }
    for (const tk of RV.takes) {
      const sel = RV.sel && RV.sel.kind === 'take' && RV.sel.id === tk.id;
      const cov = coverageText(takeCoverage(tk, camIds));
      takes.append(h('div', {
        class: `take-row ${sel ? 'sel' : ''}`,
        onclick: () => { this.select({ kind: 'take', id: tk.id }); if (RV.playing) this.stop(); this.seek(tk.start); },
      },
      h('div', { class: 'top' },
        h('span', {
          class: `check ${tk.export ? 'on' : ''}`, title: 'Export this take',
          onclick: (e) => { e.stopPropagation(); tk.export = !tk.export; this.commitTakes(); },
        }, icon('check')),
        h('input', {
          class: 'input ghost', value: tk.name, spellcheck: 'false',
          onclick: (e) => e.stopPropagation(),
          onkeydown: (e) => { if (e.key === 'Enter') e.target.blur(); },
          onchange: (e) => this.renameTake(tk.id, e.target.value),
        }),
        h('button', { class: 'x', title: 'Delete take', onclick: (e) => { e.stopPropagation(); this.deleteTake(tk.id); } }, icon('xmark'))),
      h('span', { class: 'sub' }, `${fmtTC(tk.start, fps)} → ${fmtTC(tk.end, fps)} · ${(tk.end - tk.start).toFixed(1)}s`),
      h('span', { class: `cov ${cov.ok ? 'ok' : 'warn'}` }, cov.text)));
    }
    const scroll = h('div', { class: 'side-scroll' }, takes);
    A.append(scroll, h('div', { class: 'side-foot' },
      h('button', { class: 'btn primary', onclick: () => go('export') }, 'Continue to export', icon('arrow-right'))));
    scroll.scrollTop = top;
    // a take selected elsewhere (timeline, J / L, Export) is scrolled into view
    const selId = RV.sel && RV.sel.kind === 'take' ? RV.sel.id : null;
    const row = selId && selId !== this.sideSel ? scroll.querySelector('.take-row.sel') : null;
    if (row) {
      const r = row.getBoundingClientRect();
      const b = scroll.getBoundingClientRect();
      if (r.top < b.top) scroll.scrollTop -= b.top - r.top + 8;
      else if (r.bottom > b.bottom) scroll.scrollTop += r.bottom - b.bottom + 8;
    }
    this.sideSel = selId;
  },

  // ---------------------------------------------------------------- inspector (right)
  renderInspector() {
    const I = this.insp;
    const key = RV.sel ? `${RV.sel.kind}:${RV.sel.id}` : '';
    const top = key === this.inspKey ? I.scrollTop : 0;
    this.inspKey = key;
    I.innerHTML = '';
    const open = prefs.get('insp', true);
    I.className = `inspector ${open ? '' : 'collapsed'}`;
    if (!open) {
      I.append(h('button', { class: 'side-expand', title: 'Show inspector', onclick: () => { prefs.set('insp', true); this.renderInspector(); this.grid.layout(); } },
        icon('angles-left'), h('span', null, 'Inspector')));
      return;
    }
    const collapse = h('button', { class: 'mini-btn', title: 'Hide inspector', onclick: () => { prefs.set('insp', false); this.renderInspector(); setTimeout(() => this.grid.layout(), 320); } }, icon('angles-right'));
    const sel = RV.sel;
    if (sel && sel.kind === 'file' && fileById(sel.id)) this.inspFile(I, fileById(sel.id), collapse);
    else if (sel && sel.kind === 'take' && this.selectedTake()) this.inspTake(I, this.selectedTake(), collapse);
    else this.inspSession(I, collapse);
    I.scrollTop = top;
  },
  inspSession(I, collapse) {
    const vids = videoFiles();
    const counts = {};
    for (const f of vids) {
      const m = f.offset == null ? 'none' : (f.sync?.method || 'none');
      counts[m] = (counts[m] || 0) + 1;
    }
    const st = S.data.state;
    I.append(h('h3', null, 'Session', collapse),
      h('div', { class: 'props' },
        h('span', null, 'name'), h('span', null, S.name),
        h('span', null, 'files'), h('span', null, `${vids.length}`),
        h('span', null, 'cameras'), h('span', null, `${S.data.cameras.length} (${S.data.cameras.filter((c) => c.include).length} shown)`),
        h('span', null, 'length'), h('span', null, fmtClock(sessionSpan())),
        h('span', null, 'target fps'), h('span', null, `${fpsLabel(targetFps())}`),
        h('span', null, 'synced'), h('span', { class: st.synced_at ? 'ok' : 'warn' }, st.synced_at ? fmtDate(st.synced_at) : 'not yet — press Sync')),
      h('div', { class: 'insp-sec' },
        h('span', { class: 'lbl' }, 'placement'),
        Object.keys(METHOD_LABEL).filter((m) => counts[m]).map((m) => h('div', { class: 'row', style: { gap: '8px', fontSize: '12px' } },
          h('span', { class: 'dot sq', style: { background: METHOD_COLORS[m] } }), h('span', { style: { flex: 1 } }, METHOD_LABEL[m]), h('span', { class: 'muted' }, counts[m])))),
      h('div', { class: 'insp-sec' },
        h('span', { class: 'lbl' }, 'keys'),
        h('div', { class: 'note', style: { lineHeight: '1.7', opacity: 0.75 } },
          'space play · ←/→ frame · shift ±1 s', h('br'), 'I / O in/out · N new take · del delete', h('br'), 'J / L prev/next take · K deselect · enter rename', h('br'),
          'home/end · F fit · ctrl+Z undo takes · esc deselect')),
      h('span', { class: 'note' }, 'Click a clip bar to nudge its offset, or a take to edit it.'));
  },
  inspFile(I, f, collapse) {
    const fps = fpsFloat(targetFps()) || 30;
    const p = f.probe;
    const v = p.video;
    const sync = f.sync || { method: 'none' };
    const method = f.offset == null ? 'none' : sync.method;
    const ref = sync.ref ? fileById(sync.ref) : null;
    const offInput = h('input', {
      class: 'input sm', type: 'number', step: '0.001', value: f.offset != null ? f.offset.toFixed(3) : '',
      onchange: (e) => { const v2 = Number(e.target.value); if (isFinite(v2)) this.setOffset(f, v2); },
    });
    const nudge = (d) => this.setOffset(f, (f.offset || 0) + d);
    const candidates = videoFiles().filter((g) => g.id !== f.id && g.offset != null && g.probe.audio);
    const defRef = (ref && ref.id) || (videoFiles().find((g) => g.sync?.method === 'reference') || candidates[0] || {}).id;
    const refSel = h('select', { class: 'input sm', style: { flex: 1 } },
      candidates.map((g) => h('option', { value: g.id, selected: g.id === defRef }, `${cameraById(g.camera)?.name || '?'} · ${basename(g.rel)}`)));
    I.append(h('h3', null, h('span', { class: 'ellipsis', title: f.rel }, basename(f.rel)), collapse),
      h('div', { class: 'props' },
        h('span', null, 'camera'), h('span', null, cameraById(f.camera)?.name || '—'),
        h('span', null, 'length'), h('span', null, `${fmtDur(p.duration)} · ${v.display_width}×${v.display_height}`),
        h('span', null, 'fps'), h('span', { class: v.nominal_fps === targetFps() ? '' : 'warn' }, `${fpsLabel(v.nominal_fps)}${v.vfr_suspect ? ' · VFR' : ''}`),
        h('span', null, 'audio'), h('span', { class: p.audio ? '' : 'warn' }, p.audio ? 'yes' : 'none'),
        h('span', null, 'created'), h('span', null, fmtDate(p.creation_time)),
        h('span', null, 'sync'), h('span', { style: { color: METHOD_COLORS[method] } }, METHOD_LABEL[method] + (sync.via ? ` · ${sync.via}` : '')),
        sync.reason ? [h('span', null, 'why'), h('span', { class: 'warn' }, sync.reason)] : null,
        sync.score != null ? [h('span', null, 'score'), h('span', null, `${sync.score.toFixed(3)} · ratio ${sync.ratio != null ? sync.ratio.toFixed(2) : '—'}`)] : null,
        sync.clock_dev != null ? [h('span', null, 'vs clock'), h('span', null, `${sync.clock_dev >= 0 ? '+' : '−'}${Math.abs(sync.clock_dev).toFixed(3)} s from its camera clock`)] : null,
        (sync.flags || []).length ? [h('span', null, 'flags'), h('span', { class: 'warn' }, sync.flags.join(' · '))] : null,
        ref ? [h('span', null, 'against'), h('span', { title: ref.rel }, basename(ref.rel))] : null),
      h('div', { class: 'insp-sec' },
        h('span', { class: 'lbl' }, 'offset (session time of file start, s)'),
        h('div', { class: 'row', style: { gap: '6px', flexWrap: 'nowrap' } }, offInput,
          h('button', { class: 'btn xs', title: 'Jump to clip start', onclick: () => this.seek(f.offset || 0) }, icon('arrow-right-to-bracket'))),
        h('div', { class: 'nudge' },
          [[-1, '−1 s'], [-10 / fps, '−10 f'], [-1 / fps, '−1 f'], [1 / fps, '+1 f'], [10 / fps, '+10 f'], [1, '+1 s']]
            .map(([d, l]) => h('button', { onclick: () => nudge(d), title: d > 0 ? 'move clip later' : 'move clip earlier' }, l))),
        h('span', { class: 'note' }, 'nudging marks the clip as manual; Sync won\'t override it')),
      h('div', { class: 'insp-sec' },
        h('span', { class: 'lbl' }, 'Re-sync against…'),
        p.audio && candidates.length
          ? h('div', { class: 'row', style: { gap: '6px', flexWrap: 'nowrap' } }, refSel,
            h('button', { class: 'btn xs', onclick: () => this.resync(f, refSel.value) }, icon('wave-square'), 'Go'))
          : h('span', { class: 'note' }, p.audio ? 'no other placed file with audio' : 'this file has no audio — nudge it by eye')));
  },
  inspTake(I, tk, collapse) {
    const fps = fpsFloat(targetFps()) || 30;
    const cov = takeCoverage(tk, exportCams());
    const num = (key) => h('input', {
      class: 'input sm', type: 'number', step: (1 / fps).toFixed(4), value: tk[key].toFixed(3),
      onchange: (e) => {
        const v2 = Number(e.target.value);
        if (!isFinite(v2)) return;
        if (key === 'start') tk.start = Math.min(v2, tk.end - 2 / fps);
        else tk.end = Math.max(v2, tk.start + 2 / fps);
        this.commitTakes();
      },
    });
    I.append(h('h3', null,
      h('input', { class: 'input ghost', value: tk.name, style: { fontWeight: 700, fontSize: '15px', flex: 1 }, onchange: (e) => this.renameTake(tk.id, e.target.value) }),
      collapse),
    h('div', { class: 'props' },
      h('span', null, 'in (s)'), h('div', { class: 'row', style: { gap: '4px', flexWrap: 'nowrap' } }, num('start'), h('button', { class: 'btn xs', title: 'Set in to playhead [I]', onclick: () => this.markIn() }, 'I')),
      h('span', null, 'out (s)'), h('div', { class: 'row', style: { gap: '4px', flexWrap: 'nowrap' } }, num('end'), h('button', { class: 'btn xs', title: 'Set out to playhead [O]', onclick: () => this.markOut() }, 'O')),
      h('span', null, 'length'), h('span', null, `${(tk.end - tk.start).toFixed(3)} s`),
      h('span', null, 'frames'), h('span', null, `${Math.round((tk.end - tk.start) * fps)} @ ${fpsLabel(targetFps())}`),
      h('span', null, 'export'), h('div', { class: 'toggle', onclick: () => { tk.export = !tk.export; this.commitTakes(); } },
        h('span', { class: `check ${tk.export ? 'on' : ''}` }, icon('check')), tk.export ? 'yes' : 'no')),
    h('div', { class: 'insp-sec' },
      h('span', { class: 'lbl' }, 'coverage'),
      cov.map((c) => h('div', { style: { display: 'flex', flexDirection: 'column', gap: '2px', fontSize: '11px', paddingBottom: '6px', borderBottom: '1px solid var(--hairline-soft)' } },
        h('div', { class: 'row', style: { gap: '8px' } },
          h('span', { class: 'dot sq', style: { background: c.missing ? '#E56767' : c.padded ? '#E89768' : '#97d47d' } }),
          h('b', null, c.cam?.name || '?'), h('span', { class: 'spacer' }),
          h('span', { class: 'muted' }, c.missing ? 'no footage — skipped' : c.padded ? 'padded' : 'full')),
        (c.files || []).map((f) => h('span', { class: 'muted ellipsis', title: f.rel }, basename(f.rel))),
        c.padded ? h('span', { class: 'warn' }, padText(c)) : null))),
    h('div', { class: 'row', style: { gap: '6px' } },
      h('button', { class: 'btn sm', onclick: () => this.seek(tk.start) }, icon('backward-step'), 'Go to in'),
      h('button', { class: 'btn sm', onclick: () => this.seek(tk.end) }, 'Go to out', icon('forward-step')),
      h('button', { class: 'btn sm danger', onclick: () => this.deleteTake(tk.id) }, icon('trash'), 'Delete')));
  },
};
screens.review = ReviewScreen;
window.addEventListener('beforeunload', () => {
  saveTakes(true);
  saveOpts(true);
  if (mounted === ReviewScreen) ReviewScreen.flushOffsets(true);
});

// ------------------------------------------------------------------ Export screen
const ExportScreen = {
  mount(root) {
    this.root = h('main', { class: 'page' });
    root.append(this.root);
    this.update();
  },
  unmount() {
    saveTakes();
    saveOpts();
  },
  sessionChanged() { leaveTakes(); },
  update() {
    if (!S.data) return;
    loadTakes();
    const a = document.activeElement;
    if (a && this.root.contains(a) && a.tagName === 'INPUT') return;   // keep a half-typed value
    this.render();
  },
  onJobs() { this.renderAction(); },
  takesEdited() { commitTakeEdit(); this.render(); },
  optsChanged() { queueOptsSave(); this.render(); },

  render() {
    const r = this.root;
    const keep = [r.scrollTop, this.tableBox?.scrollTop || 0, this.treeBox?.scrollTop || 0];  // re-rendering must not jump
    r.innerHTML = '';
    this.tableBox = this.treeBox = null;
    if (!S.data) return;
    const camIds = exportCams();
    this.action = h('div', { class: 'row export-action' });
    r.append(
      h('div', { class: 'page-head' },
        h('h1', null, 'Export'),
        h('p', null, 'Every ticked take × camera becomes one file in ', h('span', { class: 'accent' }, 'output/<take>/'),
          '. All files of a take get the same length: black and silence fill in where a camera has no footage.')),
      this.action,
      RV.takes.length
        ? h('div', { class: 'export-split' }, this.takesTable(camIds), this.settings(camIds))
        : h('div', { class: 'empty' }, 'No takes yet — make them in Review: drag in the takes row, mark I / O and press N, or use Auto-detect takes. ',
          h('button', { class: 'btn sm', onclick: () => go('review') }, 'Open Review', icon('arrow-right'))));
    this.renderAction();
    r.scrollTop = keep[0];
    if (this.tableBox) this.tableBox.scrollTop = keep[1];
    if (this.treeBox) this.treeBox.scrollTop = keep[2];
  },
  /** Files the export would write (cameras without footage in a take are skipped). */
  count(camIds) {
    let files = 0;
    let skipped = 0;
    let length = 0;
    for (const tk of RV.takes) {
      if (!tk.export) continue;
      length += tk.end - tk.start;
      for (const c of takeCoverage(tk, camIds)) if (c.missing) skipped++; else files++;
    }
    return { files, skipped, length };
  },
  takesTable(camIds) {
    const fps = targetFps();
    const cams = S.data.cameras;
    const nOn = RV.takes.filter((t) => t.export).length;
    const { skipped, length } = this.count(camIds);
    const setAll = (on) => { RV.takes.forEach((t) => { t.export = on; }); this.takesEdited(); };
    const cell = (c) => {
      if (c.missing) return h('span', { class: 'xcov err' }, h('span', { class: 'dot sq', style: { background: '#E56767' } }), 'none');
      return h('span', { class: `xcov ${c.padded ? 'warn' : ''}` },
        h('span', { class: 'dot sq', style: { background: c.padded ? '#E89768' : '#97d47d' } }),
        c.padded ? `+${(c.pre + c.gap + c.post).toFixed(1)} s` : 'full',
        c.files.length > 1 ? h('span', { class: 'note' }, `${c.files.length} files`) : null);
    };
    const tip = (c) => (c.missing ? 'no footage — skipped' : [...c.files.map((f) => basename(f.rel)), padText(c)].filter(Boolean).join('\n'));
    const rows = RV.takes.map((tk) => {
      const cov = takeCoverage(tk, cams.map((c) => c.id));
      return h('tr', { class: tk.export ? '' : 'off', onclick: () => { tk.export = !tk.export; this.takesEdited(); } },
        h('td', { class: 'ck' }, h('span', { class: `check ${tk.export ? 'on' : ''}` }, icon('check'))),
        h('td', { class: 'nm', title: tk.name }, tk.name),
        h('td', null, `${fmtTC(tk.start, fps)} → ${fmtTC(tk.end, fps)}`),
        h('td', { class: 'num' }, fmtDur(tk.end - tk.start)),
        cov.map((c, i) => h('td', { class: camIds.includes(cams[i].id) ? '' : 'off', title: tip(c) }, cell(c))),
        h('td', { class: 'ck' }, h('button', {
          class: 'mini-btn', title: 'Show in Review', onclick: (e) => { e.stopPropagation(); this.showInReview(tk); },
        }, icon('eye'))));
    });
    this.tableBox = h('div', { class: 'tablewrap' }, h('table', { class: 'ftable xtable' },
      h('thead', null, h('tr', null,
        h('th', null, ''), h('th', null, 'take'), h('th', null, 'in → out'), h('th', { class: 'num' }, 'length'),
        cams.map((c) => {
          const on = camIds.includes(c.id);
          return h('th', { class: `cam ${on ? '' : 'off'}`, title: on ? 'Leave this camera out' : 'Include this camera', onclick: () => this.toggleCam(c.id) },
            h('span', { class: 'xcov' }, h('span', { class: `check ${on ? 'on' : ''}` }, icon('check')), c.name));
        }),
        h('th', null, ''))),
      h('tbody', null, rows)));
    return h('div', { class: 'col', style: { gap: '10px', minWidth: 0 } },
      h('div', { class: 'row', style: { gap: '6px' } },
        h('button', { class: 'btn xs', disabled: nOn === RV.takes.length, onclick: () => setAll(true) }, 'Select all'),
        h('button', { class: 'btn xs', disabled: !nOn, onclick: () => setAll(false) }, 'Deselect all'),
        h('span', { class: 'note' }, `${nOn}/${RV.takes.length} takes · ${camIds.length}/${cams.length} cameras · ${fmtDur(length)} per camera`),
        skipped ? h('span', { class: 'small warn' }, `${skipped} skipped (no footage)`) : null,
        h('span', { class: 'spacer' }),
        h('span', { class: 'note' }, 'click a take or a camera to leave it out')),
      this.tableBox);
  },
  toggleCam(cid) {
    const ids = exportCams();
    RV.opts.camera_ids = ids.includes(cid) ? ids.filter((x) => x !== cid) : S.data.cameras.map((x) => x.id).filter((x) => x === cid || ids.includes(x));
    this.optsChanged();
  },
  showInReview(tk) {
    RV.sel = { kind: 'take', id: tk.id };
    RV.pendingIn = RV.pendingOut = null;
    RV.t = tk.start;
    go('review');
  },
  settings(camIds) {
    const o = RV.opts;
    const fps = targetFps();
    const nv = o.encoder === 'h264_nvenc';
    const seg = (key, values, label = (v) => String(v)) => h('div', { class: 'seg' },
      values.map((v) => h('button', { class: o[key] === v ? 'on' : '', onclick: () => { o[key] = v; this.optsChanged(); } }, label(v))));
    const toggle = (key, label) => h('div', { class: 'toggle', onclick: () => { o[key] = !o[key]; this.optsChanged(); } },
      h('span', { class: `check ${o[key] ? 'on' : ''}` }, icon('check')), label);
    this.treeBox = this.tree(camIds);
    return h('div', { class: 'export-side' },
      h('div', { class: 'card' },
        h('span', { class: 'card-title' }, 'Output'),
        h('div', { class: 'col', style: { gap: '10px' } },
          h('div', { class: 'kv' }, 'Encoder', seg('encoder', ['libx264', 'h264_nvenc'])),
          nv ? h('div', { class: 'kv' }, 'Preset', seg('nvenc_preset', ['p5', 'p4', 'p3'], (v) => ({ p5: 'p5 quality', p4: 'p4', p3: 'p3 fast' })[v])) : null,
          h('label', { class: 'kv' }, nv ? 'CQ (−2)' : 'CRF',
            h('input', { class: 'input sm', type: 'number', min: '0', max: '51', value: o.crf, onchange: (e) => { o.crf = Math.max(0, Math.min(51, Number(e.target.value) || 17)); this.optsChanged(); } })),
          h('div', { class: 'kv' }, 'Long side', seg('long_side', [0, 1920, 1280, 1080], (v) => (v ? String(v) : 'keep'))),
          h('div', { class: 'kv' }, 'Files at once', seg('parallel', [1, 2, 3])),
          h('div', { class: 'kv' }, 'Layout', seg('layout', ['freemocap', 'flat'])),
          h('div', { class: 'kv' }, 'Target fps', h('span', null, `${fpsLabel(fps)} (session)`)),
          toggle('audio', 'include audio'),
          toggle('overwrite', 'overwrite existing files')),
        h('div', { class: 'col', style: { gap: '6px', lineHeight: '1.5' } },
          h('span', { class: 'note' }, `h264 · ${nv ? `cq ${Number(o.crf) + 2}` : `crf ${o.crf}`} · yuv420p · ${fpsLabel(fps)} fps · ${o.long_side ? `${o.long_side}px long side` : 'source size'}`),
          nv ? h('span', { class: 'note' }, 'NVENC is usually the bottleneck: p3 is about twice as fast as p5 at 4K, slightly bigger files.') : null,
          h('span', { class: 'note' }, 'Files at once only helps if the encoder isn\'t already saturated: a GPU with one NVENC (e.g. RTX 3090) still encodes one file at a time, and libx264 already uses every core.'))),
      h('div', { class: 'card' }, h('span', { class: 'card-title' }, 'Folders'), this.treeBox));
  },
  tree(camIds) {
    const lines = [{ t: `sessions/${S.name}/`, c: '#d3d3d3' }, { t: `├ source/  (${S.data.files.length} files, untouched)`, c: '#7a7a7a' }, { t: '└ output/', c: '#f1f1f1' }];
    const takes = RV.takes.filter((t) => t.export);
    const cams = camIds.map((id) => cameraById(id)).filter(Boolean);
    const fm = RV.opts.layout !== 'flat';
    takes.forEach((tk, i) => {
      const last = i === takes.length - 1;
      const bar = last ? ' ' : '│';
      lines.push({ t: `  ${last ? '└' : '├'} ${tk.name}/`, c: '#f1f1f1' });
      if (fm) lines.push({ t: `  ${bar} ├ take_info.json`, c: '#7a7a7a' }, { t: `  ${bar} └ synchronized_videos/`, c: '#d3d3d3' });
      else lines.push({ t: `  ${bar} ├ take_info.json`, c: '#7a7a7a' });
      cams.forEach((c, j) => {
        const lastC = j === cams.length - 1;
        lines.push({ t: `  ${bar} ${fm ? '  ' : ''}${lastC ? '└' : '├'} ${c.name}_synced.mp4`, c: '#E89768' });
      });
    });
    if (!takes.length || !cams.length) lines.push({ t: '    (nothing selected)', c: '#7a7a7a' });
    return h('div', { class: 'tree export-tree' }, lines.map((l) => h('span', { style: { color: l.c } }, l.t)));
  },
  renderAction() {
    const A = this.action;
    if (!A || !A.isConnected) return;
    A.innerHTML = '';
    const { files } = this.count(exportCams());
    const job = activeJob('export');
    const last = lastJob('export');
    A.append(job
      ? h('button', { class: 'btn primary big', onclick: () => cancelJob(job.id) }, icon('xmark'), 'Cancel export')
      : h('button', { class: 'btn primary big', disabled: !files, onclick: () => this.exportNow() }, icon('file-export'),
        files ? `Export ${files} file${files === 1 ? '' : 's'}` : 'Nothing to export'));
    if (job) {
      A.append(h('div', { class: 'col export-status' },
        h('div', { class: 'progress-label' }, h('span', { class: 'ellipsis' }, job.message || 'starting…'), h('span', null, `${Math.round(job.progress * 100)}%`)),
        h('div', { class: 'progress thin' }, h('div', { style: { width: `${job.progress * 100}%` } }))));
    } else if (last) {
      const ok = last.status === 'done' && !(last.result && last.result.errors);
      A.append(h('div', { class: 'col export-status' },
        h('div', { class: 'progress-label' },
          h('span', { class: ok ? 'ok' : 'warn' }, last.status === 'done'
            ? (last.result && last.result.errors ? `done with ${last.result.errors} error(s) — see job log` : `wrote ${last.result ? last.result.written : ''} files`)
            : last.status)),
        h('div', { class: `progress thin ${ok ? 'done' : ''}` }, h('div', { style: { width: '100%' } }))));
    }
    A.append(h('span', { class: 'spacer' }),
      h('button', { class: 'btn sm', onclick: () => reveal('output') }, icon('folder-open'), 'Open output folder'));
  },
  async exportNow() {
    await saveTakes();
    await saveOpts();
    const takeIds = RV.takes.filter((t) => t.export).map((t) => t.id);
    const job = await startJob('/export', { take_ids: takeIds, camera_ids: exportCams(), options: RV.opts });
    if (job) this.renderAction();
  },
};
screens.export = ExportScreen;

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
  await loadSessions();
  if (S.name && S.sessions.some((s) => s.name === S.name)) {
    await openSession(S.name);
  } else {
    S.name = null;
    S.step = 'session';
    render();
  }
  pollSoon();
}
boot();
