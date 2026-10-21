// Mocap step: run FreeMoCap 1.8.2 on the exported takes, each with its calibration profile
// (the profiles live on the Calibrate step), plus the helpers the Calibrate screen shares.
// app.js imports this module, so app.js bindings are only used inside functions (circular import).
import {
  h, icon, tip, S, GET, POST, PUT, sUrl, toast, confirmModal, activeJob, lastJob, cancelJob,
  go, fmtDate, fmtDur, fmtBytes, prefs, putUi, render, pollSoon,
} from './app.js';
import { showSetup } from './setup.js';

export const COLORS = { green: '#97d47d', orange: '#E89768', red: '#E56767', blue: '#8880F0', grey: '#4f4f4f' };
const STAGES = {
  downloading: 'downloading', detecting_board: 'detecting the board', tracking_2d: '2D tracking',
  triangulating: 'triangulating', skeleton: 'skeleton', saving: 'saving', blender: 'Blender export',
};
const SHOWN = 12;   // takes listed before "+ N more takes"

// FreeMoCap 1.8.2's processing settings, by its own parameter names (the server keeps only what
// differs from the defaults). processes / blender / blender_exe are this app's options.
const CX = [[0, '0 · fastest'], [1, '1 · balanced'], [2, '2 · accurate']];
const CX_NAME = { 0: 'fastest', 1: 'balanced', 2: 'most accurate' };
const FIELDS = {
  run_image_tracking: { label: 'Run 2D image tracking', kind: 'check',
    tip: 'Find the body in every frame of every video with MediaPipe. Off: each take reuses the 2D data of its last run, so only triangulation and the later steps run again.' },
  processes: { label: 'Max processes', kind: 'num', opt: true, min: 1, step: 1,
    tip: 'How many videos are tracked in parallel. Each process takes a CPU core and memory; more than one per camera doesn\'t help.' },
  mediapipe_model_complexity: { label: 'Model complexity', kind: 'select', options: CX,
    tip: 'Which MediaPipe pose model runs: 0 is the light, fast one, 2 the heavy, most accurate one (FreeMoCap\'s default).' },
  min_detection_confidence: { label: 'Min detection confidence', kind: 'num', min: 0, max: 1, step: 0.05,
    tip: 'How sure MediaPipe must be before it reports a person in a frame. Higher drops doubtful detections; lower keeps more frames, and more mistakes.' },
  min_tracking_confidence: { label: 'Min tracking confidence', kind: 'num', min: 0, max: 1, step: 0.05,
    tip: 'How sure MediaPipe must be to keep following the person from the previous frame; below it, it searches the whole frame again.' },
  static_image_mode: { label: 'Static image mode', kind: 'check',
    tip: 'Treat every frame as an unrelated photo: detect from scratch each time instead of tracking. Slower; can help with very fast motion.' },
  use_yolo_crop_method: { label: 'Use YOLO crop', kind: 'check',
    tip: 'Find the person with a YOLO detector first and run MediaPipe on that crop only. Helps when the person is small in the frame; slower, and MediaPipe then always runs in static image mode.' },
  yolo_model_size: { label: 'YOLO model size', kind: 'select',
    options: [['nano', 'nano'], ['small', 'small'], ['medium', 'medium'], ['large', 'large'], ['extra_large', 'extra large'], ['high_res', 'high res']],
    tip: 'Size of the YOLO person detector: bigger finds people more reliably and runs slower.' },
  buffer_size_method: { label: 'Buffer bounding box', kind: 'select',
    options: [['buffer_by_box_size', 'by box size'], ['buffer_by_image_size', 'by image size']],
    tip: 'What the margin around the YOLO box is a percentage of: the box itself, or the whole image.' },
  bounding_box_buffer_percentage: { label: 'Buffer percentage', kind: 'num', min: 0, max: 100, step: 1,
    tip: 'Margin added around the detected person before cropping, so hands and feet near the box edge aren\'t cut off.' },
  run_3d_triangulation: { label: 'Run 3D triangulation', kind: 'check',
    tip: 'Combine the cameras\' 2D points into 3D with the calibration. Off: each take reuses the 3D data of its last run, so only post-processing runs again.' },
  minimum_cameras_for_triangulation: { label: 'Min cameras', kind: 'num', min: 2, step: 1,
    tip: 'A point is only placed in 3D when at least this many cameras see it; otherwise it\'s left empty for that frame and post-processing fills the gap by interpolation. FreeMoCap caps it at the take\'s camera count.' },
  use_triangulate_outlier_rejection: { label: 'Use outlier rejection', kind: 'check',
    tip: 'When a point\'s reprojection error is above the target, also triangulate it without one or more cameras and blend the results, favouring the combinations that agree best. Slower.' },
  maximum_cameras_to_drop: { label: 'Max cameras to drop', kind: 'num', min: 0, step: 1,
    tip: 'How many cameras outlier rejection may leave out of one point. It never goes below the minimum cameras.' },
  target_reprojection_error: { label: 'Target reprojection error', kind: 'num', min: 0.0001, step: 0.01,
    tip: 'Error at which a point counts as good enough and no camera is dropped, in normalized image units (not pixels): 0.01 is about 1% of the image.' },
  run_butterworth_filter: { label: 'Run Butterworth filter', kind: 'check',
    tip: 'Smooth the 3D trajectories with a low-pass filter to remove jitter. Gaps are filled by interpolation either way.' },
  framerate: { label: 'Framerate', kind: 'num', min: 1, step: 0.01,
    tip: 'The frame rate the filter assumes. It must match the footage: the real cutoff scales with any mismatch.' },
  cutoff_frequency: { label: 'Cutoff frequency (Hz)', kind: 'num', min: 0.1, step: 0.5,
    tip: 'Motion faster than this is smoothed away. Lower is smoother but blunts quick moves; it must stay below half the framerate.' },
  order: { label: 'Order', kind: 'num', min: 1, max: 8, step: 1,
    tip: 'How sharply the filter cuts above the cutoff frequency: higher is steeper.' },
  blender: { label: 'Export .blend after processing', kind: 'check', opt: true,
    tip: 'After processing, FreeMoCap runs Blender in the background and saves the take\'s skeleton as a .blend file in its folder. Needs Blender; adds time per take.' },
  blender_exe: { label: 'Blender path', kind: 'text', opt: true,
    tip: 'The blender.exe to use for the export. Empty: FreeMoCap looks for an installed Blender itself.' },
};
const WINS = [
  { id: 'track', title: '2D tracking', master: 'run_image_tracking', top: ['processes'], sections: [
    { title: 'MediaPipe', fields: ['mediapipe_model_complexity', 'min_detection_confidence', 'min_tracking_confidence', 'static_image_mode'] },
    { title: 'YOLO crop', gate: 'use_yolo_crop_method', fields: ['yolo_model_size', 'buffer_size_method', 'bounding_box_buffer_percentage'] }] },
  { id: 'tri', title: '3D triangulation', master: 'run_3d_triangulation', top: ['minimum_cameras_for_triangulation'], sections: [
    { title: 'Outlier rejection', gate: 'use_triangulate_outlier_rejection', fields: ['maximum_cameras_to_drop', 'target_reprojection_error'] }] },
  { id: 'post', title: 'Post-processing', master: 'run_butterworth_filter', top: [], sections: [
    { title: 'Butterworth filter', fields: ['framerate', 'cutoff_frequency', 'order'] }] },
  { id: 'out', title: 'Output', master: 'blender', top: ['blender_exe'], sections: [] },
];
const OFF = { track: 'off · reuses the last 2D data', tri: 'off · reuses the last 3D data', post: 'off · no filtering' };
const winKeys = (w) => [w.master, ...w.top, ...w.sections.flatMap((s) => (s.gate ? [s.gate] : []).concat(s.fields))];
export const camName = (c) => String(c).replace(/_synced$/, '');
export const square = (color) => h('span', { class: 'mc-sq', style: { background: color } });
const plural = (n, w) => `${n} ${w}${n === 1 ? '' : 's'}`;
const p2 = (n) => String(n).padStart(2, '0');
/** 2026-10-21 01:28[:44] */
export function fmtStamp(t, seconds = false) {
  if (!t) return '—';
  const d = new Date(t * 1000);
  return `${d.getFullYear()}-${p2(d.getMonth() + 1)}-${p2(d.getDate())} ${p2(d.getHours())}:${p2(d.getMinutes())}`
    + (seconds ? `:${p2(d.getSeconds())}` : '');
}
const shortDate = (t) => fmtStamp(t).slice(5);
export const boardText = (p) => `${p.board}${p.square_mm ? ` · ${p.square_mm} mm` : ''} · ${p.groundplane ? 'on the floor' : 'not on the floor'}`;
export const errText = (p) => (p.reprojection_error_px == null ? '—' : `${Number(p.reprojection_error_px).toFixed(2)} px`);
export const ERR_TIP = 'Reprojection error of the calibration: how far, in pixels, the detected board corners are from where the calibrated cameras put them. Lower is better; around 1–2 px or less is good.';
/** How a profile looks in a status column / a select. */
export function profileStatus(p, job) {
  if (p.status === 'running') {
    const f = job ? Math.round((job.progress || 0) * 100) : null;
    return { text: f == null ? 'calibrating' : `calibrating · ${f}%`, color: COLORS.orange };
  }
  if (p.calibrated) return { text: 'calibrated', color: COLORS.green };
  if (p.status === 'failed') return { text: 'failed', color: COLORS.red };
  return { text: 'not run', color: COLORS.blue };
}

export function notReadyCard(fm) {
  const broken = fm.state === 'broken';
  return h('div', { class: 'card mc-setup' },
    h('div', { class: 'row', style: { gap: '10px' } },
      icon(broken ? 'triangle-exclamation' : 'circle-info', broken ? 'err' : 'warn'),
      h('span', { class: 'card-title' }, broken ? 'FreeMoCap 1.8.2 needs a repair' : 'FreeMoCap 1.8.2 isn\'t installed')),
    h('p', { class: 'note', style: { margin: 0, lineHeight: '1.6' } },
      broken
        ? `It's installed but its check fails${fm.detail ? `: ${fm.detail}` : ''}. Repair it from the Setup panel.`
        : 'FreeMoCap runs in its own environment (Python 3.12, ~3 GB download), separate from this app. '
          + 'Install it from the Setup panel on the Home screen, then come back here.'),
    h('div', { class: 'row' }, h('button', { class: 'btn primary', onclick: () => showSetup() }, icon('screwdriver-wrench'), 'Open Setup')));
}

/** POST a mocap job; when the server refuses because of leftover videos (409), list them,
 *  and on confirm send it again with delete_leftovers. */
export async function postMocapJob(path, body, okLabel, takeName = (id) => id) {
  try {
    const job = await POST(sUrl(path), { ...body, delete_leftovers: !!body.delete_leftovers });
    S.jobs = S.jobs.filter((j) => j.id !== job.id).concat([job]);
    render();
    pollSoon();
    return job;
  } catch (e) {
    const lo = e.status === 409 && e.detail && typeof e.detail === 'object' ? e.detail.leftovers : null;
    if (lo && !body.delete_leftovers) {
      const msg = h('span', { class: 'col', style: { gap: '8px' } },
        h('span', null, 'These videos are in synchronized_videos/ but weren\'t part of the take\'s last export, so FreeMoCap would process them as extra cameras:'),
        Object.entries(lo).map(([id, files]) => h('span', null, h('b', null, takeName(id)), ': ', files.join(', '))));
      if (await confirmModal({ title: 'Delete leftover videos?', body: msg, ok: okLabel })) {
        return postMocapJob(path, { ...body, delete_leftovers: true }, okLabel, takeName);
      }
      return null;
    }
    toast(e.detail?.message || e.message, 'error');
    return null;
  }
}

export const MocapScreen = {
  data: null,      // GET /mocap
  name: null,      // session the data belongs to
  sel: new Set(),  // take ids ticked for processing
  opts: null,
  live: null,      // job.live of the running mocap_process job
  more: false,     // all takes listed

  mount(root) {
    this.root = h('main', { class: 'page mc-page' });
    // a background refresh that arrived while typing is applied once the field loses focus
    this.root.addEventListener('focusout', () => setTimeout(() => { if (this.deferred && !this.typing()) this.render(); }, 60));
    root.append(this.root);
    this.render();
    this.load();
  },
  unmount() {
    this.saveOpts();
    this.stopLive();
    this.closeViewer();
  },
  sessionChanged() {
    this.saveOpts();
    this.stopLive();
    this.closeViewer();
    Object.assign(this, {
      data: null, name: null, sel: new Set(), selFor: null, opts: null, live: null, error: null, more: false,
      openTake: null, details: {}, detailReq: {},     // detailReq too, or the next session's takes never fetch theirs
    });
  },
  update() { this.load(); },
  onJobs() { this.tick(); },

  typing() {
    const a = document.activeElement;
    return !!(a && this.root && this.root.contains(a) && ['INPUT', 'TEXTAREA'].includes(a.tagName));
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
      this.optsDirty = false;
      this.more = false;
      this.selFor = null;
    }
    if (this.selFor !== name && S.data) {      // the ticks are kept on the server, per session
      this.sel = new Set(S.data.ui?.mocap_selected || []);
      this.selFor = name;
    }
    this.data = d;
    if (!this.optsDirty) this.opts = { ...(d.options || {}), params: { ...(d.options?.params || {}) } };
  },
  take(id) { return (this.data?.takes || []).find((t) => t.take_id === id); },
  profile(id) { return (this.data?.profiles || []).find((p) => p.id === id); },
  activeProfile() { const p = this.profile(this.data?.active_profile); return p && p.calibrated ? p : null; },
  selectable(t) { return !!t && !t.is_calibration && t.layout_ok && !(t.missing || []).length; },
  rows() { return (this.data?.takes || []).filter((t) => !t.is_calibration); },
  selected() { return this.rows().filter((t) => this.sel.has(t.take_id) && this.selectable(t)); },
  setSel(ids, on) {
    for (const id of ids) { if (on) this.sel.add(id); else this.sel.delete(id); }
    putUi({ mocap_selected: [...this.sel] });
    this.render();
  },

  // ---------------------------------------------------------------- actions
  async process() {
    const ids = this.selected().map((t) => t.take_id);
    if (!ids.length) return;
    await this.saveOpts();
    await postMocapJob('/mocap/process', { take_ids: ids }, 'Delete and process', (id) => this.take(id)?.name || id);
  },
  async setTakeProfile(t, pid) {
    const name = this.name;
    try {
      // picking the active profile means "follow the active one" (also when it changes later)
      const d = await PUT(sUrl(`/mocap/takes/${encodeURIComponent(t.take_id)}/profile`), { profile: pid === this.data.active_profile ? null : pid });
      if (this.name === name) { this.adopt(d, name); this.render(); }
    } catch (e) { toast(e.message, 'error'); this.render(); }
  },
  optsChanged(rerender = false) {
    this.optsDirty = true;
    clearTimeout(this.optsTimer);
    this.optsTimer = setTimeout(() => this.saveOpts(), 500);
    if (rerender) this.render();
  },
  /** Save the settings now; ``beacon`` sends them without waiting (the page is going away). */
  async saveOpts(beacon = false) {
    clearTimeout(this.optsTimer);
    if (!this.optsDirty || !this.name) return;
    this.optsDirty = false;
    const name = this.name;
    const url = `/api/sessions/${encodeURIComponent(name)}/mocap/options`;
    if (beacon) {
      fetch(url, { method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(this.opts) }).catch(() => {});
      return;
    }
    try {
      const d = await PUT(url, this.opts);
      if (this.data && this.name === name) {
        Object.assign(this.data, { options: d.options, params: d.params });
        this.load();                          // takes go stale (or fresh again) with the settings
      }
    } catch (e) { toast(`Couldn't save mocap options: ${e.message}`, 'error'); }
  },
  flush() { this.saveOpts(true); },
  async openFolder(dir) {
    try { await POST(sUrl('/reveal'), { what: 'output', sub: dir }); } catch (e) { toast(e.message, 'error'); }
  },

  // ---------------------------------------------------------------- live progress
  tick() {
    if (!this.root || !this.root.isConnected || !this.data) return;
    const job = activeJob('mocap_process');
    if (job) this.startLive(job.id); else this.stopLive();
    this.renderAction();
    this.renderBanner();
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
        this.renderBanner();
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
    const keep = [r.scrollTop, this.tableBox?.scrollLeft || 0];
    r.innerHTML = '';
    this.tableBox = this.action = this.banner = null;
    this.statusCells = new Map();
    const head = h('div', { class: 'page-head' },
      h('h1', null, 'Mocap'),
      h('p', null, 'Run FreeMoCap on the takes: 2D tracking, triangulation and the skeleton. Results saved to each take\'s folder.'));
    const d = this.data;
    if (!S.name) return;
    if (!d) {
      r.append(head, h('div', { class: 'empty' }, this.error ? `Couldn't load the mocap state: ${this.error}` : [icon('spinner', 'fa-spin'), ' loading…']));
      return;
    }
    if (!d.freemocap?.ready) {
      r.append(head, notReadyCard(d.freemocap || {}));
      return;
    }
    if (!d.takes.length) {
      r.append(head, h('div', { class: 'empty' }, 'No exported takes yet — export the calibration take and some takes first. ',
        h('button', { class: 'btn sm', onclick: () => go('export') }, 'Open Export', icon('arrow-right'))));
      return;
    }
    this.action = h('div', { class: 'mc-actions' });
    r.append(h('div', { class: 'mc-cols' },
      h('div', { class: 'mc-left' }, head, this.activeCard(), this.settingsCard(), this.action),
      this.takesPanel()));
    this.tick();
    r.scrollTop = keep[0];
    if (this.tableBox) this.tableBox.scrollLeft = keep[1];
  },

  activeCard() {
    const p = this.activeProfile();
    const card = h('section', { class: 'card mc-card', 'aria-label': 'Active calibration' }, h('h2', { class: 'card-title' }, 'Active calibration'));
    if (!p) {
      card.append(h('span', { class: 'note mc-note' }, (this.data.profiles || []).length
        ? 'No profile is calibrated yet — run one on the Calibrate step.'
        : 'No calibration profile yet — create one on the Calibrate step and run it.'),
      h('div', { class: 'row' }, h('button', { class: 'btn sm', onclick: () => go('calibrate') }, icon('crosshairs'), 'Open Calibrate')));
      return card;
    }
    card.append(h('dl', { class: 'mc-dl' },
      h('dt', null, tip('profile', 'Takes are processed with this profile unless you pick another one for a take in the table. Each take\'s cameras are compared with its profile\'s.')),
      h('dd', { class: 'b' }, p.name),
      h('dt', null, tip('error', ERR_TIP)), h('dd', null, `${errText(p)} reprojection`),
      h('dt', null, 'board'), h('dd', null, boardText(p)),
      h('dt', null, 'cameras'), h('dd', { title: (p.cameras || []).map(camName).join('\n') }, String((p.cameras || []).length)),
      h('dt', null, 'calibrated'), h('dd', null, fmtStamp(p.created_at, true))));
    if (p.take_reexported) card.append(h('span', { class: 'small warn mc-note' }, 'Its calibration take was re-exported since — duplicate the profile on the Calibrate step and run the copy.'));
    if (p.settings_changed) card.append(h('span', { class: 'small warn mc-note' }, 'Its board settings changed since it was calibrated — duplicate it on the Calibrate step and run the copy.'));
    return card;
  },

  // ---------------------------------------------------------------- settings (FreeMoCap's processing options)
  defaultOf(k) {
    const d = this.data;
    if (k === 'processes') return d.default_processes;
    if (k === 'blender') return false;
    if (k === 'blender_exe') return '';
    return d.param_defaults?.[k];
  },
  valueOf(k) {
    const o = this.opts;
    if (k === 'processes') return o.processes ?? this.data.default_processes;
    if (k === 'blender') return !!o.blender;
    if (k === 'blender_exe') return o.blender_exe || '';
    const p = o.params || {};
    return k in p ? p[k] : this.defaultOf(k);
  },
  isChanged(k) {
    if (k === 'blender_exe') return false;                  // a path, not a setting with a default
    return this.valueOf(k) !== this.defaultOf(k);
  },
  setValue(k, v) {
    const o = this.opts;
    const def = this.defaultOf(k);
    if (k === 'processes') o.processes = v == null || v === def ? null : v;
    else if (k === 'blender') o.blender = !!v;
    else if (k === 'blender_exe') o.blender_exe = String(v || '').trim() || null;
    else {
      const p = { ...(o.params || {}) };
      if (v == null || v === def) delete p[k]; else p[k] = v;
      o.params = p;
    }
    this.optsChanged();
    this.renderSettings();
  },
  resetAll() {
    Object.assign(this.opts, { params: {}, processes: null, blender: false });
    this.optsChanged();
    this.renderSettings();
  },
  /** What FreeMoCap would reject halfway through a run (the server checks it too). */
  paramProblem() {
    if (!this.data?.param_defaults) return '';
    const fps = Number(this.valueOf('framerate'));
    const cut = Number(this.valueOf('cutoff_frequency'));
    if (this.valueOf('run_butterworth_filter') && cut >= fps / 2) return `The cutoff must be below half the framerate (${+(fps / 2).toFixed(3)} Hz).`;
    return '';
  },
  fieldTip(k) {
    const d = this.data;
    const f = FIELDS[k];
    if (k === 'processes') return `${f.tip} This PC has ${d.cpu_count} CPUs. By default each run uses one per camera of its takes (${d.default_processes} here).`;
    if (k === 'framerate') {
      const fps = d.param_defaults.framerate;
      const r = fps / 30;
      return `${f.tip} The default is the session footage's ${fps} fps; FreeMoCap's own default is 30`
        + (Math.abs(r - 1) > 0.005 ? `, which on this footage would ${Math.abs(r - 2) < 0.01 ? 'double' : `scale by ×${r.toFixed(2)}`} the real cutoff.` : '.');
    }
    return f.tip;
  },
  showDefault(k) {
    const f = FIELDS[k];
    const v = this.defaultOf(k);
    if (f.kind === 'check') return v ? 'on' : 'off';
    if (f.kind === 'select') return (f.options.find(([x]) => x === v) || [v, String(v)])[1];
    return String(v);
  },
  fieldRow(k) {
    const f = FIELDS[k];
    const id = `fmc-${k}`;
    const v = this.valueOf(k);
    const reset = this.isChanged(k)
      ? h('button', {
        type: 'button', class: 'mc-freset', title: `Reset to ${this.showDefault(k)}`, 'aria-label': `Reset ${f.label} to ${this.showDefault(k)}`,
        onclick: (e) => { e.preventDefault(); e.stopPropagation(); this.setValue(k, null); },
      }, icon('rotate-left'))
      : null;
    const label = tip(f.label, this.fieldTip(k));
    if (f.kind === 'check') {
      return h('div', { class: 'toggle', onclick: () => this.setValue(k, !v) },
        h('span', { class: `check ${v ? 'on' : ''}`, id, role: 'checkbox', 'aria-checked': String(!!v) }, icon('check')), label,
        reset ? h('span', { class: 'spacer' }) : null, reset);
    }
    let ctl;
    if (f.kind === 'select') {
      ctl = h('select', { id, class: 'input', onchange: (e) => this.setValue(k, typeof f.options[0][0] === 'number' ? Number(e.target.value) : e.target.value) },
        f.options.map(([x, text]) => h('option', { value: String(x), selected: x === v }, text)));
    } else if (f.kind === 'text') {
      ctl = h('input', {
        id, class: 'input', type: 'text', value: v, placeholder: 'empty: FreeMoCap finds it', spellcheck: 'false', autocomplete: 'off',
        onchange: (e) => this.setValue(k, e.target.value),
      });
    } else {
      const max = k === 'processes' ? this.data.cpu_count
        : k === 'minimum_cameras_for_triangulation' ? Math.max(2, ...this.rows().map((t) => t.cameras.length))
          : k === 'maximum_cameras_to_drop' ? Math.max(0, ...this.rows().map((t) => t.cameras.length - 2)) : f.max;
      ctl = h('input', {
        id, class: 'input', type: 'number', value: String(v), min: String(f.min), max: max == null ? null : String(max), step: String(f.step),
        onchange: (e) => {
          const raw = e.target.value.trim();
          if (raw === '') { this.setValue(k, null); return; }
          let n = Number(raw);
          if (!Number.isFinite(n)) { e.target.value = String(v); return; }
          n = Math.max(f.min, max == null ? n : Math.min(max, n));
          this.setValue(k, f.step >= 1 ? Math.round(n) : +n.toFixed(4));
        },
      });
    }
    return h('label', { class: 'field' }, h('span', { class: 'mc-flabel' }, label, reset), ctl);
  },
  summary(w) {
    const v = (k) => this.valueOf(k);
    if (w.id === 'out') return v('blender') ? 'exports a .blend per take' : 'no .blend export';
    if (!v(w.master)) return OFF[w.id];
    if (w.id === 'track') {
      const n = v('processes');
      const cx = CX_NAME[v('mediapipe_model_complexity')] ?? v('mediapipe_model_complexity');
      return `MediaPipe · ${cx} · ${n} process${n === 1 ? '' : 'es'}${v('use_yolo_crop_method') ? ' · YOLO crop' : ''}`;
    }
    if (w.id === 'tri') return `min ${v('minimum_cameras_for_triangulation')} cameras · ${v('use_triangulate_outlier_rejection') ? 'outlier rejection' : 'no outlier rejection'}`;
    return `Butterworth ${v('cutoff_frequency')} Hz · order ${v('order')} · ${v('framerate')} fps`;
  },
  settingsCard() {
    this.settingsBox = h('section', { class: 'mc-settings', 'aria-label': 'FreeMoCap settings' });
    this.fillSettings();
    return this.settingsBox;
  },
  renderSettings() {
    const box = this.settingsBox;
    if (!box || !box.isConnected) return;
    const focused = document.activeElement?.id;
    this.fillSettings();
    if (focused && !document.activeElement?.id) document.getElementById(focused)?.focus();
    this.renderAction();
  },
  fillSettings() {
    const box = this.settingsBox;
    box.innerHTML = '';
    this.win = this.win || prefs.get('mocapWin', { track: true });
    box.append(h('h2', { class: 'card-title' }, 'Settings'));
    for (const w of WINS) {
      const open = !!this.win[w.id];
      const n = winKeys(w).filter((k) => this.isChanged(k)).length;
      const win = h('div', { class: 'mc-win' },
        h('button', {
          type: 'button', class: 'mc-winhead', 'aria-expanded': String(open),
          onclick: () => { this.win = { ...this.win, [w.id]: !open }; prefs.set('mocapWin', this.win); this.renderSettings(); },
        },
        h('span', { class: 'mc-wintxt' }, h('span', { class: 'mc-wintitle' }, w.title), h('span', { class: 'mc-winsum' }, this.summary(w))),
        n ? h('span', { class: 'mc-winchg' }, `${n} changed`) : null,
        h('span', { class: 'mc-winchev', 'aria-hidden': 'true' }, icon(open ? 'chevron-up' : 'chevron-down'))));
      if (open) {
        const body = h('div', { class: 'mc-winbody' }, this.fieldRow(w.master));
        if (this.valueOf(w.master)) {
          w.top.forEach((k) => body.append(this.fieldRow(k)));
          for (const s of w.sections) {
            body.append(h('span', { class: 'mc-winsec' }, s.title));
            if (s.gate) body.append(this.fieldRow(s.gate));
            if (!s.gate || this.valueOf(s.gate)) s.fields.forEach((k) => body.append(this.fieldRow(k)));
          }
        }
        if (w.id === 'post' && this.paramProblem()) body.append(h('span', { class: 'small err mc-winwarn' }, this.paramProblem()));
        win.append(body);
      }
      box.append(win);
    }
    if (Object.keys(FIELDS).some((k) => this.isChanged(k))) {
      box.append(h('button', { type: 'button', class: 'btn mc-full', onclick: () => this.resetAll() }, icon('rotate-left'), 'Reset all to defaults'));
    }
  },

  takesPanel() {
    const rows = this.rows();
    const pickable = rows.filter((t) => this.selectable(t));
    const nSel = this.selected().length;
    const allOn = pickable.length > 0 && nSel === pickable.length;
    this.banner = h('div', { class: 'mc-banner' });
    const toolbar = h('div', { class: 'mc-toolbar' },
      h('button', { class: 'btn xs', disabled: !pickable.length, onclick: () => this.setSel(allOn ? rows.map((t) => t.take_id) : pickable.map((t) => t.take_id), !allOn) },
        allOn ? 'Deselect all' : 'Select all'),
      h('span', { class: 'note', 'aria-live': 'polite' }, h('b', { class: 'mc-strong' }, String(nSel)), ` out of ${rows.length} selected`));
    if (!rows.length) {
      return h('div', { class: 'mc-right' }, this.banner, h('div', { class: 'empty' }, 'Only calibration takes are exported — export some takes to process.'));
    }
    const shown = this.more ? rows : rows.slice(0, SHOWN);
    this.tableBox = h('div', { class: 'tablewrap mc-tablewrap' }, h('table', { class: 'ftable mc-table' },
      h('thead', null, h('tr', null,
        h('th', { class: 'ck' }, h('span', { class: 'mc-sr' }, 'Process')), h('th', null, 'take'),
        h('th', null, tip('cameras', 'How many of the take\'s cameras its calibration profile knows. FreeMoCap needs at least 2; a camera the profile doesn\'t know blocks processing.')),
        h('th', { class: 'num' }, 'length'),
        h('th', null, tip('selected calibration', 'The calibration profile the take is processed with. It follows the active profile unless you pick another one here; only calibrated profiles can be picked.')),
        h('th', null, 'status'), h('th', null, 'processed'),
        h('th', { class: 'ck' }, h('span', { class: 'mc-sr' }, 'Folder')))),
      h('tbody', null, shown.flatMap((t) => (this.openTake === t.take_id ? [this.takeRow(t), this.detailRow(t)] : [this.takeRow(t)])))));
    const more = rows.length > SHOWN
      ? h('button', { class: 'mc-more', 'aria-expanded': String(this.more), onclick: () => { this.more = !this.more; this.render(); } },
        this.more ? 'Show fewer' : `+ ${rows.length - SHOWN} more takes`, icon(this.more ? 'chevron-up' : 'chevron-down'))
      : null;
    return h('div', { class: 'mc-right' }, this.banner, toolbar, this.tableBox, more);
  },

  takeRow(t) {
    const ok = this.selectable(t);
    const on = ok && this.sel.has(t.take_id);
    const prof = this.profile(t.profile);
    const m = t.calib_match;
    let cams;
    if (!t.layout_ok) cams = h('span', { class: 'err' }, 'flat layout — export it again');
    else if (!prof || !m) cams = h('span', null, plural(t.cameras.length, 'camera'));
    else if ((m.unknown || []).length) cams = h('span', { class: 'err' }, `not in ${prof.name}: ${m.unknown.map(camName).join(', ')}`);
    else if (m.n < 2) cams = h('span', { class: 'err' }, `${m.n}/${m.of} — needs at least 2 cameras`);
    else if ((m.missing || []).length) cams = h('span', { class: 'warn' }, `${m.n}/${m.of} — no ${m.missing.map(camName).join(', ')}`);
    else cams = h('span', { class: 'ok' }, `${m.n}/${m.of}`);
    const tags = [];
    if ((t.leftovers || []).length) {
      tags.push(tip(`${t.leftovers.length} leftover`, `Videos in synchronized_videos/ that weren't part of the take's last export (${t.leftovers.join(', ')}). `
        + 'FreeMoCap would process them as extra cameras, so you\'re asked to delete them first.', { class: 'tag warn' }));
    }
    if ((t.missing || []).length) {
      tags.push(tip(`${t.missing.length} missing`, `Exported videos that aren't on disk any more (${t.missing.join(', ')}). Export the take again.`, { class: 'tag err' }));
    }
    const status = h('td', null);
    this.statusCells.set(t.take_id, status);
    this.fillStatus(status, t);
    const why = !t.layout_ok ? 'flat layout: FreeMoCap needs synchronized_videos/' : (t.missing || []).length ? 'videos are missing — export the take again' : '';
    return h('tr', { class: `${ok ? 'can' : ''} ${on ? '' : 'off'}`, title: why, onclick: () => { if (ok) this.setSel([t.take_id], !on); } },
      h('td', { class: 'ck' }, h('span', { class: `check ${on ? 'on' : ''} ${ok ? '' : 'dis'}`, role: 'checkbox', 'aria-checked': String(on), 'aria-label': 'Process this take' }, icon('check'))),
      h('td', { class: 'nm', title: t.dir }, h('button', {
        class: 'mc-tname', 'aria-expanded': String(this.openTake === t.take_id),
        onclick: (e) => { e.stopPropagation(); this.toggleTake(t); },
      }, icon(this.openTake === t.take_id ? 'chevron-down' : 'chevron-right', 'mc-chev'), t.name)),
      h('td', { class: 'cams', title: (t.camera_names || t.cameras).map(camName).join('\n') }, cams, tags.length ? ' ' : null, tags),
      h('td', { class: 'num' }, fmtDur(t.duration)),
      h('td', { class: 'mc-pick', onclick: (e) => e.stopPropagation() }, this.profileSelect(t)),
      status,
      h('td', { class: 'when', title: t.processed_at ? fmtDate(t.processed_at) : '' },
        t.processed_at ? `${shortDate(t.processed_at)}${t.seconds ? ` · ${fmtDur(t.seconds)}` : ''}` : '—'),
      h('td', { class: 'ck' }, h('button', {
        class: 'mini-btn', title: `Open output/${t.dir}/`, 'aria-label': 'Open take folder', onclick: (e) => { e.stopPropagation(); this.openFolder(t.dir); },
      }, icon('folder-open'))));
  },
  profileSelect(t) {
    const profs = this.data.profiles || [];
    const busy = (t.status === 'running') || !!activeJob('mocap_process');
    if (!profs.length) return h('select', { class: 'mc-select', disabled: true, 'aria-label': 'Calibration profile for this take' }, h('option', null, 'no calibration'));
    const cal = activeJob('mocap_calibrate');
    return h('select', {
      class: 'mc-select', disabled: busy, 'aria-label': 'Calibration profile for this take',
      title: t.profile_explicit ? 'picked for this take' : 'follows the active profile',
      onchange: (e) => this.setTakeProfile(t, e.target.value),
    },
    t.profile ? null : h('option', { value: '', selected: true, disabled: true }, 'none calibrated'),
    profs.map((p) => {
      const st = profileStatus(p, cal);
      const label = p.calibrated ? `${p.name} · ${errText(p)}${p.active ? ' (active)' : ''}` : `${p.name} · ${st.text}`;
      return h('option', { value: p.id, disabled: !p.calibrated, selected: p.id === t.profile }, label);
    }));
  },
  // ---------------------------------------------------------------- take detail (click a take's name)
  toggleTake(t) {
    if (this.openTake === t.take_id) {
      this.openTake = null;
      this.closeViewer();
    } else {
      this.openTake = t.take_id;
    }
    this.render();
  },
  async loadDetail(tid, key) {
    const name = this.name;
    try {
      const d = await GET(sUrl(`/mocap/takes/${encodeURIComponent(tid)}/detail`));
      if (this.name !== name) return;
      this.details = { ...(this.details || {}), [tid]: { ...d, key } };
      if (this.openTake === tid) { if (this.typing()) this.deferred = true; else this.render(); }
    } catch (e) { if (this.name === name) toast(e.message, 'error'); }
  },
  /** The 3D skeleton viewer (skeleton3d.js) of the open take: one element that survives re-renders. */
  mountViewer(tid, box) {
    if (this.viewer && this.viewer.tid === tid) { box.append(this.viewer.el); return; }
    this.closeViewer();
    const el = h('div', { class: 'mc-viewer' });
    this.viewer = { tid, el, view: null };
    box.append(el);
    import('./skeleton3d.js').then((m) => {
      if (this.viewer?.el !== el) return;
      this.viewer.view = new m.SkeletonView(el, { url: sUrl(`/mocap/takes/${encodeURIComponent(tid)}/skeleton`) });
    }).catch(() => {
      if (this.viewer?.el === el) el.append(h('div', { class: 'mc-tview-empty' }, h('span', null, 'The 3D viewer isn\'t available.')));
    });
  },
  closeViewer() {
    if (!this.viewer) return;
    try { this.viewer.view?.destroy(); } catch { /* already gone */ }
    this.viewer.el.remove();
    this.viewer = null;
  },
  async processOne(t) {
    await this.saveOpts();
    await postMocapJob('/mocap/process', { take_ids: [t.take_id] }, 'Delete and process', (id) => this.take(id)?.name || id);
  },
  async openBlend(t) {
    try { await POST(sUrl(`/mocap/takes/${encodeURIComponent(t.take_id)}/open_blend`)); } catch (e) { toast(e.message, 'error'); }
  },
  detailRow(t) {
    const done = t.status === 'done' || t.status === 'stale';
    const key = `${t.status}|${t.processed_at || ''}`;
    this.details = this.details || {};
    this.detailReq = this.detailReq || {};
    const det = this.details[t.take_id]?.key === key ? this.details[t.take_id] : null;
    if (!det && this.detailReq[t.take_id] !== key) {
      this.detailReq[t.take_id] = key;
      this.loadDetail(t.take_id, key);
    }
    // left: the skeleton (processed takes only)
    const view = h('div', { class: 'mc-tview' });
    if (done) this.mountViewer(t.take_id, view);
    else {
      if (this.viewer?.tid === t.take_id) this.closeViewer();
      view.append(h('div', { class: 'mc-tview-empty' }, icon('person'),
        h('span', null, t.status === 'running' ? 'Processing — the skeleton shows up here when it\'s done.' : 'No skeleton yet: process this take to see it here.')));
    }
    // right: details
    const q = det?.quality;
    const info = det?.info;
    const dl = h('dl', { class: 'mc-dl' });
    const row = (k, v, help = '') => dl.append(h('dt', null, help ? tip(k, help) : k), h('dd', null, v));
    if (done && q && !q.error) {
      if (q.reprojection_median_px != null) {
        const ce = info?.calibration?.reprojection_error_px;
        row('reprojection', `${q.reprojection_median_px} px median${ce != null ? ` (calibration ${Number(ce).toFixed(2)} px)` : ''}`,
          'Median distance, in pixels, between where each camera saw a body point and where its 3D position projects back into that camera, over all frames and cameras. Lower is better.');
      }
      if (q.body_tracked_pct != null) row('body tracked', `${q.body_tracked_pct}% of frames`, 'Frames in which all 33 body points got a 3D position before gap filling.');
      if (q.gaps_filled_frames != null) {
        row('gaps filled', q.gaps_filled_frames ? `${q.gaps_filled_frames} frames · longest ${q.longest_gap}` : 'none',
          'Frames where a body point had no 3D position and post-processing filled it by interpolation; the longest such run, in frames.');
      }
      if (q.upper_arm_mm) row('upper arm', `${Math.round(q.upper_arm_mm[0])} ± ${Math.round(q.upper_arm_mm[1])} mm`, 'Left shoulder to elbow over the take: median ± half the interquartile range. A steady length means steady tracking; the value also checks the square size.');
      if (q.thigh_mm) row('thigh', `${Math.round(q.thigh_mm[0])} ± ${Math.round(q.thigh_mm[1])} mm`, 'Left hip to knee over the take: median ± half the interquartile range.');
    } else if (done && q?.error) row('details', h('span', { class: 'warn' }, q.error));
    const usedProf = info?.calibration?.profile_name || this.profile(t.profile)?.name;
    if (usedProf) row('calibration', usedProf + (done && info?.calibration?.profile_name && info.calibration.profile !== t.profile ? ' (another profile is selected now)' : ''));
    if (done && info?.params) row('settings', this.paramsText(info.params), 'FreeMoCap settings this take was processed with, where they differ from the current defaults.');
    row('frames', `${t.frames ?? '—'} · ${fmtDur(t.duration)}`);
    const camChips = done && q?.camera_detected
      ? q.camera_detected.map((c) => h('span', { class: 'tag' }, `${camName(c.camera)} · ${c.pct}%`))
      : t.cameras.map((c) => h('span', { class: 'tag' }, camName(c)));
    row('cameras', h('div', { class: 'mc-chips' }, camChips), done && q?.camera_detected ? 'Per camera: the share of frames in which MediaPipe found the body.' : '');
    row('output directory', h('span', null, det?.out_dir || `output/${t.dir}/`,
      det?.out_size ? h('span', { class: 'note' }, ` · ${fmtBytes(det.out_size)}`) : null));
    if (done) {
      row('processed', fmtStamp(t.processed_at, true));
      if (t.seconds) row('took', fmtDur(t.seconds));
      if (info?.freemocap) row('FreeMoCap', info.freemocap);
    } else if (t.status === 'failed') {
      row('failed', fmtStamp(t.processed_at, true));
      if (t.error) row('error', h('span', { class: 'err mc-errline', title: t.error }, t.error));
    }
    if (!det && (done || t.status === 'failed')) dl.append(h('dt', null, ''), h('dd', { class: 'note' }, [icon('spinner', 'fa-spin'), ' reading FreeMoCap\'s output…']));
    // actions: at the end of the details, like a profile's on the Calibrate step
    const busy = !!activeJob('mocap_process') || !!activeJob('mocap_calibrate');
    const why = busy ? 'wait for the running job' : this.blockReason([t]);
    const actions = h('div', { class: 'mc-tactions' },
      done && det?.blend ? h('button', { class: 'btn sm', onclick: () => this.openBlend(t) }, icon('cube'), 'Open in Blender') : null,
      h('button', { class: 'btn sm', disabled: !!why || !this.selectable(t), title: why, onclick: () => this.processOne(t) },
        icon(done ? 'rotate-right' : 'person-running'), done ? 'Process again' : 'Process this take'),
      h('button', { class: 'btn sm', onclick: () => this.openFolder(t.dir) }, icon('folder-open'), 'Open folder'));
    const details = h('section', { class: 'mc-tinfo' }, h('h3', null, 'Details'), dl, actions);
    return h('tr', { class: 'mc-tdetail' }, h('td', { colspan: '8' },
      h('div', { class: 'mc-tpanel' }, view, details)));
  },
  /** The settings a run used that differ from today's defaults, in the Settings card's words. */
  paramsText(used) {
    const diff = Object.keys(FIELDS).filter((k) => k in used && !FIELDS[k].opt && used[k] !== this.defaultOf(k)
      && !(k === 'framerate' && Math.abs(used[k] - this.defaultOf(k)) <= 0.005 * this.defaultOf(k)));
    if (!diff.length) return 'defaults';
    return diff.map((k) => {
      const f = FIELDS[k];
      const v = used[k];
      const shown = f.kind === 'check' ? (v ? 'on' : 'off') : f.kind === 'select' ? ((f.options.find(([x]) => x === v) || [v, String(v)])[1]) : String(v);
      return `${f.label.replace(/ \(.*\)$/, '').toLowerCase()} ${shown}`;
    }).join(' · ');
  },

  fillStatus(td, t) {
    td.innerHTML = '';
    const lv = this.live?.takes?.[t.take_id];
    const st = (color, text, extra = null, title = '') => h('div', { class: 'mc-status', title },
      h('span', { class: 'mc-st' }, square(color), text), extra);
    const SUBSET = 'Processed with only the cameras this take has: FreeMoCap used the matching part of its calibration.';
    let el;
    if (lv && lv.status === 'running') {
      const f = Math.max(0, Math.min(1, lv.fraction || 0));
      el = st(COLORS.orange, `${STAGES[lv.stage] || lv.stage || 'starting'} · ${Math.round(f * 100)}%`,
        h('div', { class: 'mini-bar' }, h('div', { style: { width: `${f * 100}%` } })));
    } else if (lv && lv.status === 'queued') el = st(COLORS.grey, 'queued');
    else if (lv && lv.status === 'failed') el = st(COLORS.red, 'failed', h('span', { class: 'note ellipsis mc-errnote' }, lv.error || ''), lv.error || '');
    else if (lv && lv.status === 'done') el = st(COLORS.green, 'done');
    else if (t.status === 'done') el = st(COLORS.green, t.subset ? h('span', null, 'done · ', tip('subset', SUBSET)) : 'done');
    else if (t.status === 'stale') {
      el = st(COLORS.orange, tip('stale', `Processed, but something changed since${t.stale_reason ? ` (${t.stale_reason})` : ''}: its results are out of date. Process it again to update them.`),
        h('span', { class: 'note' }, t.stale_reason || 'reprocess it'));
    }
    else if (t.status === 'failed') el = st(COLORS.red, 'failed', h('span', { class: 'note ellipsis mc-errnote' }, t.error || ''), t.error || '');
    else if (t.status === 'running') el = st(COLORS.orange, 'running');
    else el = h('div', { class: 'mc-status' }, h('span', { class: 'mc-st muted' }, square(COLORS.grey), 'not processed'));
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
    if (!sel.length) return 'Tick the takes to process';
    if (activeJob('mocap_calibrate')) return 'A calibration is running';
    const nocal = sel.find((t) => !this.profile(t.profile)?.calibrated);
    if (nocal) return `${nocal.name}: no calibrated profile — run one on the Calibrate step`;
    const bad = sel.find((t) => (t.calib_match?.unknown || []).length);
    if (bad) return `${bad.name}: a camera isn't in its calibration`;
    const few = sel.find((t) => t.calib_match && t.calib_match.n < 2);
    if (few) return `${few.name}: needs at least 2 calibrated cameras`;
    const pp = this.paramProblem();
    if (pp) return pp;
    // with a stage off FreeMoCap reuses the take's earlier results (the server checks the files)
    const fresh = sel.find((t) => !['done', 'stale'].includes(t.status));
    if (fresh && !this.valueOf('run_image_tracking')) return `${fresh.name}: 2D tracking is off, but it has no earlier 2D data`;
    if (fresh && !this.valueOf('run_3d_triangulation')) return `${fresh.name}: 3D triangulation is off, but it has no earlier 3D data`;
    return '';
  },
  renderAction() {
    const A = this.action;
    if (!A || !A.isConnected) return;
    A.innerHTML = '';
    const job = activeJob('mocap_process');
    if (job) {
      A.append(h('button', { class: 'btn primary big mc-full', onclick: () => cancelJob(job.id) }, icon('xmark'), 'Cancel processing'));
      return;
    }
    const sel = this.selected();
    const reason = this.blockReason(sel);
    A.append(h('button', { class: 'btn primary big mc-full', disabled: !!reason, title: reason, onclick: () => this.process() },
      icon('person-running'), sel.length ? `Process ${sel.length} selected` : 'Process selected'));
    if (reason && sel.length) A.append(h('span', { class: 'small warn' }, reason));
    const last = lastJob('mocap_process');
    if (last && last.status === 'error') A.append(h('span', { class: 'small err mc-errline', title: last.error || '' }, `Last run failed: ${last.error}`));
    else if (last && last.status === 'cancelled') A.append(h('span', { class: 'small warn' }, 'Last run cancelled.'));
  },
  renderBanner() {
    const B = this.banner;
    if (!B || !B.isConnected) return;
    B.innerHTML = '';
    const job = activeJob('mocap_process');
    if (job) {
      const j = this.liveJob && this.liveJob.id === job.id ? this.liveJob : job;
      const f = Math.max(0, Math.min(1, j.progress || 0));
      B.append(h('div', { class: 'mc-banner-row' }, icon('person-running', 'mc-banner-ic run'),
        h('span', { class: 'mc-banner-txt' }, `Processing · ${Math.round(f * 100)}%`, h('span', { class: 'mc-sub' }, ` — ${j.message || 'starting FreeMoCap…'}`))),
      h('div', { class: 'mc-bar' }, h('div', { class: 'run', style: { width: `${f * 100}%` } })));
      return;
    }
    const rows = this.rows();
    const done = rows.filter((t) => t.status === 'done');
    const subset = done.filter((t) => t.subset);
    const stale = rows.filter((t) => t.status === 'stale').length;
    const failed = rows.filter((t) => t.status === 'failed').length;
    const notes = [];
    if (subset.length === 1) notes.push(`${subset[0].name} ran on a ${subset[0].cameras.length}-camera subset`);
    else if (subset.length) notes.push(`${subset.length} ran on a camera subset`);
    if (stale) notes.push(`${stale} stale`);
    if (failed) notes.push(`${failed} failed`);
    if (!notes.length && !done.length) notes.push(this.activeProfile() ? 'tick takes and process them' : 'calibrate a profile first');
    B.append(h('div', { class: 'mc-banner-row' }, icon('person-running', 'mc-banner-ic'),
      h('span', { class: 'mc-banner-txt' }, `${done.length} of ${rows.length} takes processed`, notes.length ? h('span', { class: 'mc-sub' }, ` — ${notes.join(' · ')}`) : null)),
    h('div', { class: 'mc-bar' }, h('div', { style: { width: `${rows.length ? (done.length / rows.length) * 100 : 0}%` } })));
  },
};
window.addEventListener('pagehide', () => MocapScreen.flush());   // a settings change made just before closing the tab
