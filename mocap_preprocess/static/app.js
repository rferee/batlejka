// mocap-preprocess UI. Plain ES modules, no build step.
import { applyFit } from './grid.js';

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
  reference: '#97d47d', audio: '#d3d3d3', metadata: '#E2B35B', none: '#E56767', manual: '#8880F0',
};

// ------------------------------------------------------------------ header
const STEPS = [
  { id: 'session', label: 'Session' },
  { id: 'process', label: 'Process' },
  { id: 'cameras', label: 'Cameras' },
  { id: 'sync', label: 'Sync' },
  { id: 'review', label: 'Review & export' },
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
    case 'review': return !!st.exported_at;
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
    S.data = null;
    S.jobs = [];
    jobLogs.clear();
  }
  S.name = name;
  prefs.set('session', name);
  if (mounted && mounted.sessionChanged) mounted.sessionChanged();
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
          else if (j.status === 'done' && j.kind !== 'proxies' && j.kind !== 'scan') toast(`${j.label} done`, 'ok');
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
        h('span', { class: 'note' }, 'low-res H.264, 15-frame GOP — only used for the preview')),
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
        h('span', { class: 'note' }, 'score = normalized correlation peak; ratio = peak vs. best peak >0.1 s away. Low ratio usually means looping music matched a bar off.')) : null);
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
    const signs = { ok: h('img', { src: '/static/assets/success-sign-green.svg', alt: '' }), warn: h('img', { src: '/static/assets/warning-sign-orange.svg', alt: '' }), error: icon('circle-xmark') };
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
    this.dirty = false;
    this.saving = true;
    try {
      const d = await PUT(sUrl('/cameras'), body);
      this.saving = false;
      if (!this.dirty) {
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
