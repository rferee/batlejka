// 3D skeleton viewer for a processed take (Mocap step): FreeMoCap's 33 MediaPipe body points
// drawn on a plain 2D canvas through a perspective camera. Third person orbits the subject
// (drag, wheel, double-click resets); first person looks out of the subject's eyes.
import { h, icon, prefs } from './app.js';

const GRID_STEP = 500;                 // mm
const FACE = 11;                       // landmarks 0–10 are the face: hidden in first person
const SMOOTH = 3;                      // first-person head pose: average of ±3 frames
const C = {
  bg: '#0d1014', grid: 'rgba(255,255,255,0.08)', bone: '#d3d3d3', joint: '#f1f1f1',
  x: '#E56767', y: '#97d47d', z: '#8880F0', muted: 'rgba(255,255,255,0.6)',
};

const sub = (a, b) => [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
const add = (a, b) => [a[0] + b[0], a[1] + b[1], a[2] + b[2]];
const mul = (a, s) => [a[0] * s, a[1] * s, a[2] * s];
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const mid = (a, b) => [(a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2];
const unit = (a) => { const l = Math.hypot(a[0], a[1], a[2]); return l > 1e-9 ? [a[0] / l, a[1] / l, a[2] / l] : null; };
const ok = (p) => p && Number.isFinite(p[0]) && Number.isFinite(p[1]) && Number.isFinite(p[2]);
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
/** Rotate v about the unit axis k by angle a (Rodrigues). */
function rot(v, k, a) {
  const c = Math.cos(a), s = Math.sin(a);
  return add(add(mul(v, c), mul(cross(k, v), s)), mul(k, dot(k, v) * (1 - c)));
}
function pct(values, q) {
  if (!values.length) return 0;
  const s = Float64Array.from(values).sort();
  return s[Math.min(s.length - 1, Math.max(0, Math.round(q * (s.length - 1))))];
}
const fmtT = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, '0')}`;

export class SkeletonView {
  /** Renders into `el`; `url` = GET …/mocap/takes/<id>/skeleton (none = the empty state). */
  constructor(el, { url = null, fps = null } = {}) {
    this.el = el;
    this.fpsOverride = fps;
    this.mode = prefs.get('skelMode', 'third') === 'first' ? 'first' : 'third';
    this.frame = 0;
    this.pos = 0;
    this.playing = false;
    this.sk = null;
    this.view = { yaw: 0.55, pitch: 0.28, zoom: 1 };
    this.look = { yaw: 0, pitch: 0 };
    this.build();
    if (url) this.load(url);
    else this.setEmpty();
  }

  // ---------------------------------------------------------------- DOM
  build() {
    const segBtn = (m, label) => h('button', { type: 'button', onclick: () => this.setMode(m) }, label);
    this.modeBtns = { first: segBtn('first', 'First person'), third: segBtn('third', 'Third person') };
    this.canvas = h('canvas', { class: 'skel-canvas' });
    this.corner = h('span', { class: 'skel-corner' });
    this.msgEl = h('span', { class: 'skel-msg' });
    this.vp = h('div', { class: 'skel-vp', tabindex: '0', role: 'img' }, this.canvas, this.corner, this.msgEl);
    this.playBtn = h('button', { type: 'button', class: 'skel-play', onclick: () => this.toggle() });
    this.range = h('input', { type: 'range', min: '0', max: '0', step: '1', 'aria-label': 'Frame', oninput: (e) => this.seek(Number(e.target.value)) });
    this.timeEl = h('span', { class: 'skel-time' });
    this.capEl = h('span', { class: 'skel-cap' });
    this.root = h('div', { class: 'skel' },
      h('div', { class: 'seg skel-mode', role: 'group', 'aria-label': 'Camera' }, this.modeBtns.first, this.modeBtns.third),
      this.vp,
      h('div', { class: 'skel-ctl' }, this.playBtn, this.range, this.timeEl),
      this.capEl);
    this.el.append(this.root);

    this.ctx = this.canvas.getContext('2d');
    this.onDown = (e) => {
      if (e.button !== 0) return;
      this.drag = { x: e.clientX, y: e.clientY };
      this.vp.setPointerCapture(e.pointerId);
      this.vp.classList.add('dragging');
    };
    this.onMove = (e) => {
      if (!this.drag) return;
      const dx = e.clientX - this.drag.x;
      const dy = e.clientY - this.drag.y;
      this.drag = { x: e.clientX, y: e.clientY };
      if (this.mode === 'third') {
        this.view.yaw -= dx * 0.008;
        this.view.pitch = clamp(this.view.pitch + dy * 0.008, -1.45, 1.45);
      } else {
        this.look.yaw = clamp(this.look.yaw - dx * 0.004, -1.6, 1.6);
        this.look.pitch = clamp(this.look.pitch + dy * 0.004, -1.2, 1.2);
      }
      this.draw();
    };
    this.onUp = () => { this.drag = null; this.vp.classList.remove('dragging'); };
    this.onWheel = (e) => {
      if (this.mode !== 'third') return;
      e.preventDefault();
      this.view.zoom = clamp(this.view.zoom * Math.exp(e.deltaY * 0.0012), 0.15, 6);
      this.draw();
    };
    this.onDbl = () => { this.resetView(); this.draw(); };
    this.onKey = (e) => {
      if (!this.sk) return;
      const k = e.key;
      if (k === ' ') { e.preventDefault(); this.toggle(); }
      else if (k === 'ArrowLeft' || k === 'ArrowRight') {
        e.preventDefault();
        const d = (e.shiftKey ? Math.round(this.sk.fps) : 1) * (k === 'ArrowLeft' ? -1 : 1);
        this.pause();
        this.seek(this.frame + d);
      } else if (k === 'Home') { e.preventDefault(); this.pause(); this.seek(0); }
      else if (k === 'End') { e.preventDefault(); this.pause(); this.seek(this.sk.n - 1); }
    };
    this.vp.addEventListener('pointerdown', this.onDown);
    this.vp.addEventListener('pointermove', this.onMove);
    this.vp.addEventListener('pointerup', this.onUp);
    this.vp.addEventListener('pointercancel', this.onUp);
    this.vp.addEventListener('wheel', this.onWheel, { passive: false });
    this.vp.addEventListener('dblclick', this.onDbl);
    this.vp.addEventListener('keydown', this.onKey);
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(this.vp);
    this.renderMode();
    this.renderControls();
  }

  resize() {
    const w = this.vp.clientWidth;
    const hgt = this.vp.clientHeight;
    if (!w || !hgt) return;
    const dpr = window.devicePixelRatio || 1;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(hgt * dpr);
    this.w = w;
    this.h = hgt;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.draw();
  }

  setMode(m) {
    this.mode = m === 'first' ? 'first' : 'third';
    prefs.set('skelMode', this.mode);
    this.look = { yaw: 0, pitch: 0 };
    this.renderMode();
    this.draw();
  }

  renderMode() {
    for (const [m, b] of Object.entries(this.modeBtns)) {
      b.classList.toggle('on', m === this.mode);
      b.setAttribute('aria-pressed', String(m === this.mode));
    }
    this.vp.classList.toggle('first', this.mode === 'first');
    const zUp = this.sk ? this.sk.zUp : true;
    this.corner.textContent = this.mode === 'first' ? 'first person · through the eyes' : `3D · mm${zUp ? ' · z up' : ''}`;
    this.vp.setAttribute('aria-label', this.sk
      ? `Skeleton of ${this.sk.take}, ${this.mode} person view, frame ${this.frame + 1} of ${this.sk.n}`
      : 'Skeleton viewer');
  }

  renderControls() {
    const sk = this.sk;
    const ready = !!sk;
    this.playBtn.disabled = !ready;
    this.range.disabled = !ready;
    this.playBtn.replaceChildren(icon(this.playing ? 'pause' : 'play'));
    this.playBtn.setAttribute('aria-label', this.playing ? 'Pause' : 'Play');
    if (!ready) {
      this.range.max = '0';
      this.range.value = '0';
      this.range.style.setProperty('--p', '0%');
      this.timeEl.textContent = '0:00.0 / 0:00.0';
      this.capEl.textContent = this.state === 'empty' ? 'The skeleton plays here after processing.' : '';
      return;
    }
    this.range.max = String(sk.n - 1);
    this.range.value = String(this.frame);
    this.range.style.setProperty('--p', `${sk.n > 1 ? (this.frame / (sk.n - 1)) * 100 : 0}%`);
    this.timeEl.textContent = `${fmtT(this.frame / sk.fps)} / ${fmtT((sk.n - 1) / sk.fps)}`;
    const fpsTxt = Math.abs(sk.fps - Math.round(sk.fps)) < 0.01 ? String(Math.round(sk.fps)) : sk.fps.toFixed(2);
    this.capEl.textContent = `frame ${this.frame + 1} / ${sk.n} · ${fpsTxt} fps · ${sk.L} body points`;
  }

  setMessage(text, err = false) {
    this.msgEl.textContent = text || '';
    this.msgEl.classList.toggle('err', !!err);
  }

  // ---------------------------------------------------------------- data
  setEmpty(text = 'not processed yet') {
    this.pause();
    this.sk = null;
    this.state = 'empty';
    this.setMessage(text);
    this.renderMode();
    this.renderControls();
    this.draw();
  }

  async load(url) {
    this.pause();
    if (this.abort) this.abort.abort();
    if (!url) { this.setEmpty(); return; }
    const ctl = new AbortController();
    this.abort = ctl;
    this.sk = null;
    this.state = 'loading';
    this.setMessage('loading skeleton…');
    this.renderControls();
    this.draw();
    let r;
    let d = null;
    try {
      r = await fetch(url, { signal: ctl.signal });
      try { d = await r.json(); } catch { /* empty body */ }
    } catch (e) {
      if (ctl.signal.aborted) return;
      this.state = 'error';
      this.setMessage(`couldn't load the skeleton: ${e.message}`, true);
      this.renderControls();
      return;
    }
    if (ctl.signal.aborted || this.destroyed) return;
    if (r.status === 404) { this.setEmpty(); return; }
    if (!r.ok || !d || !Array.isArray(d.data)) {
      this.state = 'error';
      this.setMessage((d && d.detail) || `couldn't load the skeleton (${r.status})`, true);
      this.renderControls();
      return;
    }
    if (!d.data.length) { this.setEmpty('no frames in this take'); return; }
    this.sk = prepare(d, this.fpsOverride);
    this.state = 'ready';
    this.frame = 0;
    this.pos = 0;
    this.resetView();
    this.setMessage(this.sk.anyPoint ? '' : 'FreeMoCap found no body points in this take');
    this.renderMode();
    this.renderControls();
    this.draw();
  }

  resetView() {
    this.view = { yaw: 0.55, pitch: 0.28, zoom: 1 };
    this.look = { yaw: 0, pitch: 0 };
  }

  // ---------------------------------------------------------------- playback
  toggle() { if (this.playing) this.pause(); else this.play(); }

  play() {
    if (!this.sk || this.playing) return;
    if (this.frame >= this.sk.n - 1) { this.frame = 0; this.pos = 0; }
    this.playing = true;
    this.last = performance.now();
    const tick = (now) => {
      if (!this.playing) return;
      this.pos += ((now - this.last) / 1000) * this.sk.fps;
      this.last = now;
      if (this.pos >= this.sk.n - 1) { this.pos = this.sk.n - 1; this.playing = false; }
      const f = Math.floor(this.pos);
      if (f !== this.frame || !this.playing) { this.frame = f; this.renderControls(); this.draw(); }
      if (this.playing) this.raf = requestAnimationFrame(tick);
    };
    this.raf = requestAnimationFrame(tick);
    this.renderControls();
  }

  pause() {
    this.playing = false;
    cancelAnimationFrame(this.raf);
    if (this.playBtn) this.renderControls();
  }

  seek(f) {
    if (!this.sk) return;
    this.frame = clamp(Math.round(f), 0, this.sk.n - 1);
    this.pos = this.frame;
    this.renderControls();
    this.renderMode();
    this.draw();
  }

  destroy() {
    this.destroyed = true;
    this.pause();
    if (this.abort) this.abort.abort();
    this.ro.disconnect();
    this.vp.removeEventListener('wheel', this.onWheel);
    this.root.remove();
  }

  // ---------------------------------------------------------------- camera
  camera() {
    const sk = this.sk || DEFAULT_SCENE;
    const W = this.w || 1;
    const H = this.h || 1;
    if (this.mode === 'first' && this.sk) {
      const hp = this.sk.head[this.frame];
      let fwd = rot(hp.fwd, sk.U, this.look.yaw);          // drag looks around: turn about up,
      const right = unit(cross(fwd, sk.U)) || sk.B1;       // then tilt about the side axis
      fwd = rot(fwd, right, -this.look.pitch);
      const up = unit(cross(right, fwd)) || hp.up;
      const fov = 85 * Math.PI / 180;     // wide enough to see one's own hands and knees
      return { pos: hp.pos, fwd, right, up, f: (H / 2) / Math.tan(fov / 2), near: 40, cx: W / 2, cy: H / 2, first: true };
    }
    const fov = 40 * Math.PI / 180;
    const d0 = sk.radius / Math.sin(fov / 2) * 1.08;
    const dist = d0 * this.view.zoom;
    const { yaw, pitch } = this.view;
    const dir = add(mul(add(mul(sk.B0, Math.cos(yaw)), mul(sk.B1, Math.sin(yaw))), Math.cos(pitch)), mul(sk.U, Math.sin(pitch)));
    const pos = add(sk.center, mul(dir, dist));
    const fwd = mul(dir, -1);
    const right = unit(cross(fwd, sk.U)) || sk.B1;
    const up = cross(right, fwd);
    return { pos, fwd, right, up, f: (H / 2) / Math.tan(fov / 2), near: 10, cx: W / 2, cy: H / 2 };
  }

  // ---------------------------------------------------------------- drawing
  draw() {
    const ctx = this.ctx;
    if (!ctx || !this.w) return;
    const W = this.w;
    const H = this.h;
    ctx.fillStyle = C.bg;
    ctx.fillRect(0, 0, W, H);
    const cam = this.camera();
    const toCam = (p) => { const v = sub(p, cam.pos); return [dot(v, cam.right), dot(v, cam.up), dot(v, cam.fwd)]; };
    const scr = (c) => [cam.cx + cam.f * c[0] / c[2], cam.cy - cam.f * c[1] / c[2]];
    const segment = (a, b) => {           // a, b world points → screen segment clipped at the near plane
      let ca = toCam(a);
      let cb = toCam(b);
      if (ca[2] < cam.near && cb[2] < cam.near) return null;
      if (ca[2] < cam.near || cb[2] < cam.near) {
        const t = (cam.near - ca[2]) / (cb[2] - ca[2]);
        const p = [ca[0] + (cb[0] - ca[0]) * t, ca[1] + (cb[1] - ca[1]) * t, cam.near];
        if (ca[2] < cam.near) ca = p; else cb = p;
      }
      return [scr(ca), scr(cb)];
    };

    // floor grid
    const sk = this.sk || DEFAULT_SCENE;
    ctx.strokeStyle = C.grid;
    ctx.lineWidth = 1;
    ctx.beginPath();
    const ext = Math.max(2000, Math.ceil(sk.radius * 2.5 / GRID_STEP) * GRID_STEP);
    const g0 = sk.gridOrigin;
    for (let k = -ext; k <= ext; k += GRID_STEP) {
      for (const [a, b] of [[sk.G0, sk.G1], [sk.G1, sk.G0]]) {
        const s = segment(add(add(g0, mul(a, k)), mul(b, -ext)), add(add(g0, mul(a, k)), mul(b, ext)));
        if (s) { ctx.moveTo(s[0][0], s[0][1]); ctx.lineTo(s[1][0], s[1][1]); }
      }
    }
    ctx.stroke();

    // skeleton
    if (this.sk) {
      const P = this.sk.point;
      const f = this.frame;
      const skipFace = this.mode === 'first';
      ctx.strokeStyle = C.bone;
      ctx.lineWidth = 2;
      ctx.lineCap = 'square';
      ctx.lineJoin = 'miter';
      ctx.beginPath();
      for (const [a, b] of this.sk.connections) {
        if (skipFace && (a < FACE || b < FACE)) continue;
        const pa = P(f, a);
        const pb = P(f, b);
        if (!pa || !pb) continue;
        const s = segment(pa, pb);
        if (s) { ctx.moveTo(s[0][0], s[0][1]); ctx.lineTo(s[1][0], s[1][1]); }
      }
      if (!skipFace) {        // MediaPipe has no neck bone: shoulders' midpoint → mouth (or nose)
        const ls = P(f, 11), rs = P(f, 12);
        const ml = P(f, 9), mr = P(f, 10);
        const top = ml && mr ? mid(ml, mr) : P(f, 0);
        const s = ls && rs && top ? segment(mid(ls, rs), top) : null;
        if (s) { ctx.moveTo(s[0][0], s[0][1]); ctx.lineTo(s[1][0], s[1][1]); }
      }
      ctx.stroke();
      ctx.fillStyle = C.joint;
      for (let i = skipFace ? FACE : 0; i < this.sk.L; i++) {
        const p = P(f, i);
        if (!p) continue;
        const c = toCam(p);
        if (c[2] < cam.near) continue;
        const [x, y] = scr(c);
        const r = cam.first ? clamp(cam.f * 12 / c[2], 2, 7) : 2.5;
        ctx.fillRect(x - r, y - r, r * 2, r * 2);
      }
    }

    // axis gizmo (orientation of world x / y / z in this view)
    const ox = 40;
    const oy = H - 36;
    ctx.font = "12px 'JetBrains Mono', ui-monospace, monospace";
    ctx.lineWidth = 2;
    for (const [axis, color, label] of [[[1, 0, 0], C.x, 'x'], [[0, 1, 0], C.y, 'y'], [[0, 0, 1], C.z, 'z']]) {
      const dx = dot(axis, cam.right);
      const dy = dot(axis, cam.up);
      const ex = ox + dx * 22;
      const ey = oy - dy * 22;
      ctx.strokeStyle = color;
      ctx.beginPath();
      ctx.moveTo(ox, oy);
      ctx.lineTo(ex, ey);
      ctx.stroke();
      ctx.fillStyle = color;
      ctx.fillText(label, ex + (dx >= 0 ? 3 : -10), ey + (dy >= 0 ? -3 : 11));
    }
  }
}

// A scene for the empty / loading state: a floor around the origin, z up.
const DEFAULT_SCENE = {
  center: [0, 0, 900], radius: 1100, U: [0, 0, 1], B0: [0, -1, 0], B1: [1, 0, 0],
  G0: [1, 0, 0], G1: [0, 1, 0], gridOrigin: [0, 0, 0], zUp: true,
};

/** The endpoint's payload → points with gaps held over, framing, floor and the first-person head pose. */
function prepare(d, fpsOverride) {
  const n = d.data.length;
  const L = d.landmarks || 33;
  const xyz = new Float32Array(n * L * 3).fill(NaN);
  let anyPoint = false;
  for (let i = 0; i < L; i++) {
    let last = -1;
    let first = -1;
    for (let f = 0; f < n; f++) {
      const row = d.data[f] || [];
      const o = 3 * i;
      if (row[o] != null && row[o + 1] != null && row[o + 2] != null) {
        last = f;
        if (first < 0) first = f;
        xyz.set([row[o], row[o + 1], row[o + 2]], (f * L + i) * 3);
      } else if (last >= 0) {                  // hold the last good position
        xyz.copyWithin((f * L + i) * 3, (last * L + i) * 3, (last * L + i) * 3 + 3);
      }
    }
    if (first > 0) for (let f = 0; f < first; f++) xyz.copyWithin((f * L + i) * 3, (first * L + i) * 3, (first * L + i) * 3 + 3);
    if (first >= 0) anyPoint = true;
  }
  const point = (f, i) => {
    const o = (f * L + i) * 3;
    const p = [xyz[o], xyz[o + 1], xyz[o + 2]];
    return ok(p) ? p : null;
  };

  // up: shoulders over hips; FreeMoCap's z is up when the board lay on the floor
  const ups = [[], [], []];
  const faces = [[], [], []];
  for (let f = 0; f < n; f += Math.max(1, Math.floor(n / 300))) {
    const ls = point(f, 11), rs = point(f, 12), lh = point(f, 23), rh = point(f, 24);
    if (!ls || !rs || !lh || !rh) continue;
    const u = unit(sub(mid(ls, rs), mid(lh, rh)));
    if (!u) continue;
    u.forEach((v, k) => ups[k].push(v));
    const fc = unit(cross(u, sub(rs, ls)));      // facing direction: up × (left → right shoulder)
    if (fc) fc.forEach((v, k) => faces[k].push(v));
  }
  let U = unit(ups.map((a) => pct(a, 0.5))) || [0, 0, 1];
  const zUp = U[2] > 0.8;
  if (zUp) U = [0, 0, 1];
  let face = unit(faces.map((a) => pct(a, 0.5))) || [0, -1, 0];
  face = unit(sub(face, mul(U, dot(face, U)))) || unit(cross(U, [1, 0, 0])) || [0, 1, 0];
  const B0 = face;
  const B1 = unit(cross(U, B0));

  // framing: robust extent of every point over the take
  const ax = [[], [], []];
  const step = Math.max(1, Math.floor((n * L) / 20000));
  let k = 0;
  for (let f = 0; f < n; f++) {
    for (let i = 0; i < L; i++) {
      if (k++ % step) continue;
      const p = point(f, i);
      if (p) p.forEach((v, j) => ax[j].push(v));
    }
  }
  const lo = ax.map((a) => pct(a, 0.02));
  const hi = ax.map((a) => pct(a, 0.98));
  const center = ax[0].length ? lo.map((v, j) => (v + hi[j]) / 2) : [0, 0, 900];
  const radius = ax[0].length ? Math.max(600, Math.hypot(hi[0] - lo[0], hi[1] - lo[1], hi[2] - lo[2]) / 2) : 1100;

  // floor: z = 0 when z is up, else just under the feet
  let floor = 0;
  if (!zUp) {
    const feet = [];
    for (let f = 0; f < n; f += Math.max(1, Math.floor(n / 300))) {
      for (let i = 27; i < Math.min(L, 33); i++) { const p = point(f, i); if (p) feet.push(dot(p, U)); }
    }
    floor = feet.length ? pct(feet, 0.02) : dot(center, U) - radius;
  }
  const G0 = zUp ? [1, 0, 0] : B0;
  const G1 = zUp ? [0, 1, 0] : B1;
  const below = sub(center, mul(U, dot(center, U) - floor));
  const snap = (v) => Math.round(v / GRID_STEP) * GRID_STEP;
  const gridOrigin = add(add(mul(U, floor), mul(G0, snap(dot(below, G0)))), mul(G1, snap(dot(below, G1))));

  // first-person head pose per frame, smoothed over ±SMOOTH frames: the head's heading (ears →
  // nose, kept horizontal: its pitch is too noisy to look through) with a natural downward gaze
  // and a level horizon
  const GAZE_DOWN = 20 * Math.PI / 180;
  const raw = [];
  for (let f = 0; f < n; f++) {
    const le = point(f, 2), re = point(f, 5), nose = point(f, 0), lear = point(f, 7), rear = point(f, 8);
    const ls = point(f, 11), rs = point(f, 12);
    const pos = le && re ? mid(le, re) : nose;
    let heading = nose && lear && rear ? sub(nose, mid(lear, rear)) : null;
    if (!heading && ls && rs) heading = cross(U, sub(rs, ls));     // no face: where the chest points
    heading = heading ? unit(sub(heading, mul(U, dot(heading, U)))) : null;
    raw.push({ pos, heading });
  }
  const head = [];
  let prev = { pos: center, heading: B0 };
  for (let f = 0; f < n; f++) {
    let pos = [0, 0, 0];
    let heading = [0, 0, 0];
    let np = 0;
    let nh = 0;
    for (let j = Math.max(0, f - SMOOTH); j <= Math.min(n - 1, f + SMOOTH); j++) {
      if (raw[j].pos) { pos = add(pos, raw[j].pos); np++; }
      if (raw[j].heading) { heading = add(heading, raw[j].heading); nh++; }
    }
    pos = np ? mul(pos, 1 / np) : prev.pos;
    heading = (nh && unit(heading)) || prev.heading;
    prev = { pos, heading };
    const fwd = unit(sub(mul(heading, Math.cos(GAZE_DOWN)), mul(U, Math.sin(GAZE_DOWN))));
    const right = unit(cross(fwd, U)) || B1;
    head.push({ pos, fwd, up: unit(cross(right, fwd)) || U });
  }

  return {
    n, L, take: d.take || '', point, anyPoint, connections: d.connections || [],
    fps: fpsOverride || d.fps || 30, U, B0, B1, G0, G1, zUp, center, radius, gridOrigin, head,
  };
}
