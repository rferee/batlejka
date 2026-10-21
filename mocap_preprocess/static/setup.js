// Setup card on the Session screen: one row per requirement (ffmpeg, GPU, GPU decode,
// NVENC, git, uv, FreeMoCap + its self-test), expanded unless the user folds it.
// Also the source of the GPU test results other screens use to disable
// options (gpuCap).
import { h, icon, GET, POST, S, toast, confirmModal, onJobs, go, pollSoon, render } from './app.js';

const SU = { data: null, loading: false, refreshing: false, open: null, el: null, hooked: false, error: null };

const STATUS_LABEL = { ok: 'ok', warn: 'needs attention', error: 'failed', neutral: 'not applicable', busy: 'working' };
/** The 12px square status mark; `ping` pulses it (the card's overall OK). */
const mark = (status, ping = false) => h('span', {
  class: `su-mark s-${STATUS_LABEL[status] ? status : 'neutral'}`, role: 'img', 'aria-label': STATUS_LABEL[status] || status,
}, ping ? h('span', { class: 'ping', 'aria-hidden': 'true' }) : null);
// The app installs and manages these itself, so their rows show no install paths
// (ffmpeg and git are system-wide: their paths stay).
const MANAGED = new Set(['uv', 'freemocap']);
const isPath = (s) => /^[A-Za-z]:[\\/]|^[\\/]|\\/.test(s.trim());
function shownHint(row) {
  if (!row.hint || !MANAGED.has(row.id)) return row.hint;
  return row.hint.split(' · ').filter((part) => !isPath(part)).join(' · ') || null;
}
const isActive = (j) => j && (j.status === 'queued' || j.status === 'running');
const setupJob = () => S.jobs.find((j) => j.kind === 'setup' && isActive(j));

/** The cached GPU test result for 'decode' or 'nvenc': {ok, error}, or null while unknown. */
export function gpuCap(kind) {
  return SU.data?.gpu?.[kind] || null;
}

/** Fetch /api/setup (refresh re-runs every check); re-renders the app when GPU results change. */
export async function loadSetup(refresh = false) {
  if (!SU.hooked) {
    SU.hooked = true;
    onJobs((list, finished) => {
      if (finished.some((j) => j.kind === 'setup')) loadSetup();
      else if (list.some((j) => j.kind === 'setup' && isActive(j)) || SU.wasBusy) renderCard();
      SU.wasBusy = list.some((j) => j.kind === 'setup' && isActive(j));
    });
  }
  const before = JSON.stringify(SU.data?.gpu || null);
  if (refresh) SU.refreshing = true;
  else SU.loading = true;
  renderCard();
  try {
    SU.data = await GET(`/api/setup${refresh ? '?refresh=1' : ''}`);
    SU.error = null;
  } catch (e) {
    SU.error = e.message;
  } finally {
    SU.loading = SU.refreshing = false;
  }
  renderCard();
  if (JSON.stringify(SU.data?.gpu || null) !== before) render();   // enable/disable GPU options
  return SU.data;
}

/** The card element (one persistent element; the Session screen re-appends it on every render). */
export function setupCard() {
  if (!SU.el) {
    SU.el = h('section', { class: 'home-setup', id: 'setup', 'aria-label': 'Setup' });
    renderCard();
  }
  return SU.el;
}

/** Go to the Session screen and bring the Setup card into view, expanded. */
export function showSetup() {
  SU.open = true;
  go('session');
  renderCard();
  requestAnimationFrame(() => document.getElementById('setup')?.scrollIntoView({ block: 'start', behavior: 'smooth' }));
}

async function runAction(id) {
  if (id === 'remove') {
    const ok = await confirmModal({
      title: 'Remove FreeMoCap?',
      body: 'Deletes .tools/freemocap-v1.8.2 (the FreeMoCap environment, its logs and self-test data). '
        + 'Your sessions, exports and processed takes are not touched. Installing again downloads ~3 GB.',
      ok: 'Remove',
    });
    if (!ok) return;
  }
  try {
    const job = await POST(`/api/setup/freemocap/${id}`);
    S.jobs = S.jobs.filter((j) => j.id !== job.id).concat([job]);
    SU.wasBusy = true;
    pollSoon();
    loadSetup();     // its rows now show the job as busy
  } catch (e) { toast(e.message, 'error'); }
}

function rowEl(r) {
  // a running setup job shows live in its row (the server's copy is from the last fetch)
  const live = setupJob();
  let row = r;
  if (live && r.status === 'busy') {
    row = { ...r, detail: `${live.label}: ${live.message || 'starting…'} (${Math.round(live.progress * 100)}%)`, progress: live.progress };
  }
  const busy = !!live;
  return h('div', { class: `su-row s-${row.status}` },
    mark(row.status),
    h('div', { class: 'su-txt' },
      h('b', null, row.label),
      row.detail ? h('span', null, row.detail) : null,
      row.status === 'busy' ? h('div', { class: 'progress thin' }, h('div', { style: { width: `${(row.progress || 0) * 100}%` } })) : null,
      shownHint(row) ? h('span', { class: 'dim' }, shownHint(row)) : null),
    (row.actions || []).length ? h('div', { class: 'su-acts' }, row.actions.map((a) => h('button', {
      type: 'button',
      class: `btn su-btn ${a.id === 'install' || (a.id === 'repair' && row.status === 'error') ? 'primary' : ''} ${a.id === 'remove' ? 'danger' : ''}`,
      disabled: busy, title: busy ? 'Another setup job is running' : '',
      onclick: () => runAction(a.id),
    }, a.label))) : null);
}

function renderCard() {
  const el = SU.el;
  if (!el) return;
  el.innerHTML = '';
  const d = SU.data;
  const recheck = h('button', {
    type: 'button', class: 'btn su-btn', disabled: SU.refreshing || SU.loading, title: 'Run every check again (GPU tests included)',
    onclick: () => loadSetup(true),
  }, icon('rotate', SU.refreshing ? 'fa-spin' : ''), SU.refreshing ? 'Checking…' : 'Re-check');
  const head = (status, sum, ping, toggle) => h('div', {
    class: 'su-head', onclick: toggle ? (e) => { if (!e.target.closest('button')) toggle(); } : null,
  },
  mark(status, ping),
  h('div', { class: 'su-title' }, h('h2', null, 'Setup'), h('span', { class: 'dim' }, sum)),
  h('div', { class: 'su-acts' }, recheck,
    toggle ? h('button', {
      type: 'button', class: 'btn su-btn su-sq', 'aria-label': SU.openNow ? 'Collapse setup' : 'Expand setup',
      'aria-expanded': SU.openNow ? 'true' : 'false', onclick: toggle,
    }, icon(SU.openNow ? 'chevron-up' : 'chevron-down')) : null));
  if (!d) {
    SU.openNow = false;
    el.append(head(SU.error ? 'error' : 'busy',
      SU.error ? `couldn't check: ${SU.error}` : 'checking ffmpeg, GPU, FreeMoCap…', false, null));
    return;
  }
  const bad = d.rows.filter((r) => r.status === 'warn' || r.status === 'error');
  const live = setupJob();
  const open = SU.open ?? true;   // open unless the user folds it: nothing else competes for its column
  SU.openNow = open;
  const status = live ? 'busy' : bad.some((r) => r.status === 'error') ? 'error' : bad.length ? 'warn' : 'ok';
  const sum = live ? `${live.label}: ${live.message || 'starting…'} (${Math.round(live.progress * 100)}%)`
    : bad.length ? `${bad.length} thing${bad.length === 1 ? ' needs' : 's need'} attention: ${bad.map((r) => r.label).join(', ')}`
      : 'Setup OK · everything this app needs is in place';
  el.classList.toggle('collapsed', !open);
  el.append(head(status, sum, status === 'ok', () => { SU.open = !open; renderCard(); }));
  if (open) el.append(h('div', { class: 'su-rows' }, d.rows.map(rowEl)));
}
