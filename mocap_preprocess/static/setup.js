// Setup card on the Session screen: one row per requirement (ffmpeg, GPU, GPU decode,
// NVENC, git, uv, FreeMoCap + its self-test), expanded while anything is missing or
// failing. Also the source of the GPU test results other screens use to disable
// options (gpuCap).
import { h, icon, GET, POST, S, toast, confirmModal, onJobs, go, pollSoon, render } from './app.js';

const SU = { data: null, loading: false, refreshing: false, open: null, el: null, hooked: false, error: null };

const STATUS_ICON = {
  ok: ['circle-check', 'st-ok'], warn: ['triangle-exclamation', 'st-warn'], error: ['circle-xmark', 'st-error'],
  neutral: ['circle-minus', 'st-neutral'], busy: ['spinner', 'st-busy fa-spin'],
};
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
    SU.el = h('section', { class: 'card setup-card', id: 'setup' });
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
  const [ic, cls] = STATUS_ICON[row.status] || STATUS_ICON.neutral;
  const busy = !!live;
  return h('div', { class: `setup-row s-${row.status}` },
    h('span', { class: `st ${cls}` }, h('i', { class: `fa-solid fa-${ic}` })),
    h('span', { class: 'lbl' }, row.label),
    h('div', { class: 'col', style: { gap: '4px', minWidth: 0 } },
      h('span', { class: 'detail' }, row.detail),
      row.status === 'busy' ? h('div', { class: 'progress thin' }, h('div', { style: { width: `${(row.progress || 0) * 100}%` } })) : null,
      row.hint ? h('span', { class: 'hint' }, row.hint) : null),
    h('div', { class: 'acts' }, (row.actions || []).map((a) => h('button', {
      class: `btn xs ${a.id === 'install' || (a.id === 'repair' && row.status === 'error') ? 'primary' : ''} ${a.id === 'remove' ? 'danger' : ''}`,
      disabled: busy, title: busy ? 'Another setup job is running' : '',
      onclick: () => runAction(a.id),
    }, a.label))));
}

function renderCard() {
  const el = SU.el;
  if (!el) return;
  el.innerHTML = '';
  const d = SU.data;
  const recheck = h('button', {
    class: 'btn xs', disabled: SU.refreshing || SU.loading, title: 'Run every check again (GPU tests included)',
    onclick: () => loadSetup(true),
  }, icon('rotate', SU.refreshing ? 'fa-spin' : ''), SU.refreshing ? 'Checking…' : 'Re-check');
  if (!d) {
    el.append(h('div', { class: 'setup-head' },
      SU.error ? h('span', { class: 'st st-error' }, h('i', { class: 'fa-solid fa-circle-xmark' }))
        : h('span', { class: 'st st-busy' }, h('i', { class: 'fa-solid fa-spinner fa-spin' })),
      h('span', { class: 'title' }, 'Setup'),
      h('span', { class: 'sum' }, SU.error ? `couldn't check: ${SU.error}` : 'checking ffmpeg, GPU, FreeMoCap…'),
      h('span', { class: 'spacer' }), recheck));
    return;
  }
  const bad = d.rows.filter((r) => r.status === 'warn' || r.status === 'error');
  const live = setupJob();
  const open = SU.open ?? (bad.length > 0 || !!live);
  const [ic, cls] = live ? STATUS_ICON.busy : bad.some((r) => r.status === 'error') ? STATUS_ICON.error : bad.length ? STATUS_ICON.warn : STATUS_ICON.ok;
  const sum = live ? `${live.label}: ${live.message || 'starting…'} (${Math.round(live.progress * 100)}%)`
    : bad.length ? `${bad.length} thing${bad.length === 1 ? ' needs' : 's need'} attention: ${bad.map((r) => r.label).join(', ')}`
      : `Setup OK · ${d.rows.filter((r) => r.status === 'ok').map((r) => r.label).join(' · ')}`;
  el.classList.toggle('collapsed', !open);
  el.append(h('div', { class: 'setup-head', onclick: (e) => { if (!e.target.closest('button')) { SU.open = !open; renderCard(); } } },
    h('span', { class: `st ${cls}` }, h('i', { class: `fa-solid fa-${ic}` })),
    h('span', { class: 'title' }, 'Setup'),
    h('span', { class: 'sum ellipsis' }, sum),
    h('span', { class: 'spacer' }),
    recheck,
    h('button', { class: 'mini-btn', title: open ? 'Collapse' : 'Expand', onclick: () => { SU.open = !open; renderCard(); } },
      icon(open ? 'chevron-up' : 'chevron-down'))));
  if (open) el.append(h('div', { class: 'setup-rows' }, d.rows.map(rowEl)));
}
