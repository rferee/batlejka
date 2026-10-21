// Cameras screen.
import { applyFit } from '../grid.js';
import {
  PUT, S, activeJob, basename, confirmModal, fmtClock, fmtDur, fpsLabel,
  h, icon, mediaUrl, nextFree, render, sUrl,
  startJob, tip, toast, videoFiles,
} from '../app.js';

// ------------------------------------------------------------------ Cameras screen
const PHONE_MAKE = /apple|google|samsung|xiaomi|oneplus|huawei|motorola|oppo|vivo|iphone|ipad|pixel|android/i;
const SHOWN = 5;   // files per camera card before "+ N more files"
const GROUPED_TIP = {
  'metadata and background': 'Files with the same make, model, codec, resolution and frame rate go together; identical devices are told apart by their static background and by clips that couldn\'t have been recorded at once.',
  subfolder: 'Each subfolder of the session\'s source folder became one camera.',
  you: 'Cameras you made by moving files into a new camera.',
};
const ROT_TIP = 'Turn each camera until the person stands upright in the sample frame. The preview only rotates on screen; the turn is applied when takes are exported.';

export const CamerasScreen = {
  cams: [],
  assign: {},
  dirty: false,
  saving: false,
  fits: [],
  open: {},       // camera id -> file list expanded
  moved: {},      // file id -> moved_from note (or null) of moves not saved yet; saved ones are on the files
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
    this.open = {};
    this.moved = {};
  },
  load() {
    this.cams = structuredClone(S.data?.cameras || []);
    this.assign = {};
    this.moved = {};
  },
  /** The "moved here from …" note of a file: the server's (PUT /cameras records it), or this screen's until saved. */
  moveOf(f) { return f.id in this.moved ? this.moved[f.id] : f.moved_from; },
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
    const at = (f) => this.moveOf(f)?.at || 0;
    return videoFiles().filter((f) => this.camOf(f) === cid)
      .sort((a, b) => at(b) - at(a) || (a.offset ?? 1e12) - (b.offset ?? 1e12) || a.rel.localeCompare(b.rel));
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
    this.moved = {};
    await startJob('/regroup');
  },
  move(f, cid) {
    const was = this.camOf(f);
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
    // the note shows at once, by the server's own rule (PUT /cameras); the saved one replaces it
    let note = this.moveOf(f);
    if (!note && was && was !== cid) note = { camera: was, name: this.cams.find((c) => c.id === was)?.name || was, at: Date.now() / 1000 };
    this.moved[f.id] = note && note.camera !== cid ? note : null;
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
    const top = r.scrollTop;   // re-rendering (expand a list, move a file) must not jump back up
    r.innerHTML = '';
    this.fits = [];
    this.ro.disconnect();
    if (!S.data) return;
    const vids = videoFiles();
    const n = this.cams.length;
    const names = {};
    for (const c of this.cams) names[c.name] = (names[c.name] || 0) + 1;
    const orphans = vids.filter((f) => !this.cams.some((c) => c.id === this.camOf(f)));
    r.append(h('div', { class: 'cam-top' },
      h('div', { class: 'page-head cam-top-head' },
        h('h1', null, 'Cameras'),
        h('p', null, 'Fix orientation against a sample frame and name the cameras.')),
      this.banner(vids, orphans, Object.keys(names).filter((k) => names[k] > 1))));
    const grid = h('div', { class: 'camgrid' });
    this.grid = grid;
    for (const c of this.cams) grid.append(this.card(c, names[c.name] > 1));
    if (orphans.length) grid.append(this.orphanCard(orphans));
    r.append(grid);
    r.scrollTop = top;
    requestAnimationFrame(() => this.refit());
  },
  /** Status line like Process's: how the files were grouped, what needs a look, and the regroup button. */
  banner(vids, orphans, dupNames) {
    const n = this.cams.length;
    const by = {};
    for (const c of this.cams) {
      const how = c.grouped_by === 'subfolder' ? 'subfolder' : c.grouped_by === 'manual' ? 'you' : 'metadata and background';
      by[how] = (by[how] || 0) + 1;
    }
    const kinds = Object.keys(by);
    const moved = vids.filter((f) => this.moveOf(f)).length;
    // one sentence: "Grouped 98 files under 5 cameras by metadata and background, 2 moved by you."
    const how = [];
    if (kinds.length === 1) how.push(' by ', tip(kinds[0], GROUPED_TIP[kinds[0]]));
    else {
      kinds.forEach((k, i) => how.push(i ? ', ' : ' (', `${by[k]} by `, tip(k, GROUPED_TIP[k])));
      if (kinds.length) how.push(')');
    }
    if (moved) how.push(`, ${moved} moved by you`);
    const items = [];
    if (orphans.length) {
      const one = orphans.length === 1;
      items.push(['without a camera', `${orphans.length} file${one ? '' : 's'}: pick a camera for ${one ? 'it' : 'them'} below`]);
    }
    if (dupNames.length) items.push(['name used twice', `${dupNames.join(', ')}: export file names would collide`]);
    const busy = activeJob('regroup');
    let sign;
    if (busy) sign = icon('spinner', 'fa-spin proc-sign muted');
    else if (!vids.length) sign = icon('folder-open', 'proc-sign muted');
    else if (items.length) sign = icon('triangle-exclamation', 'proc-sign warn');
    else sign = icon('check', 'proc-sign ok');
    const grouped = vids.length - orphans.length;
    return h('div', { class: 'proc-banner' },
      sign,
      h('div', { class: 'lines' },
        h('span', { class: 'headline' },
          vids.length ? [`Grouped ${grouped} file${grouped === 1 ? '' : 's'} under ${n} camera${n === 1 ? '' : 's'}`, ...how, '.'] : 'No video files yet'),
        items.map(([title, detail]) => h('span', { class: 'item' },
          h('span', { class: 'mark', style: { background: '#E89768' } }),
          h('span', null, h('b', null, title), ' — ', detail)))),
      h('button', { class: 'btn sm proc-rescan', 'data-regroup': '1', disabled: !!busy, onclick: () => this.regroup() },
        icon('wand-magic-sparkles'), 'Re-run auto grouping'));
  },
  // ---------------------------------------------------------------- drag a file onto another camera
  dragStart(e, f, row) {
    this.dragFid = f.id;
    e.dataTransfer.setData('text/plain', f.rel);
    e.dataTransfer.effectAllowed = 'move';
    row.classList.add('dragging');
    const src = row.closest('.camcard');
    if (src) src.classList.add('drag-src');
    if (this.grid) this.grid.classList.add('dragging-file');
  },
  dragEnd(row) {
    this.dragFid = null;
    row.classList.remove('dragging');
    if (this.grid) this.grid.classList.remove('dragging-file');
    for (const el of this.root.querySelectorAll('.camcard.drop-target, .camcard.drag-src')) el.classList.remove('drop-target', 'drag-src');
  },
  /** Make a camera card accept dropped files (any file not already on this camera). */
  dropTarget(card, c) {
    const dragged = () => {
      const f = this.dragFid && videoFiles().find((x) => x.id === this.dragFid);
      return f && this.camOf(f) !== c.id ? f : null;
    };
    card.addEventListener('dragover', (e) => {
      if (!dragged()) return;
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      card.classList.add('drop-target');
    });
    card.addEventListener('dragleave', (e) => {
      if (!card.contains(e.relatedTarget)) card.classList.remove('drop-target');
    });
    card.addEventListener('drop', (e) => {
      const f = dragged();
      card.classList.remove('drop-target');
      if (!f) return;
      e.preventDefault();
      this.dragFid = null;
      this.move(f, c.id);
    });
    return card;
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
    const row = h('div', {
      class: 'camfile', draggable: 'true', title: `${f.rel} (drag onto another camera to move it)`,
      ondragstart: (e) => this.dragStart(e, f, row), ondragend: () => this.dragEnd(row),
    }, h('span', { class: 'cam-grip', 'aria-hidden': 'true' }, icon('grip-vertical')), mini,
    h('div', { class: 'meta' },
      h('span', { class: 'ellipsis', style: { color: 'var(--fg-1)' } }, f.rel),
      h('span', { class: 'dim' }, fmtDur(f.probe.duration)),
      this.moveOf(f) ? h('span', { class: 'cam-moved' }, `moved here from ${this.moveOf(f).name || 'another camera'}`) : null),
    this.moveSelect(f));
    return row;
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
      box.append(h('div', { class: 'cam-noframe', role: 'img', 'aria-label': 'no preview frame' },
        icon('person'), h('span', null, 'no preview frame')));
    }
    if (first) {
      const sample = `sample · ${basename(first.rel)} @ ${fmtClock(0.3 * (p.duration || 0))}`;
      box.append(h('span', { class: 'chip tl', title: sample }, sample));
    }
    box.append(rotChip);

    const angles = [0, 90, 180, 270];
    const angleBtns = angles.map((a) => h('button', { class: a === c.rotation ? 'on' : '', onclick: () => setRot(a) }, `${a}°`));
    const setRot = (deg) => {
      c.rotation = ((deg % 360) + 360) % 360;
      if (img) img.classList.add('turn');   // animate the user's turns, not the first layout
      this.refit(); // CSS transform only: nothing is re-encoded until export
      rotChip.textContent = `${c.rotation}°`;
      angleBtns.forEach((b, i) => b.classList.toggle('on', angles[i] === c.rotation));
      this.schedule();
    };
    const make = [p.make, p.model].filter(Boolean).join(' ');
    let rotNote;
    let rotColor = '';
    if (v && v.rotation_meta) rotNote = `upright from its metadata (${v.rotation_meta}°)${c.rotation ? ` · rotated ${c.rotation}° more by you` : ''}`;
    else if (c.rotation) rotNote = `rotated ${c.rotation}° by you`;
    else if (PHONE_MAKE.test(make)) { rotNote = 'no rotation tag — check this one'; rotColor = 'warn'; }
    else rotNote = 'no rotation metadata';

    return this.dropTarget(h('div', { class: 'camcard' },
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
          h('span', null, `${make ? `${make} · ` : ''}${dw}×${dh} · ${v ? fpsLabel(v.nominal_fps) : '?'} fps`),
          h('span', null, tip(rotNote, ROT_TIP, { class: rotColor }))),
        this.fileList(c, files))), c);
  },
  /** The camera's files: the first few, "+ N more files" for the rest, or a note for a single clip. */
  fileList(c, files) {
    const open = !!this.open[c.id];
    const shown = open ? files : files.slice(0, SHOWN);
    const hidden = files.length - SHOWN;
    let note = null;
    if (files.length === 1) {
      let span = 0;
      for (const f of videoFiles()) if (f.offset != null) span = Math.max(span, f.offset + (f.probe.duration || 0));
      const d = files[0].probe.duration || 0;
      note = span && d >= 0.6 * span ? 'one continuous clip for the whole session' : 'one clip';
    }
    return h('div', { class: 'camfiles' },
      shown.map((f) => this.fileRow(f, () => c.rotation)),
      hidden > 0 ? h('button', {
        type: 'button', class: 'proc-more', 'aria-expanded': String(open),
        onclick: () => { this.open[c.id] = !open; this.render(); },
      }, open ? 'Show fewer' : `+ ${hidden} more file${hidden === 1 ? '' : 's'}`, icon(open ? 'chevron-up' : 'chevron-down')) : null,
      note ? h('span', { class: 'cam-note' }, note) : null);
  },
  orphanCard(files) {
    return h('div', { class: 'camcard' },
      h('div', { class: 'cambody' },
        h('b', null, 'Unassigned'),
        h('span', { class: 'note' }, 'pick a camera for these files'),
        h('div', { class: 'camfiles' }, files.map((f) => this.fileRow(f, () => 0)))));
  },
};
window.addEventListener('beforeunload', () => CamerasScreen.flush());
