// Review screen, plus the takes / export-options store it shares with Export.
import { applyFit, Grid } from '../grid.js';
import { Timeline } from '../timeline.js';
import {
  GET, METHOD_COLORS, POST, PUT, S, activeJob, api, basename,
  cameraById, confirmModal, fileById, fmtClock, fmtDate, fmtDur, fmtOffset, fmtTC,
  fpsFloat, fpsLabel, h, icon, isMounted, mediaUrl, nextFree,
  onJobs, pad2, pollSoon, prefs, refreshSession, render, reveal, sUrl,
  targetFps, tip, toast, videoFiles,
  METHOD_LABEL,
} from '../app.js';
import { METHOD_TIP } from './sync.js';

// ------------------------------------------------------------------ Review & export screen
const SPEEDS = [0.25, 0.5, 1, 2];
export const clone = (x) => JSON.parse(JSON.stringify(x));

export function sessionSpan() {
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

export function coverageText(cov) {
  const missing = cov.filter((c) => c.missing).map((c) => c.cam?.name || '?');
  const padded = cov.filter((c) => !c.missing && c.padded).map((c) => `${c.cam?.name} +${(c.pre + c.gap + c.post).toFixed(1)}s pad`);
  const ok = cov.length - missing.length;
  if (!missing.length && !padded.length) return { ok: true, text: `${ok}/${cov.length} cams` };
  return { ok: false, text: [`${ok}/${cov.length} cams`, ...missing.map((m) => `${m} missing`), ...padded].join(' · ') };
}

export const padText = (c) => [
  c.pre > 0.0005 ? `${c.pre.toFixed(2)} s black before` : '',
  c.gap > 0.0005 ? `${c.gap.toFixed(2)} s black between files` : '',
  c.post > 0.0005 ? `${c.post.toFixed(2)} s black after` : ''].filter(Boolean).join(' · ');

// Takes and export options are edited on both Review and Export, so they live
// here rather than in either screen, and are saved shortly after each change.
export const RV = {
  t: 0, playing: false, speed: 1, sel: null, pendingIn: null, pendingOut: null,
  takes: [], committed: [], undo: [], takesDirty: false, takesSaving: false,
  pendingOffsets: new Map(), opts: null, optsDirty: false,
};
const saveTimers = {};
export const exportCams = () => RV.opts.camera_ids.filter((id) => cameraById(id));

/** Take the session's takes and export options, unless edits to them aren't saved yet. */
export function loadTakes() {
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
export function commitTakeEdit() {
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
export async function saveTakes(beacon = false) {
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
export function queueOptsSave() {
  RV.optsDirty = true;
  clearTimeout(saveTimers.opts);
  saveTimers.opts = setTimeout(() => saveOpts(), 500);
}
export async function saveOpts(beacon = false) {
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
export function leaveTakes() {
  saveTakes(true);
  saveOpts(true);
  Object.assign(RV, {
    t: 0, sel: null, pendingIn: null, pendingOut: null, takes: [], committed: [], undo: [],
    takesDirty: false, takesSaving: false, opts: null, optsDirty: false, syncedAt: undefined,
  });
}

export const ReviewScreen = {
  mount(root) {
    this.side = h('aside', { class: 'side' });
    this.gridEl = h('div', { class: 'grid' });
    this.insp = h('aside', { class: 'inspector' });
    this.transport = h('div', { class: 'transport' });
    this.tlEl = h('div', { class: 'timeline' });
    root.append(this.rootEl = h('div', { class: 'review' },
      this.side,
      h('div', { class: 'rv-main' },
        h('div', { class: 'stage' }, this.gridEl),
        this.transport,
        h('div', { style: { overflowY: 'auto', maxHeight: '46vh', flexShrink: '0' } }, this.tlEl)),
      this.insp));
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
    if (RV.preview === undefined) RV.preview = prefs.get('preview', false);   // Preview: FreeMoCap's videos in the tiles
    this.adopt();
    this.renderAll();
    this.loadPreviews();
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
    this.previews = null;
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
      this.deferTimer = setTimeout(() => { if (isMounted(this)) this.update(); }, 400);
      return;
    }
    this.adopt();
    this.renderAll();
  },
  onJobs() {
    this.renderTransportButtons();
    // a calibration, a mocap run or a preview build ended: new previews, new take colours
    const busy = ['previews', 'mocap_process', 'mocap_calibrate'].some((k) => activeJob(k));
    if (this.fmcBusy && !busy) this.loadPreviews();
    this.fmcBusy = busy;
  },
  /** FreeMoCap's annotated videos per take (GET /previews): colours the takes, feeds Preview. */
  async loadPreviews() {
    const name = S.name;
    try {
      const d = await GET(sUrl('/previews'));
      if (name !== S.name || !isMounted(this)) return;
      this.previews = d;
    } catch { return; }    // previews are a nicety: Cut Takes works without them
    this.buildGridFiles();
    if (RV.playing) this.grid.tick(RV.t); else this.grid.show(RV.t);
    this.refreshTimeline();
    this.renderTransportButtons();
  },
  async togglePreview() {
    RV.preview = !RV.preview;
    prefs.set('preview', RV.preview);
    this.buildGridFiles();
    if (RV.playing) this.grid.tick(RV.t); else this.grid.show(RV.t);
    this.renderTransportButtons();
    const missing = this.previews?.missing || 0;
    if (RV.preview && missing && !activeJob('previews')) {
      try {
        await POST(sUrl('/previews/build'), {});
        toast(`Making ${missing} preview video${missing === 1 ? '' : 's'}; they show up as they're ready`);
        pollSoon();
      } catch (e) { toast(e.message, 'error'); }
    }
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
    if (!RV.preview || !this.previews) return;
    const fps = fpsFloat(targetFps()) || 30;
    for (const t of this.previews.takes) {
      const name = RV.takes.find((x) => x.id === t.take_id)?.name || t.take_id;
      for (const [cid, c] of Object.entries(t.cameras)) {
        if (!c.url) continue;
        if (!this.gridFiles.has(cid)) this.gridFiles.set(cid, []);
        this.gridFiles.get(cid).push({
          id: `pv:${t.take_id}:${cid}`, rel: `${name} · ${t.kind === 'calibration' ? 'board detection' : 'skeleton'}`,
          start: t.start, dur: t.end - t.start, fps, proxyUrl: c.url, thumbUrl: null, preview: true, rotation: 0,
        });
      }
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
      takeKinds: new Map((this.previews?.takes || []).map((t) => [t.take_id, t.kind])),
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
    this.autoBtn = h('button', { class: 'tb', title: 'Propose takes from clip clusters', onclick: () => this.autoTakes() }, icon('wand-magic-sparkles'), 'Auto-detect takes');
    this.addBtn = h('button', { class: 'tb', title: 'Create a take from the I/O marks [N]', onclick: () => this.createFromPending() }, icon('plus'), 'Add take', h('kbd', null, 'N'));
    T.append(
      h('div', { class: 'grp' },
        h('button', { class: 'tb sq', title: 'Previous take', onclick: () => this.jumpTake(-1) }, icon('backward-step')),
        this.playBtn,
        h('button', { class: 'tb sq', title: 'Next take', onclick: () => this.jumpTake(1) }, icon('forward-step'))),
      this.tcEl, this.frEl,
      h('div', { class: 'grp' }, this.autoBtn,
        h('button', { class: 'tb', title: 'Fit the whole session [F]', onclick: () => this.timeline.fit() }, icon('arrows-left-right-to-line'), 'Fit'),
        this.previewBtn = h('button', {
          class: 'tb', 'aria-pressed': 'false', onclick: () => this.togglePreview(),
          'data-tip': 'Show FreeMoCap\'s results in the video tiles: the board detection on calibration takes (blue on the timeline) and the skeleton on processed takes (green). Other takes keep the normal videos.',
        }, icon('eye'), 'Preview')),
      h('span', { class: 'spacer' }),
      h('div', { class: 'grp' },
        h('button', { class: `tb ${RV.pendingIn != null ? 'on' : ''}`, title: 'Mark in [I]', onclick: () => this.markIn() }, icon('arrow-right-to-bracket'), 'Mark in', h('kbd', null, 'I')),
        h('button', { class: `tb ${RV.pendingOut != null ? 'on' : ''}`, title: 'Mark out [O]', onclick: () => this.markOut() }, 'Mark out', h('kbd', null, 'O'), icon('arrow-right-from-bracket')),
        this.addBtn),
      h('button', {
        class: 'tb', 'aria-label': 'Playback speed', 'data-tip': 'Playback speed of the preview; click to cycle through the speeds.', style: { minWidth: '56px', justifyContent: 'center' },
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
    const pv = !!RV.preview;
    this.previewBtn.classList.toggle('on', pv);
    this.previewBtn.setAttribute('aria-pressed', String(pv));
    this.previewBtn.firstChild.className = pv && activeJob('previews') ? 'fa-solid fa-spinner fa-spin' : 'fa-solid fa-eye';
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
    // takes are project blocks: click to make one current (selected + playhead at its in-point);
    // rename with Enter / double-click on the timeline, export choice lives on Export
    const list = h('div', { class: 'take-list', role: 'group', 'aria-label': 'Takes list' });
    for (const tk of RV.takes) {
      const sel = RV.sel && RV.sel.kind === 'take' && RV.sel.id === tk.id;
      const cov = coverageText(takeCoverage(tk, camIds));
      list.append(h('div', { class: 'take-item' },
        h('button', {
          type: 'button', class: 'take-blk', 'aria-pressed': String(!!sel),
          onclick: () => { this.select({ kind: 'take', id: tk.id }); if (RV.playing) this.stop(); this.seek(tk.start); },
        },
        h('span', { class: 'take-name', title: tk.name }, tk.name),
        h('span', { class: 'sub' }, `${fmtTC(tk.start, fps)} → ${fmtTC(tk.end, fps)} · ${(tk.end - tk.start).toFixed(1)}s`),
        h('span', { class: `cov ${cov.ok ? 'ok' : 'warn'}` }, cov.text)),
        h('button', {
          type: 'button', class: 'x', title: 'Delete take', 'aria-label': `Delete ${tk.name}`,
          onclick: (e) => { e.stopPropagation(); this.deleteTake(tk.id); },
        }, icon('xmark'))));
    }
    const scroll = h('div', { class: 'side-scroll' },
      h('div', { class: 'section-title' },
        h('h2', null, 'Takes', h('span', { class: 'count' }, ` ${RV.takes.length}`)),
        h('button', {
          class: 'mini-btn', title: 'Collapse sidebar', 'aria-label': 'Collapse sidebar',
          onclick: () => { prefs.set('side', false); this.renderSide(); },
        }, icon('angles-left'))),
      RV.takes.length ? list : h('div', { class: 'note', style: { lineHeight: '1.5' } },
        'No takes yet. Drag in the takes row, mark I / O and press N, or use Auto-detect takes.'));
    A.append(scroll);
    scroll.scrollTop = top;
    // a take selected elsewhere (timeline, J / L, Export) is scrolled into view
    const selId = RV.sel && RV.sel.kind === 'take' ? RV.sel.id : null;
    const row = selId && selId !== this.sideSel ? scroll.querySelector('.take-blk[aria-pressed="true"]') : null;
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
    const top = key === this.inspKey ? (I.querySelector('.insp-body')?.scrollTop || 0) : 0;
    this.inspKey = key;
    I.innerHTML = '';
    const open = prefs.get('insp', true);
    I.className = `inspector ${open ? '' : 'collapsed'} ${prefs.get('keysOpen', true) ? '' : 'keys-folded'}`;
    if (!open) {
      I.append(h('button', { class: 'side-expand', title: 'Show inspector', onclick: () => { prefs.set('insp', true); this.renderInspector(); this.grid.layout(); } },
        icon('angles-left'), h('span', null, 'Inspector')));
      return;
    }
    const collapse = h('button', {
      class: 'mini-btn', title: 'Hide inspector', 'aria-label': 'Hide inspector',
      onclick: () => { prefs.set('insp', false); this.renderInspector(); setTimeout(() => this.grid.layout(), 320); },
    }, icon('angles-right'));
    // small windows on top (what's selected), Hotkeys pinned at the bottom of the column
    const body = h('div', { class: 'insp-body' });
    I.append(h('h2', { class: 'insp-title' }, 'Inspector', collapse), body, this.hotkeysWindow());
    const sel = RV.sel;
    if (sel && sel.kind === 'file' && fileById(sel.id)) this.inspFile(body, fileById(sel.id));
    else if (sel && sel.kind === 'take' && this.selectedTake()) this.inspTake(body, this.selectedTake());
    else this.inspSession(body);
    body.scrollTop = top;
  },
  hotkeysWindow() {
    const open = prefs.get('keysOpen', true);
    const list = (rows, cls = '') => h('dl', { class: `keys ${cls}`.trim() },
      rows.map(([keys, action]) => [h('dt', null, h('kbd', null, keys)), h('dd', null, action)]));
    return h('section', { class: 'insp-win hotkeys', 'aria-label': 'Hotkeys' },
      h('div', { class: 'win-head' },
        h('h3', null, 'Hotkeys'),
        h('button', {
          class: 'mini-btn', 'aria-expanded': String(open), 'aria-controls': 'hotkeys-list',
          'aria-label': open ? 'Collapse hotkeys' : 'Expand hotkeys', title: open ? 'Collapse hotkeys' : 'Expand hotkeys',
          onclick: () => { prefs.set('keysOpen', !open); this.renderInspector(); },
        }, icon(open ? 'chevron-down' : 'chevron-up'))),
      open ? h('div', { class: 'keys-list', id: 'hotkeys-list' },
        list(HOTKEYS), h('span', { class: 'keys-sub' }, 'mouse'), list(MOUSE_HINTS, 'mouse')) : null);
  },
  inspSession(I) {
    const vids = videoFiles();
    const counts = {};
    for (const f of vids) {
      const m = f.offset == null ? 'none' : (f.sync?.method || 'none');
      counts[m] = (counts[m] || 0) + 1;
    }
    const st = S.data.state;
    I.append(
      insWindow('Session',
        h('div', { class: 'props' },
          h('span', null, 'name'), h('span', null, S.name),
          h('span', null, 'files'), h('span', null, `${vids.length}`),
          h('span', null, 'cameras'), h('span', null, `${S.data.cameras.length} (${S.data.cameras.filter((c) => c.include).length} shown)`),
          h('span', null, 'length'), h('span', null, fmtClock(sessionSpan())),
          h('span', null, 'target fps'), h('span', null, `${fpsLabel(targetFps())}`),
          h('span', null, 'synced'), h('span', { class: st.synced_at ? 'ok nowrap' : 'warn' }, st.synced_at ? fmtDate(st.synced_at) : 'not yet — see the Sync step')),
        h('label', { class: 'field' }, tip('Audio from', 'Which camera\'s sound plays with the preview. Playback only: exports keep each camera\'s own audio.'),
          h('select', {
            class: 'input',
            onchange: (e) => {
              RV.audioCam = e.target.value || null;
              prefs.set(RV.audioKey, RV.audioCam);
              this.grid.setAudio(RV.audioCam);
            },
          }, h('option', { value: '' }, 'muted'),
          S.data.cameras.filter((c) => c.include).map((c) => h('option', { value: c.id, selected: c.id === RV.audioCam }, c.name))))),
      insWindow('Placement',
        Object.keys(METHOD_LABEL).filter((m) => counts[m]).map((m) => h('div', { class: 'place-row' },
          h('span', { class: 'mark', style: { background: METHOD_COLORS[m] } }), h('span', { style: { flex: 1 } }, tip(METHOD_LABEL[m], METHOD_TIP[m])), h('span', { class: 'muted' }, counts[m]))),
        vids.length ? null : h('span', { class: 'note' }, 'no files placed yet')));
  },
  inspFile(I, f) {
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
    I.append(h('section', { class: 'insp-win', 'aria-label': 'File' },
      h('h3', null, h('span', { class: 'ellipsis', title: f.rel }, basename(f.rel))),
      h('div', { class: 'props' },
        h('span', null, 'camera'), h('span', null, cameraById(f.camera)?.name || '—'),
        h('span', null, 'length'), h('span', null, `${fmtDur(p.duration)} · ${v.display_width}×${v.display_height}`),
        h('span', null, 'fps'), h('span', { class: v.nominal_fps === targetFps() ? '' : 'warn' }, `${fpsLabel(v.nominal_fps)}${v.vfr_suspect ? ' · VFR' : ''}`),
        h('span', null, 'audio'), h('span', { class: p.audio ? '' : 'warn' }, p.audio ? 'yes' : 'none'),
        h('span', null, 'created'), h('span', null, fmtDate(p.creation_time)),
        h('span', null, 'sync'), h('span', { style: { color: METHOD_COLORS[method] } }, tip(METHOD_LABEL[method] + (sync.via ? ` · ${sync.via}` : ''), METHOD_TIP[method])),
        sync.reason ? [h('span', null, 'why'), h('span', { class: 'warn' }, sync.reason)] : null,
        sync.score != null ? [h('span', null, tip('score', 'Strength of its audio match: the correlation peak (0–1), then how many times higher it is than the next best peak.')), h('span', null, `${sync.score.toFixed(3)} · ratio ${sync.ratio != null ? sync.ratio.toFixed(2) : '—'}`)] : null,
        sync.clock_dev != null ? [h('span', null, tip('vs clock', 'How far the audio placed this clip from where its camera clock predicts. Within ±W is expected.')), h('span', null, `${sync.clock_dev >= 0 ? '+' : '−'}${Math.abs(sync.clock_dev).toFixed(3)} s from its camera clock`)] : null,
        (sync.flags || []).length ? [h('span', null, tip('flags', 'Problems the sync checks found, such as overlapping another clip of the same camera or sitting outside ±W of its clock.')), h('span', { class: 'warn' }, sync.flags.join(' · '))] : null,
        ref ? [h('span', null, tip('against', 'The placed clip this one\'s audio was matched against.')), h('span', { title: ref.rel }, basename(ref.rel))] : null)),
      insWindow(['Offset', 'Where this file starts on the session timeline. Typing a value or nudging marks the clip as manual, so Sync won\'t override it.'],
        h('label', { class: 'field' }, 'start in session (s)',
          h('div', { class: 'row', style: { gap: '6px', flexWrap: 'nowrap' } }, offInput,
            h('button', { class: 'btn xs', title: 'Jump to clip start', 'aria-label': 'Jump to clip start', onclick: () => this.seek(f.offset || 0) }, icon('arrow-right-to-bracket')))),
        h('div', { class: 'nudge' },
          [[-1, '−1 s'], [-10 / fps, '−10 f'], [-1 / fps, '−1 f'], [1 / fps, '+1 f'], [10 / fps, '+10 f'], [1, '+1 s']]
            .map(([d, l]) => h('button', { onclick: () => nudge(d), title: d > 0 ? 'move clip later' : 'move clip earlier' }, l)))),
      insWindow(['Re-sync against…', 'Matches this file\'s audio against the chosen placed file at any lag and moves it there. The result counts as manual, so Sync keeps it.'],
        p.audio && candidates.length
          ? h('div', { class: 'row', style: { gap: '6px', flexWrap: 'nowrap' } }, refSel,
            h('button', { class: 'btn xs', onclick: () => this.resync(f, refSel.value) }, icon('wave-square'), 'Go'))
          : h('span', { class: 'note' }, p.audio ? 'no other placed file with audio' : 'this file has no audio — nudge it by eye')));
  },
  inspTake(I, tk) {
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
    I.append(h('section', { class: 'insp-win', 'aria-label': 'Take' },
      h('h3', null,
        h('input', {
          class: 'input ghost', value: tk.name, 'aria-label': 'Take name', style: { fontWeight: 700, fontSize: '14px', flex: 1 },
          onkeydown: (e) => { if (e.key === 'Enter') e.target.blur(); },
          onchange: (e) => this.renameTake(tk.id, e.target.value),
        })),
      h('label', { class: 'field' }, 'in (s)',
        h('div', { class: 'row', style: { gap: '4px', flexWrap: 'nowrap' } }, num('start'), h('button', { class: 'btn xs', title: 'Set in to playhead [I]', onclick: () => this.markIn() }, 'I'))),
      h('label', { class: 'field' }, 'out (s)',
        h('div', { class: 'row', style: { gap: '4px', flexWrap: 'nowrap' } }, num('end'), h('button', { class: 'btn xs', title: 'Set out to playhead [O]', onclick: () => this.markOut() }, 'O'))),
      h('div', { class: 'props' },
      h('span', null, 'length'), h('span', null, `${(tk.end - tk.start).toFixed(3)} s`),
      h('span', null, 'frames'), h('span', null, `${Math.round((tk.end - tk.start) * fps)} @ ${fpsLabel(targetFps())}`)),
      h('div', { class: 'row', style: { gap: '6px' } },
        h('button', { class: 'btn sm', onclick: () => this.seek(tk.start) }, icon('backward-step'), 'Go to in'),
        h('button', { class: 'btn sm', onclick: () => this.seek(tk.end) }, 'Go to out', icon('forward-step')),
        h('button', { class: 'btn sm danger', onclick: () => this.deleteTake(tk.id) }, icon('trash'), 'Delete'))),
    insWindow(['Coverage', 'Which cameras have footage in this take. Padded: black and silence fill the gaps so every file has the take\'s length. Cameras without footage are skipped.'],
      cov.map((c) => h('div', { class: 'cov-cam' },
        h('div', { class: 'place-row' },
          h('span', { class: 'mark', style: { background: c.missing ? '#E56767' : c.padded ? '#E89768' : '#97d47d' } }),
          h('b', null, c.cam?.name || '?'), h('span', { class: 'spacer' }),
          h('span', { class: 'muted' }, c.missing ? 'no footage — skipped' : c.padded ? 'padded' : 'full')),
        (c.files || []).map((f) => h('span', { class: 'muted ellipsis', title: f.rel }, basename(f.rel))),
        c.padded ? h('span', { class: 'warn' }, padText(c)) : null))));
  },
};

/** One of the inspector's small windows: a 5% tint card with its title inside; `[title, description]` adds a tooltip. */
function insWindow(title, ...kids) {
  const [label, desc] = Array.isArray(title) ? title : [title];
  return h('section', { class: 'insp-win', 'aria-label': label }, h('h3', null, desc ? tip(label, desc) : label), ...kids);
}

const HOTKEYS = [
  ['space', 'play / pause'], ['← / →', 'step one frame'], ['shift + ← / →', 'step ±1 s'],
  ['I / O', 'mark in / out'], ['N', 'new take'], ['del', 'delete take'],
  ['J / L', 'previous / next take'], ['K', 'deselect'], ['enter', 'rename take'],
  ['home / end', 'jump to start / end'], ['F', 'fit timeline'], ['ctrl + Z', 'undo takes'], ['esc', 'deselect'],
];
const MOUSE_HINTS = [
  ['wheel', 'zoom timeline'], ['shift + wheel', 'pan timeline'], ['wheel drag', 'pan timeline'], ['drag in takes row', 'add a take'],
  ['double-click take', 'rename take'], ['click clip bar', 'nudge its offset'], ['click take', 'edit it'],
];
window.addEventListener('beforeunload', () => {
  saveTakes(true);
  saveOpts(true);
  if (isMounted(ReviewScreen)) ReviewScreen.flushOffsets(true);
});
