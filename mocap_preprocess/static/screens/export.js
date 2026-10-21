// Export screen.
import { gpuCap } from '../setup.js';
import {
  S, activeJob, basename, cancelJob, fmtDate, fmtDur, fmtTC, fpsLabel,
  go, h, icon, lastJob, startJob, targetFps, tip,
} from '../app.js';
import { RV, commitTakeEdit, exportCams, leaveTakes, loadTakes, padText, queueOptsSave, saveOpts, saveTakes, takeCoverage } from './review.js';

const SHOWN = 13;   // takes listed before "+ N more takes"
const COVERAGE = [
  ['#97d47d', 'full', 'The camera has footage for the whole take.'],
  ['#E89768', '+N s', 'The camera\'s footage misses N seconds of the take; black and silence fill them so every file of the take has the same length.'],
  ['#E56767', 'none', 'The camera has no footage in this take, so no file is written for it.'],
];

// ------------------------------------------------------------------ Export screen
export const ExportScreen = {
  showAll: false,
  mount(root) {
    this.root = h('main', { class: 'page xp-page' });
    root.append(this.root);
    this.update();
  },
  unmount() {
    saveTakes();
    saveOpts();
  },
  sessionChanged() {
    this.showAll = false;
    leaveTakes();
  },
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
    // re-rendering must not jump: the page scrolls, the table only sideways
    const keep = [r.scrollTop, this.tableBox?.scrollLeft || 0];
    r.innerHTML = '';
    this.tableBox = null;
    if (!S.data) return;
    const camIds = exportCams();
    this.actions = h('div', { class: 'xp-actions' });
    this.banner = h('div', { class: 'xp-banner' });
    const head = h('div', { class: 'page-head' },
      h('h1', null, 'Export Takes'),
      h('p', null, 'Every ticked take × camera becomes one file in ', h('span', { class: 'accent' }, 'output/<take>/'),
        '. All files of a take get the same length: black and silence fill in where a camera has no footage.'),
      h('p', { class: 'xp-req' }, 'Takes must be exported here before calibration and mocap.'));
    if (!RV.takes.length) {
      r.append(h('div', { class: 'xp-left' }, head),
        h('div', { class: 'xp-right' },
          h('div', { class: 'empty' }, 'No takes yet — cut them in Cut Takes: drag in the takes row, mark I / O and press N, or use Auto-detect takes. ',
            h('button', { class: 'btn sm', onclick: () => go('review') }, 'Open Cut Takes', icon('arrow-right')))));
      r.scrollTop = keep[0];
      return;
    }
    r.append(
      h('div', { class: 'xp-left' }, head, this.outputCard()),
      h('div', { class: 'xp-right' }, this.banner, this.takesTable(camIds)));
    this.renderAction();
    r.scrollTop = keep[0];
    if (this.tableBox) this.tableBox.scrollLeft = keep[1];
  },
  /** Files the export would write (cameras without footage in a take are skipped). */
  count(camIds) {
    let files = 0;
    let skipped = 0;
    let length = 0;
    let takes = 0;
    for (const tk of RV.takes) {
      if (!tk.export) continue;
      takes++;
      length += tk.end - tk.start;
      for (const c of takeCoverage(tk, camIds)) if (c.missing) skipped++; else files++;
    }
    return { files, skipped, length, takes };
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
    const covTitle = (c) => (c.missing ? 'no footage: this camera is left out of this take' : [...c.files.map((f) => basename(f.rel)), padText(c)].filter(Boolean).join('\n'));
    const hidden = Math.max(0, RV.takes.length - SHOWN);
    const shown = this.showAll ? RV.takes : RV.takes.slice(0, SHOWN);
    const rows = shown.map((tk) => {
      const cov = takeCoverage(tk, cams.map((c) => c.id));
      return h('tr', { class: tk.export ? 'on' : 'off', onclick: () => { tk.export = !tk.export; this.takesEdited(); } },
        h('td', { class: 'ck' }, h('span', { class: `check ${tk.export ? 'on' : ''}`, title: 'Export this take' }, icon('check'))),
        h('td', { class: 'nm', title: tk.name }, tk.name),
        h('td', null, `${fmtTC(tk.start, fps)} → ${fmtTC(tk.end, fps)}`),
        h('td', { class: 'num' }, fmtDur(tk.end - tk.start)),
        cov.map((c, i) => h('td', { class: camIds.includes(cams[i].id) ? '' : 'off', title: covTitle(c) }, cell(c))),
        h('td', { class: 'ck' }, h('button', {
          class: 'mini-btn', title: 'Show in Cut Takes', 'aria-label': 'Show in Cut Takes', onclick: (e) => { e.stopPropagation(); this.showInReview(tk); },
        }, icon('eye'))));
    });
    this.tableBox = h('div', { class: 'xp-tablewrap' }, h('table', { class: 'ftable xtable' },
      h('thead', null, h('tr', null,
        h('th', null, ''), h('th', null, 'take'), h('th', null, 'in → out'), h('th', { class: 'num' }, 'length'),
        cams.map((c) => {
          const on = camIds.includes(c.id);
          return h('th', { class: `cam ${on ? '' : 'off'}`, title: on ? 'Leave this camera out' : 'Include this camera', onclick: () => this.toggleCam(c.id) },
            h('span', { class: 'xcov' }, h('span', { class: `check ${on ? 'on' : ''}` }, icon('check')), c.name));
        }),
        h('th', null, ''))),
      h('tbody', null, rows)));
    return h('div', { class: 'xp-takes' },
      h('div', { class: 'xp-toolbar' },
        h('button', { class: 'btn xs', disabled: nOn === RV.takes.length, onclick: () => setAll(true) }, 'Select all'),
        h('button', { class: 'btn xs', disabled: !nOn, onclick: () => setAll(false) }, 'Deselect all'),
        h('span', { class: 'note' }, `${nOn}/${RV.takes.length} takes · ${camIds.length}/${cams.length} cameras · ${fmtDur(length)} per camera`),
        skipped ? h('span', { class: 'small warn' }, tip(`${skipped} file${skipped === 1 ? '' : 's'} left out (no footage)`,
          'A camera that has no footage at all during a take is left out of that take only. The take is still exported with its other cameras.')) : null,
        h('span', { class: 'spacer' }),
        h('div', { class: 'legend xp-legend' },
          COVERAGE.map(([color, label, desc]) => h('span', null, h('span', { class: 'dot sq', style: { background: color } }), tip(label, desc))))),
      this.tableBox,
      hidden ? h('button', {
        class: 'xp-more', 'aria-expanded': String(this.showAll),
        onclick: () => { this.showAll = !this.showAll; this.render(); },
      }, this.showAll ? 'Show fewer' : `+ ${hidden} more take${hidden === 1 ? '' : 's'}`, icon(this.showAll ? 'chevron-up' : 'chevron-down')) : null);
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
  outputCard() {
    const o = RV.opts;
    const fps = targetFps();
    const nv = o.encoder === 'h264_nvenc';
    const seg = (key, values, label = (v) => String(v), why = () => null) => h('div', { class: 'seg' },
      values.map((v) => h('button', {
        class: o[key] === v ? 'on' : '', disabled: !!why(v), title: why(v) || '',
        onclick: () => { o[key] = v; this.optsChanged(); },
      }, label(v))));
    const toggle = (key, label) => h('div', { class: 'toggle', onclick: () => { o[key] = !o[key]; this.optsChanged(); } },
      h('span', { class: `check ${o[key] ? 'on' : ''}` }, icon('check')), label);
    // a setting's name with its picker right under it; a div, as a <label> would click the first button
    const field = (name, ...kids) => h('div', { class: 'field', role: 'group' }, name, ...kids);
    const nvenc = gpuCap('nvenc');
    const noNvenc = nvenc && !nvenc.ok ? `NVENC is unavailable on this machine: ${nvenc.error}` : null;
    const summary = `h264 · ${nv ? `cq ${Number(o.crf) + 2}` : `crf ${o.crf}`} · yuv420p · ${fpsLabel(fps)} fps · ${o.long_side ? `${o.long_side}px long side` : 'source size'}`;
    return h('section', { class: 'card xp-card', 'aria-label': 'Output' },
      h('h2', { class: 'card-title' }, tip('Output', `Every file is written as ${summary}.`)),
      field(tip('Encoder', 'libx264 encodes on the CPU; h264_nvenc on the NVIDIA GPU, much faster at 4K. Both write H.264.'),
        seg('encoder', ['libx264', 'h264_nvenc'], undefined, (v) => (v === 'h264_nvenc' ? noNvenc : null)),
        nv && noNvenc ? h('span', { class: 'note warn' }, 'NVENC failed its test in Setup — this export would fail; pick libx264.') : null),
      nv ? field(tip('Preset', 'NVENC speed against quality. NVENC is usually the bottleneck: p3 is about twice as fast as p5 at 4K, with slightly bigger files.'),
        seg('nvenc_preset', ['p5', 'p4', 'p3'], (v) => ({ p5: 'p5 quality', p4: 'p4', p3: 'p3 fast' })[v])) : null,
      h('label', { class: 'field' }, nv
        ? tip('CQ (−2)', 'Quality on the CRF scale: NVENC is sent CQ = this + 2, which looks about the same as libx264 at this CRF. Lower means better quality and bigger files.')
        : tip('CRF', 'libx264 constant rate factor: lower means better quality and bigger files. Around 17 is visually lossless for most footage.'),
        h('input', { class: 'input', type: 'number', min: '0', max: '51', value: o.crf, onchange: (e) => { o.crf = Math.max(0, Math.min(51, Number(e.target.value) || 17)); this.optsChanged(); } })),
      field(tip('Long side', 'Scales each video down so its longer side is at most this many pixels; keep leaves the source size. Never upscales.'),
        seg('long_side', [0, 1920, 1280, 1080], (v) => (v ? String(v) : 'keep'))),
      field(tip('Files at once', 'How many files are encoded in parallel. Only helps if the encoder isn\'t saturated: a GPU with one NVENC (e.g. RTX 3090) still encodes one file at a time, and libx264 already uses every core.'),
        seg('parallel', [1, 2, 3])),
      field(tip('Layout', 'freemocap writes output/<take>/synchronized_videos/, which Calibrate and Mocap read; flat puts the files straight in output/<take>/.'),
        seg('layout', ['freemocap', 'flat'])),
      field(tip('Target fps', 'Every file is conformed to the session\'s target frame rate, set on the Process step: frames are dropped or duplicated to match.'),
        h('span', { class: 'xp-val' }, `${fpsLabel(fps)} (session)`)),
      toggle('audio', tip('include audio', 'Keeps each camera\'s audio in its files, with silence where it has no footage. Off writes video only.')),
      toggle('overwrite', tip('overwrite existing files', 'Replaces files already in the output folder. Off keeps them and skips those take × camera files.')),
      this.actions);   // the action closes the form it belongs to
  },
  /** The actions (left) and the status banner (right): both follow the export job. */
  renderAction() {
    const A = this.actions;
    const B = this.banner;
    if (!A || !A.isConnected) return;
    const { files, takes } = this.count(exportCams());
    const job = activeJob('export');
    const last = lastJob('export');
    A.innerHTML = '';
    A.append(job
      ? h('button', { class: 'btn primary big xp-run', onclick: () => cancelJob(job.id) }, icon('xmark'), 'Cancel export')
      : h('button', { class: 'btn primary big xp-run', disabled: !files, onclick: () => this.exportNow() }, icon('file-export'),
        files ? `Export ${files} file${files === 1 ? '' : 's'}` : 'Nothing to export'));

    if (!B) return;
    B.innerHTML = '';
    const banner = (ico, color, title, detail, bar, barColor) => {
      B.append(h('div', { class: 'xp-banner-row' },
        h('span', { class: 'xp-banner-icon', style: { color } }, ico),
        h('span', { class: 'xp-banner-text' }, title, detail ? h('span', { class: 'xp-banner-detail' }, ` — ${detail}`) : null)));
      if (bar != null) B.append(h('div', { class: 'xp-bar' }, h('div', { style: { width: `${bar * 100}%`, background: barColor } })));
    };
    if (job) {
      banner(icon('spinner', 'fa-spin'), '#E89768', `Exporting… ${Math.round(job.progress * 100)}%`, job.message || 'starting', job.progress, '#E89768');
    } else if (last && last.status === 'done') {
      const errors = last.result && last.result.errors;
      const written = last.result ? last.result.written : null;
      if (errors) banner(icon('triangle-exclamation'), '#E89768', `done with ${errors} error${errors === 1 ? '' : 's'}`, 'see the job log', 1, '#E89768');
      else banner(icon('check'), '#97d47d', `wrote ${written ?? ''} file${written === 1 ? '' : 's'}`, 'last export finished', 1, '#97d47d');
    } else if (last && last.status === 'error') {
      banner(icon('circle-xmark'), '#E56767', 'export failed', last.error || 'see the job log', 1, '#E56767');
    } else if (last && last.status === 'cancelled') {
      banner(icon('ban'), 'rgba(255,255,255,0.6)', 'export cancelled', 'files already written were kept', 1, 'rgba(255,255,255,0.2)');
    } else if (S.data.state.exported_at) {
      banner(icon('check'), '#97d47d', `${files} file${files === 1 ? '' : 's'} from ${takes} take${takes === 1 ? '' : 's'} ticked`,
        `last export ${fmtDate(S.data.state.exported_at)}`, null);
    } else {
      banner(icon('file-export'), 'rgba(255,255,255,0.6)', `${files} file${files === 1 ? '' : 's'} from ${takes} take${takes === 1 ? '' : 's'} ticked`,
        'nothing exported yet', null);
    }
  },
  async exportNow() {
    await saveTakes();
    await saveOpts();
    const takeIds = RV.takes.filter((t) => t.export).map((t) => t.id);
    const job = await startJob('/export', { take_ids: takeIds, camera_ids: exportCams(), options: RV.opts });
    if (job) this.renderAction();
  },
};
