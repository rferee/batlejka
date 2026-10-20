// Mocap step: calibrate the cameras with FreeMoCap 1.8.2, then process the exported takes.
// app.js imports this module, so app.js bindings are only used inside functions (circular import).
import {
  h, icon, S, GET, POST, PUT, sUrl, toast, confirmModal, startJob, activeJob, lastJob, cancelJob,
  reveal, go, fmtDate, fmtDur, prefs, render, pollSoon,
} from './app.js';
import * as setup from './setup.js';

const STAGES = {
  downloading: 'downloading', detecting_board: 'detecting the board', tracking_2d: '2D tracking',
  triangulating: 'triangulating', skeleton: 'skeleton', saving: 'saving', blender: 'Blender export',
};
const GREEN = '#97d47d';
const AMBER = '#E2B35B';
const RED = '#E56767';
const camName = (c) => String(c).replace(/_synced$/, '');
const errColor = (e) => (e == null ? 'var(--fg-2)' : e < 1 ? GREEN : e < 2 ? AMBER : RED);
const dot = (color) => h('span', { class: 'dot sq', style: { background: color } });
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const pad2 = (n) => String(n).padStart(2, '0');
const shortDate = (t) => {
  const d = new Date(t * 1000);
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
};

function openSetup() {
  if (typeof setup.showSetup === 'function') setup.showSetup();
  else go('session');
}

export const MocapScreen = {
  data: null,      // GET /mocap
  name: null,      // session the data belongs to
  form: null,      // calibration form {take_id, board, square, groundplane}
  sel: new Set(),  // take ids ticked for processing
  opts: null,
  live: null,      // job.live of the running mocap_process job

  mount(root) {
    this.root = h('main', { class: 'page' });
    // a background refresh that arrived while typing is applied once the field loses focus
    this.root.addEventListener('focusout', () => setTimeout(() => { if (this.deferred && !this.typing()) this.render(); }, 60));
    root.append(this.root);
    this.render();
    this.load();
  },
  unmount() {
    this.saveOpts();
    this.stopLive();
  },
  sessionChanged() {
    this.saveOpts();
    this.stopLive();
    Object.assign(this, { data: null, name: null, form: null, sel: new Set(), opts: null, live: null, error: null });
  },
  update() { this.load(); },
  onJobs() { this.tick(); },

  typing() {
    const a = document.activeElement;
    return !!(a && this.root && this.root.contains(a) && ['INPUT', 'SELECT', 'TEXTAREA'].includes(a.tagName));
  },

  // ---------------------------------------------------------------- data
  async load() {
    if (!S.name) return;
    if (this.loading) { this.again = true; return; }
    const name = S.name;
    this.loading = true;
    try {
      const d = await GET(sUrl('/mocap'));
      if (name !== S.name) return;
      this.adopt(d, name);
      this.error = null;
      if (!activeJob('mocap_process')) this.live = null;
    } catch (e) {
      if (name !== S.name) return;
      this.error = e.message;
    } finally {
      this.loading = false;
    }
    if (this.again) { this.again = false; this.load(); return; }
    if (this.typing()) this.deferred = true;
    else this.render();
  },
  adopt(d, name) {
    if (this.name !== name) {
      this.name = name;
      this.form = null;
      this.sel = new Set(prefs.get(`mocapSel.${name}`, []));
      this.optsDirty = false;
    }
    this.data = d;
    if (!this.optsDirty) this.opts = { ...(d.options || {}) };
    const ids = d.takes.map((t) => t.take_id);
    const c = d.calibration;
    if (!this.form) {
      this.form = {
        take_id: c && ids.includes(c.take_id) ? c.take_id : d.suggested_calibration_take || ids[0] || null,
        board: c?.board || '7x5',
        square: c?.square_mm != null ? String(c.square_mm) : '',
        groundplane: !!c?.groundplane,
      };
    } else if (!ids.includes(this.form.take_id)) {
      this.form.take_id = d.suggested_calibration_take || ids[0] || null;
    }
  },
  take(id) { return (this.data?.takes || []).find((t) => t.take_id === id); },
  /** The calibration take is left out of the takes table: the saved one, else the one picked in the form. */
  calTakeId() { return this.data?.calibration?.take_id || this.form?.take_id || null; },
  selectable(t) { return !!t && t.take_id !== this.calTakeId() && t.layout_ok && !(t.missing || []).length; },
  rows() { return (this.data?.takes || []).filter((t) => t.take_id !== this.calTakeId()); },
  selected() { return this.rows().filter((t) => this.sel.has(t.take_id) && this.selectable(t)); },
  setSel(ids, on) {
    for (const id of ids) { if (on) this.sel.add(id); else this.sel.delete(id); }
    prefs.set(`mocapSel.${this.name}`, [...this.sel]);
    this.render();
  },

  // ---------------------------------------------------------------- actions
  async calibrate() {
    const f = this.form;
    const job = await this.startMocapJob('/mocap/calibrate', {
      take_id: f.take_id, board: f.board, square_mm: Number(f.square), groundplane: f.groundplane,
    }, 'Delete and calibrate');
    if (job) this.tick();
  },
  async process() {
    const ids = this.selected().map((t) => t.take_id);
    if (!ids.length) return;
    await this.saveOpts();
    await this.startMocapJob('/mocap/process', { take_ids: ids }, 'Delete and process');
  },
  /** POST a mocap job; when the server refuses because of leftover videos (409), list them,
   *  and on confirm send it again with delete_leftovers. */
  async startMocapJob(path, body, okLabel) {
    try {
      const job = await POST(sUrl(path), { ...body, delete_leftovers: !!body.delete_leftovers });
      S.jobs = S.jobs.filter((j) => j.id !== job.id).concat([job]);
      render();
      pollSoon();
      return job;
    } catch (e) {
      const lo = e.status === 409 && e.detail && typeof e.detail === 'object' ? e.detail.leftovers : null;
      if (lo && !body.delete_leftovers) {
        const name = (id) => this.take(id)?.name || id;
        const msg = h('span', { class: 'col', style: { gap: '8px' } },
          h('span', null, 'These videos are in synchronized_videos/ but weren\'t part of the take\'s last export, so FreeMoCap would process them as extra cameras:'),
          Object.entries(lo).map(([id, files]) => h('span', null, h('b', null, name(id)), ': ', files.join(', '))));
        if (await confirmModal({ title: 'Delete leftover videos?', body: msg, ok: okLabel })) {
          return this.startMocapJob(path, { ...body, delete_leftovers: true }, okLabel);
        }
        return null;
      }
      toast(e.detail?.message || e.message, 'error');
      return null;
    }
  },
  optsChanged(rerender = false) {
    this.optsDirty = true;
    clearTimeout(this.optsTimer);
    this.optsTimer = setTimeout(() => this.saveOpts(), 500);
    if (rerender) this.render();
  },
  async saveOpts() {
    clearTimeout(this.optsTimer);
    if (!this.optsDirty || !this.name) return;
    this.optsDirty = false;
    const name = this.name;
    try {
      const d = await PUT(`/api/sessions/${encodeURIComponent(name)}/mocap/options`, this.opts);
      if (this.data && this.name === name) this.data.options = d.options;
    } catch (e) { toast(`Couldn't save mocap options: ${e.message}`, 'error'); }
  },
  async openFolder(dir) {
    try { await POST(sUrl('/reveal'), { what: 'output', sub: dir }); } catch (e) { toast(e.message, 'error'); }
  },

  // ---------------------------------------------------------------- live progress
  tick() {
    if (!this.root || !this.root.isConnected || !this.data) return;
    const job = activeJob('mocap_process');
    if (job) this.startLive(job.id); else this.stopLive();
    this.refreshCalButton();
    this.renderCalStatus();
    this.renderAction();
    this.renderStatuses();
  },
  startLive(id) {
    if (this.liveId === id) return;
    this.stopLive();
    this.liveId = id;
    const poll = async () => {
      try {
        const j = await GET(`/api/jobs/${id}?live=1&log_since=999999999`);
        if (this.liveId !== id) return;
        this.live = j.live || null;
        this.liveJob = j;
        this.renderAction();
        this.renderStatuses();
      } catch { /* job pruned or server restarting */ }
      if (this.liveId === id) this.liveTimer = setTimeout(poll, 600);
    };
    poll();
  },
  stopLive() {
    clearTimeout(this.liveTimer);
    this.liveId = null;
  },

  // ---------------------------------------------------------------- rendering
  render() {
    const r = this.root;
    if (!r) return;
    this.deferred = false;
    const keep = [r.scrollTop, this.tableBox?.scrollTop || 0];
    r.innerHTML = '';
    this.tableBox = this.calStatus = this.action = this.calBtn = null;
    this.statusCells = new Map();
    r.append(h('div', { class: 'page-head' },
      h('h1', null, 'Mocap'),
      h('p', null, 'Calibrate the cameras on the calibration take, then run FreeMoCap 1.8.2 on the other takes: '
        + '2D tracking (MediaPipe, on the CPU), triangulation and the skeleton. Results go into each take\'s folder ',
      h('span', { class: 'accent' }, 'output/<take>/output_data/'), '.')));
    const d = this.data;
    if (!S.name) return;
    if (!d) {
      r.append(h('div', { class: 'empty' }, this.error ? `Couldn't load the mocap state: ${this.error}` : [icon('spinner', 'fa-spin'), ' loading…']));
      return;
    }
    if (!d.freemocap?.ready) {
      r.append(this.notReady(d.freemocap || {}));
      return;
    }
    if (!d.takes.length) {
      r.append(h('div', { class: 'empty' }, 'No exported takes yet — export the calibration take and some takes first. ',
        h('button', { class: 'btn sm', onclick: () => go('export') }, 'Open Export', icon('arrow-right'))));
      return;
    }
    r.append(h('div', { class: 'mocap-split' }, this.calibrationCard(), this.takesPanel()));
    this.tick();
    r.scrollTop = keep[0];
    if (this.tableBox) this.tableBox.scrollTop = keep[1];
  },

  notReady(fm) {
    const broken = fm.state === 'broken';
    return h('div', { class: 'card mocap-setup' },
      h('div', { class: 'row', style: { gap: '10px' } },
        icon(broken ? 'triangle-exclamation' : 'circle-info', broken ? 'err' : 'warn'),
        h('span', { class: 'card-title' }, broken ? 'FreeMoCap 1.8.2 needs a repair' : 'FreeMoCap 1.8.2 isn\'t installed')),
      h('p', { class: 'muted small', style: { margin: 0, lineHeight: '1.6' } },
        broken
          ? `It's installed but its check fails${fm.detail ? `: ${fm.detail}` : ''}. Repair it from the Setup panel.`
          : 'FreeMoCap runs in its own environment (Python 3.12, ~3 GB download), separate from this app. '
            + 'Install it from the Setup panel on the Session screen, then come back here.'),
      h('div', { class: 'row' }, h('button', { class: 'btn primary', onclick: openSetup }, icon('screwdriver-wrench'), 'Open Setup')));
  },

  calibrationCard() {
    const d = this.data;
    const f = this.form;
    const c = d.calibration;
    const seg = (values, cur, set) => h('div', { class: 'seg' },
      values.map((v) => h('button', { class: v === cur ? 'on' : '', onclick: () => { set(v); this.render(); } }, v)));
    this.calBtn = h('button', { class: 'btn primary', onclick: () => this.calibrate() }, icon('crosshairs'), c ? 'Recalibrate' : 'Calibrate');
    this.calHint = h('span', { class: 'note' });
    this.calStatus = h('div', { class: 'col', style: { gap: '6px' } });
    return h('div', { class: 'card mocap-cal' },
      h('span', { class: 'card-title' }, 'Calibration'),
      h('div', { class: 'col', style: { gap: '10px' } },
        h('label', { class: 'kv' }, 'Take',
          h('select', { class: 'input sm', onchange: (e) => { f.take_id = e.target.value; this.render(); } },
            d.takes.map((t) => h('option', { value: t.take_id, selected: t.take_id === f.take_id },
              `${t.name} · ${t.cameras.length} cams · ${fmtDur(t.duration)}`)))),
        h('div', { class: 'kv' }, 'Board', seg(['7x5', '5x3'], f.board, (v) => { f.board = v; })),
        h('label', { class: 'kv' }, 'Square (mm)',
          h('input', {
            class: 'input sm', type: 'number', min: '0', step: '0.1', placeholder: 'required', value: f.square,
            oninput: (e) => { f.square = e.target.value; this.refreshCalButton(); },
          })),
        h('span', { class: 'note', style: { lineHeight: '1.5' } },
          'Edge of one black square on the printed ChArUco board. It sets the units of every output (mm), so measure it.'),
        h('div', { class: 'toggle', onclick: () => { f.groundplane = !f.groundplane; this.render(); } },
          h('span', { class: `check ${f.groundplane ? 'on' : ''}` }, icon('check')), 'Board flat on the floor'),
        h('span', { class: 'note', style: { lineHeight: '1.5' } },
          f.groundplane ? 'The board defines the floor: z = 0 at the board, z up.' : 'Otherwise the first camera is the origin.')),
      h('div', { class: 'row', style: { gap: '12px' } }, this.calBtn, this.calHint),
      this.calStatus,
      c ? this.calibrationResult(c) : null);
  },
  calibrationResult(c) {
    const err = c.reprojection_error_px;
    const ct = this.take(c.take_id);
    const warns = [];
    if (this.data.calibration_stale_reason) warns.push(this.data.calibration_stale_reason);
    else if (!ct) warns.push('The calibration take isn\'t exported any more.');
    else if (ct.exported_at && c.created_at && ct.exported_at > c.created_at + 1) {
      warns.push('The calibration take was re-exported after this calibration — calibrate again.');
    }
    if (err != null && err >= 2) warns.push('Above 2 px: check the square size, that the board was seen by every camera, and that no camera moved.');
    return h('div', { class: 'mocap-result' },
      h('div', { class: 'row', style: { gap: '10px', alignItems: 'baseline' } },
        h('span', { class: 'reproj', style: { color: errColor(err) } }, err == null ? '—' : `${err.toFixed(2)} px`),
        h('span', { class: 'note' }, 'reprojection error')),
      h('div', { class: 'props' },
        h('span', null, 'take'), h('span', null, ct?.name || c.take_id),
        h('span', null, 'board'), h('span', null, `${c.board} · ${c.square_mm} mm${c.groundplane ? ' · on the floor' : ''}`),
        h('span', null, 'cameras'), h('span', null, String((c.cameras || []).length)),
        h('span', null, 'date'), h('span', null, fmtDate(c.created_at)),
        h('span', null, 'FreeMoCap'), h('span', null, c.freemocap || '—')),
      h('div', { class: 'mocap-cams' }, (c.cameras || []).map((x) => h('span', { class: 'tag' }, camName(x)))),
      warns.map((w) => h('div', { class: 'mocap-hint warn' }, icon('triangle-exclamation'), h('span', null, w))));
  },
  refreshCalButton() {
    if (!this.calBtn || !this.calBtn.isConnected) return;
    const f = this.form;
    const sq = Number(f.square);
    const sqOk = f.square !== '' && isFinite(sq) && sq > 0;
    const busy = activeJob('mocap_calibrate') ? 'calibrating…' : activeJob('mocap_process') ? 'wait for processing to finish' : '';
    const reason = busy || (!f.take_id ? 'no exported take' : !sqOk ? 'enter the square size' : '');
    this.calBtn.disabled = !!reason;
    this.calHint.textContent = reason && !activeJob('mocap_calibrate') ? reason : '';
  },
  renderCalStatus() {
    const box = this.calStatus;
    if (!box || !box.isConnected) return;
    box.innerHTML = '';
    const job = activeJob('mocap_calibrate');
    if (job) {
      box.append(
        h('div', { class: 'progress-label' }, h('span', { class: 'ellipsis' }, job.message || 'starting…'), h('span', null, `${Math.round(job.progress * 100)}%`)),
        h('div', { class: 'progress thin' }, h('div', { style: { width: `${job.progress * 100}%` } })),
        h('div', { class: 'row' }, h('button', { class: 'btn xs danger', onclick: () => cancelJob(job.id) }, icon('xmark'), 'Cancel')));
      return;
    }
    const last = lastJob('mocap_calibrate');
    if (last && last.status === 'error') box.append(h('div', { class: 'mocap-hint err' }, icon('circle-xmark'), h('span', null, `Calibration failed: ${last.error}`)));
    else if (last && last.status === 'cancelled') box.append(h('span', { class: 'note' }, 'Calibration cancelled.'));
  },

  takesPanel() {
    const d = this.data;
    const rows = this.rows();
    const pickable = rows.filter((t) => this.selectable(t));
    const nSel = this.selected().length;
    this.action = h('div', { class: 'row mocap-action' });
    const o = this.opts;
    const opts = h('div', { class: 'mocap-opts' },
      h('label', { class: 'row', style: { gap: '8px' } }, 'Processes',
        h('input', {
          class: 'input sm', type: 'number', min: '1', max: String(d.cpu_count || 64), style: { width: '70px' },
          placeholder: String(d.default_processes ?? ''), value: o.processes ?? '',
          onchange: (e) => {
            const v = parseInt(e.target.value, 10);
            o.processes = v > 0 ? Math.min(v, d.cpu_count || v) : null;
            e.target.value = o.processes ?? '';
            this.optsChanged();
          },
        })),
      h('span', { class: 'note' }, `2D tracking: one process per camera at most (${d.cpu_count} CPUs; empty = ${d.default_processes})`),
      h('div', { class: 'toggle', onclick: () => { o.blender = !o.blender; this.optsChanged(true); } },
        h('span', { class: `check ${o.blender ? 'on' : ''}` }, icon('check')), 'Export .blend'),
      o.blender ? h('input', {
        class: 'input sm', style: { flex: '1', minWidth: '240px' }, placeholder: 'blender.exe (empty: FreeMoCap looks for it)',
        value: o.blender_exe || '', spellcheck: 'false',
        onchange: (e) => { o.blender_exe = e.target.value.trim() || null; this.optsChanged(); },
      }) : null);
    const toolbar = h('div', { class: 'row', style: { gap: '6px' } },
      h('button', { class: 'btn xs', disabled: !pickable.length || nSel === pickable.length, onclick: () => this.setSel(pickable.map((t) => t.take_id), true) }, 'Select all'),
      h('button', { class: 'btn xs', disabled: !nSel, onclick: () => this.setSel(rows.map((t) => t.take_id), false) }, 'Deselect all'),
      h('span', { class: 'note' }, `${nSel}/${rows.length} selected`),
      h('span', { class: 'spacer' }),
      h('span', { class: 'note' }, d.calibration ? `cameras compared with the calibration (${d.calibration.cameras.length})` : 'calibrate first to compare cameras'));
    let table;
    if (!rows.length) {
      table = h('div', { class: 'empty' }, 'Only the calibration take is exported — export some takes to process.');
    } else {
      this.tableBox = table = h('div', { class: 'tablewrap' }, h('table', { class: 'ftable mtable' },
        h('thead', null, h('tr', null,
          h('th', null, ''), h('th', null, 'take'), h('th', null, 'cameras'), h('th', { class: 'num' }, 'length'),
          h('th', null, 'status'), h('th', null, 'processed'), h('th', null, ''))),
        h('tbody', null, rows.map((t) => this.takeRow(t)))));
    }
    return h('div', { class: 'col', style: { gap: '14px', minWidth: 0 } },
      h('span', { class: 'card-title' }, 'Takes'), this.action, opts, toolbar, table);
  },
  takeRow(t) {
    const d = this.data;
    const ok = this.selectable(t);
    const on = ok && this.sel.has(t.take_id);
    const m = t.calib_match;
    let cams;
    if (!t.layout_ok) cams = h('span', { class: 'err' }, 'flat layout — export it again');
    else if (!d.calibration || !m) cams = h('span', null, plural(t.cameras.length, 'camera'));
    else if ((m.unknown || []).length) cams = h('span', { class: 'err' }, `not in the calibration: ${m.unknown.map(camName).join(', ')}`);
    else if (m.n < 2) cams = h('span', { class: 'err' }, `${m.n}/${m.of} — needs at least 2 cameras`);
    else if ((m.missing || []).length) cams = h('span', { class: 'warn' }, `${m.n}/${m.of} — no ${m.missing.map(camName).join(', ')}`);
    else cams = h('span', { class: 'ok' }, `${m.n}/${m.of}`);
    const tags = [];
    if ((t.leftovers || []).length) tags.push(h('span', { class: 'tag warn', title: t.leftovers.join('\n') }, `${t.leftovers.length} leftover`));
    if ((t.missing || []).length) tags.push(h('span', { class: 'tag err', title: t.missing.join('\n') }, `${t.missing.length} missing`));
    const status = h('td', null);
    this.statusCells.set(t.take_id, status);
    this.fillStatus(status, t);
    const why = !t.layout_ok ? 'flat layout: FreeMoCap needs synchronized_videos/' : (t.missing || []).length ? 'videos are missing — export the take again' : '';
    return h('tr', { class: `${ok ? 'can' : ''} ${on ? '' : 'off'}`, title: why, onclick: () => { if (ok) this.setSel([t.take_id], !on); } },
      h('td', { class: 'ck' }, h('span', { class: `check ${on ? 'on' : ''} ${ok ? '' : 'dis'}` }, icon('check'))),
      h('td', { class: 'nm', title: t.dir }, t.name),
      h('td', { class: 'cams', title: (t.camera_names || t.cameras).map(camName).join('\n') }, cams, tags.length ? ' ' : null, tags),
      h('td', { class: 'num' }, fmtDur(t.duration)),
      status,
      h('td', { title: t.processed_at ? fmtDate(t.processed_at) : '' },
        t.processed_at ? `${shortDate(t.processed_at)}${t.seconds ? ` · ${fmtDur(t.seconds)}` : ''}` : '—'),
      h('td', { class: 'ck' }, h('button', {
        class: 'mini-btn', title: `Open output/${t.dir}/`, onclick: (e) => { e.stopPropagation(); this.openFolder(t.dir); },
      }, icon('folder-open'))));
  },
  fillStatus(td, t) {
    td.innerHTML = '';
    const lv = this.live?.takes?.[t.take_id];
    const st = (color, text, extra = null, title = '') => h('div', { class: 'mocap-status', title },
      h('span', { class: 'mocap-st' }, dot(color), text), extra);
    let el;
    if (lv && lv.status === 'running') {
      const f = Math.max(0, Math.min(1, lv.fraction || 0));
      el = st('#E89768', `${STAGES[lv.stage] || lv.stage || 'starting'} · ${Math.round(f * 100)}%`,
        h('div', { class: 'mini-bar' }, h('div', { style: { width: `${f * 100}%` } })));
    } else if (lv && lv.status === 'queued') el = st('var(--rf-darkgray)', 'queued');
    else if (lv && lv.status === 'failed') el = st(RED, 'failed', h('span', { class: 'note ellipsis err-note' }, lv.error || ''), lv.error || '');
    else if (lv && lv.status === 'done') el = st(GREEN, 'done');
    else if (t.status === 'done') el = st(GREEN, `done${t.subset ? ' · subset' : ''}`, null, t.subset ? 'processed with a calibration of only this take\'s cameras' : '');
    else if (t.status === 'stale') el = st(AMBER, 'stale', h('span', { class: 'note' }, t.stale_reason || 'reprocess it'), t.stale_reason || '');
    else if (t.status === 'failed') el = st(RED, 'failed', h('span', { class: 'note ellipsis err-note' }, t.error || ''), t.error || '');
    else if (t.status === 'running') el = st('#E89768', 'running');
    else el = st('var(--rf-darkgray)', 'not processed');
    td.append(el);
  },
  renderStatuses() {
    if (!this.statusCells || !this.data) return;
    for (const t of this.data.takes) {
      const td = this.statusCells.get(t.take_id);
      if (td && td.isConnected) this.fillStatus(td, t);
    }
  },
  /** Why "Process" is disabled right now, or '' if it isn't. */
  blockReason(sel) {
    if (!this.data.calibration) return 'Calibrate the cameras first';
    if (activeJob('mocap_calibrate')) return 'Calibrating…';
    if (!sel.length) return 'Tick the takes to process';
    const bad = sel.find((t) => (t.calib_match?.unknown || []).length);
    if (bad) return `${bad.name}: a camera isn't in the calibration`;
    const few = sel.find((t) => t.calib_match && t.calib_match.n < 2);
    if (few) return `${few.name}: needs at least 2 calibrated cameras`;
    return '';
  },
  renderAction() {
    const A = this.action;
    if (!A || !A.isConnected) return;
    A.innerHTML = '';
    const job = activeJob('mocap_process');
    if (job) {
      const j = this.liveJob && this.liveJob.id === job.id ? this.liveJob : job;
      A.append(
        h('button', { class: 'btn primary big', onclick: () => cancelJob(job.id) }, icon('xmark'), 'Cancel'),
        h('div', { class: 'col mocap-progress' },
          h('div', { class: 'progress-label' }, h('span', { class: 'ellipsis' }, j.message || 'starting…'), h('span', null, `${Math.round((j.progress || 0) * 100)}%`)),
          h('div', { class: 'progress thin' }, h('div', { style: { width: `${(j.progress || 0) * 100}%` } }))));
    } else {
      const sel = this.selected();
      const reason = this.blockReason(sel);
      A.append(h('button', { class: 'btn primary big', disabled: !!reason, title: reason, onclick: () => this.process() },
        icon('person-running'), sel.length ? `Process ${plural(sel.length, 'take')}` : 'Process selected'));
      const last = lastJob('mocap_process');
      if (reason) A.append(h('span', { class: 'note' }, reason));
      else if (last && last.status === 'error') A.append(h('span', { class: 'small err ellipsis', title: last.error || '' }, `last run failed: ${last.error}`));
      else if (last && last.status === 'cancelled') A.append(h('span', { class: 'small warn' }, 'last run cancelled'));
      else if (last && last.status === 'done') A.append(h('span', { class: 'small ok ellipsis' }, last.message || 'last run done'));
    }
    A.append(h('span', { class: 'spacer' }),
      h('button', { class: 'btn sm', onclick: () => reveal('output') }, icon('folder-open'), 'Open output folder'));
  },
};
