// Session timeline: ruler, takes row, one lane per camera with file bars and
// waveforms. Two stacked canvases: `base` (redrawn on data/view changes) and
// `over` (playhead, marks, drag previews — redrawn every frame while playing).

const LABEL_W = 88;
const RULER_H = 24;
const TAKES_H = 28;
const GAP = 6;
const LANE_H = 30;
const LANE_GAP = 4;
const PAD_B = 20;
const EDGE_PX = 6;
const SNAP_PX = 7;
const FONT = '"JetBrains Mono", ui-monospace, monospace';

const C = {
  orange: '#E89768',
  text: '#d3d3d3',
  dim: '#7a7a7a',
  rule: '#4f4f4f',
  lane: 'rgba(255,255,255,0.02)',
  wave: 'rgba(241,241,241,0.26)',
  takeFill: 'rgba(232,151,104,0.10)',
  takeFillSel: 'rgba(232,151,104,0.30)',
  takeStroke: 'rgba(232,151,104,0.45)',
};
// bar colours by sync method: reference/audio normal, full search orange, metadata amber, none red, manual blue
const METHOD = {
  reference: ['rgba(255,255,255,0.07)', 'rgba(255,255,255,0.22)', 'rgba(255,255,255,0.15)'],
  audio: ['rgba(255,255,255,0.06)', 'rgba(255,255,255,0.18)', 'rgba(255,255,255,0.14)'],
  search: ['rgba(232,151,104,0.12)', 'rgba(232,151,104,0.60)', 'rgba(232,151,104,0.24)'],
  metadata: ['rgba(226,179,91,0.12)', 'rgba(226,179,91,0.60)', 'rgba(226,179,91,0.24)'],
  none: ['rgba(229,103,103,0.12)', 'rgba(229,103,103,0.60)', 'rgba(229,103,103,0.24)'],
  manual: ['rgba(136,128,240,0.14)', 'rgba(136,128,240,0.65)', 'rgba(136,128,240,0.26)'],
};
const TICK_STEPS = [1 / 30, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600];

const pad2 = (n) => String(n).padStart(2, '0');
function rulerLabel(t, step) {
  const neg = t < 0;
  t = Math.abs(t);
  const h = Math.floor(t / 3600);
  const m = Math.floor(t / 60) % 60;
  const s = Math.floor(t) % 60;
  let out = `${h}:${pad2(m)}:${pad2(s)}`;
  if (step < 1) out = `${m}:${pad2(s)}.${String(Math.round((t % 1) * 100)).padStart(2, '0')}`;
  return neg ? `-${out}` : out;
}

/** Max-pyramid of waveform peaks so drawing costs O(pixels), not O(bins). */
function buildLevels(peaks) {
  const levels = [Uint8Array.from(peaks)];
  while (levels[levels.length - 1].length > 64) {
    const prev = levels[levels.length - 1];
    const next = new Uint8Array(Math.ceil(prev.length / 2));
    for (let i = 0; i < next.length; i += 1) next[i] = Math.max(prev[2 * i], prev[2 * i + 1] || 0);
    levels.push(next);
  }
  return levels;
}

export class Timeline {
  /**
   * opts: {
   *   onScrub(t), onSelect({kind, id} | null), onTakesChanged(final),
   *   onCreateTake(start, end), onRenameTake(id, name), loadWave(fileId) -> Promise<wave|null>
   * }
   */
  constructor(container, opts) {
    this.el = container;
    this.opts = opts;
    this.model = { lanes: [], takes: [], span: 60, fps: 30, selected: null, pendingIn: null, pendingOut: null };
    this.t = 0;
    this.t0 = 0;
    this.pps = 10;
    this.fitted = false;
    this.waves = new Map(); // fileId -> {bin, start, levels} | 'loading' | null
    this.drag = null;
    this.base = document.createElement('canvas');
    this.over = document.createElement('canvas');
    this.hint = document.createElement('div');
    this.hint.className = 'tl-hint';
    this.hint.textContent = 'wheel zoom · shift+wheel pan · drag in takes row to add a take · dbl-click or enter to rename';
    container.append(this.base, this.over, this.hint);
    this.bctx = this.base.getContext('2d');
    this.octx = this.over.getContext('2d');
    this.over.addEventListener('pointerdown', (e) => this.onDown(e));
    this.over.addEventListener('pointermove', (e) => this.onMove(e));
    this.over.addEventListener('pointerup', (e) => this.onUp(e));
    this.over.addEventListener('pointercancel', (e) => this.onUp(e));
    this.over.addEventListener('dblclick', (e) => this.onDbl(e));
    this.over.addEventListener('wheel', (e) => this.onWheel(e), { passive: false });
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(container);
    if (document.fonts) document.fonts.ready.then(() => this.drawBase());
    this.resize();
  }

  destroy() {
    this.ro.disconnect();
    this.el.innerHTML = '';
  }

  // ---------------------------------------------------------------- geometry
  get height() {
    return RULER_H + TAKES_H + GAP + Math.max(1, this.model.lanes.length) * (LANE_H + LANE_GAP) + PAD_B;
  }
  x(t) { return LABEL_W + (t - this.t0) * this.pps; }
  tAt(x) { return this.t0 + (x - LABEL_W) / this.pps; }
  laneY(i) { return RULER_H + TAKES_H + GAP + i * (LANE_H + LANE_GAP); }

  resize() {
    const w = this.el.clientWidth;
    const h = this.height;
    this.el.style.height = `${h}px`;
    const dpr = window.devicePixelRatio || 1;
    for (const c of [this.base, this.over]) {
      c.style.width = `${w}px`;
      c.style.height = `${h}px`;
      c.width = Math.round(w * dpr);
      c.height = Math.round(h * dpr);
      c.getContext('2d').setTransform(dpr, 0, 0, dpr, 0, 0);
    }
    this.w = w;
    this.h = h;
    if (!this.fitted && w > LABEL_W + 50) this.fit(false);
    this.clampView();
    this.drawBase();
    this.drawOver();
  }

  fit(redraw = true) {
    const span = Math.max(1, this.model.span);
    const avail = Math.max(50, (this.w || 800) - LABEL_W - 16);
    this.pps = avail / span;
    this.t0 = -8 / this.pps;
    this.fitted = true;
    this.userView = false;
    if (redraw) { this.drawBase(); this.drawOver(); }
  }

  clampView() {
    const span = Math.max(1, this.model.span);
    const avail = Math.max(50, (this.w || 800) - LABEL_W - 16);
    this.pps = Math.min(2400, Math.max(avail / (span * 4), this.pps));
    const visible = avail / this.pps;
    this.t0 = Math.min(span - visible * 0.1, Math.max(-visible * 0.9, this.t0));
  }

  // ---------------------------------------------------------------- data
  setModel(model) {
    const oldLanes = this.model.lanes.length;
    const oldSpan = this.model.span;
    this.model = model;
    if (model.lanes.length !== oldLanes) this.resize();
    if (Math.abs(model.span - oldSpan) > 0.01 && !this.userView) this.fit(false);
    for (const lane of model.lanes) for (const f of lane.files) this.ensureWave(f);
    this.drawBase();
    this.drawOver();
  }

  ensureWave(f) {
    if (!f.wave || this.waves.has(f.waveKey)) return;
    this.waves.set(f.waveKey, 'loading');
    this.opts.loadWave(f).then((w) => {
      this.waves.set(f.waveKey, w && w.peaks ? { bin: w.bin, start: w.start || 0, levels: buildLevels(w.peaks) } : null);
      this.drawBase();
    }).catch(() => this.waves.set(f.waveKey, null));
  }

  setPlayhead(t) {
    const old = this.t;
    this.t = t;
    // keep the playhead visible while playing/stepping
    const x = this.x(t);
    if (!this.drag && (x > this.w - 24 || x < LABEL_W)) {
      const visible = (this.w - LABEL_W) / this.pps;
      this.t0 = x > this.w - 24 ? t - visible * 0.15 : t - visible * 0.85;
      this.clampView();
      this.drawBase();
    } else if (this.liveChanged(old, t)) {
      this.drawBase();
    }
    this.drawOver();
  }

  liveChanged(a, b) {
    for (const lane of this.model.lanes) {
      for (const f of lane.files) {
        const ia = a >= f.start && a < f.start + f.dur;
        const ib = b >= f.start && b < f.start + f.dur;
        if (ia !== ib) return true;
      }
    }
    return false;
  }

  // ---------------------------------------------------------------- drawing
  drawBase() {
    const g = this.bctx;
    const { w, h } = this;
    if (!w) return;
    g.clearRect(0, 0, w, h);
    g.save();
    g.beginPath();
    g.rect(LABEL_W, 0, w - LABEL_W, h);
    g.clip();
    this.drawRuler(g);
    this.drawTakes(g);
    this.drawLanes(g);
    g.restore();
    // labels column
    g.font = `11px ${FONT}`;
    g.textBaseline = 'middle';
    g.fillStyle = C.orange;
    g.fillText('takes', 20, RULER_H + TAKES_H / 2);
    this.model.lanes.forEach((lane, i) => {
      g.fillStyle = lane.excluded ? C.dim : C.text;
      const y = this.laneY(i) + LANE_H / 2;
      let name = lane.name;
      while (g.measureText(name).width > LABEL_W - 24 && name.length > 2) name = name.slice(0, -1);
      g.fillText(name === lane.name ? name : `${name}…`, 20, y);
    });
  }

  drawRuler(g) {
    const { w } = this;
    const minLabelPx = 90;
    let step = TICK_STEPS.find((s) => s * this.pps >= minLabelPx) || 3600;
    if (step < 1 / 30 + 1e-9) step = 1 / (this.model.fps || 30);
    const minor = step >= 1 ? step / (step % 3 === 0 && step < 60 ? 3 : 5) : step / 2;
    const tA = this.tAt(LABEL_W);
    const tB = this.tAt(w);
    g.strokeStyle = C.rule;
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(LABEL_W, RULER_H - 0.5);
    g.lineTo(w, RULER_H - 0.5);
    g.stroke();
    g.beginPath();
    for (let t = Math.floor(tA / minor) * minor; t <= tB; t += minor) {
      const x = Math.round(this.x(t)) + 0.5;
      const major = Math.abs(t / step - Math.round(t / step)) < 1e-6;
      g.moveTo(x, RULER_H);
      g.lineTo(x, RULER_H - (major ? 7 : 3));
    }
    g.stroke();
    g.font = `10px ${FONT}`;
    g.fillStyle = C.text;
    g.textBaseline = 'top';
    for (let t = Math.floor(tA / step) * step; t <= tB; t += step) {
      if (t < -1e-9) continue;
      g.fillText(rulerLabel(t, step), Math.round(this.x(t)) + 3, 3);
    }
    // frame ticks when zoomed in far enough
    const fps = this.model.fps || 30;
    if (this.pps / fps >= 6) {
      g.strokeStyle = 'rgba(255,255,255,0.08)';
      g.beginPath();
      for (let n = Math.floor(tA * fps); n / fps <= tB; n += 1) {
        const x = Math.round(this.x(n / fps)) + 0.5;
        g.moveTo(x, RULER_H);
        g.lineTo(x, RULER_H - 2);
      }
      g.stroke();
    }
  }

  drawTakes(g) {
    const y = RULER_H;
    g.fillStyle = 'rgba(255,255,255,0.015)';
    g.fillRect(LABEL_W, y, this.w - LABEL_W, TAKES_H);
    g.strokeStyle = 'rgba(255,255,255,0.10)';
    g.beginPath();
    g.moveTo(LABEL_W, y + TAKES_H - 0.5);
    g.lineTo(this.w, y + TAKES_H - 0.5);
    g.stroke();
    const sel = this.model.selected;
    const lanesTop = this.laneY(0);
    const lanesBottom = this.laneY(this.model.lanes.length) - LANE_GAP;
    for (const tk of this.model.takes) {
      const x0 = this.x(tk.start);
      const x1 = this.x(tk.end);
      if (x1 < LABEL_W || x0 > this.w) continue;
      const isSel = sel && sel.kind === 'take' && sel.id === tk.id;
      if (isSel) {
        // shade the take across all lanes so coverage is easy to judge
        g.fillStyle = 'rgba(232,151,104,0.05)';
        g.fillRect(x0, lanesTop - GAP, x1 - x0, lanesBottom - lanesTop + GAP);
        g.strokeStyle = 'rgba(232,151,104,0.55)';
        g.setLineDash([3, 3]);
        g.beginPath();
        g.moveTo(Math.round(x0) + 0.5, lanesTop - GAP);
        g.lineTo(Math.round(x0) + 0.5, lanesBottom);
        g.moveTo(Math.round(x1) - 0.5, lanesTop - GAP);
        g.lineTo(Math.round(x1) - 0.5, lanesBottom);
        g.stroke();
        g.setLineDash([]);
      }
      g.fillStyle = isSel ? C.takeFillSel : C.takeFill;
      g.fillRect(x0, y + 3, x1 - x0, TAKES_H - 6);
      g.strokeStyle = isSel ? C.orange : C.takeStroke;
      g.lineWidth = 1;
      g.strokeRect(Math.round(x0) + 0.5, y + 3.5, Math.max(1, Math.round(x1 - x0) - 1), TAKES_H - 7);
      if (isSel) {
        g.fillStyle = C.orange;
        g.fillRect(Math.round(x0), y + 3, 3, TAKES_H - 6);
        g.fillRect(Math.round(x1) - 3, y + 3, 3, TAKES_H - 6);
      }
      if (!tk.export) {
        g.strokeStyle = 'rgba(232,151,104,0.25)';
        g.beginPath();
        g.moveTo(x0, y + TAKES_H / 2);
        g.lineTo(x1, y + TAKES_H / 2);
        g.stroke();
      }
      if (x1 - x0 > 14) {
        g.save();
        g.beginPath();
        g.rect(x0 + 2, y, x1 - x0 - 4, TAKES_H);
        g.clip();
        g.font = `bold 11px ${FONT}`;
        g.fillStyle = isSel ? '#f1f1f1' : C.orange;
        g.textBaseline = 'middle';
        g.fillText(tk.name, Math.max(x0, LABEL_W) + 6, y + TAKES_H / 2 + 0.5);
        g.restore();
      }
    }
  }

  drawLanes(g) {
    const sel = this.model.selected;
    this.model.lanes.forEach((lane, i) => {
      const y = this.laneY(i);
      g.fillStyle = C.lane;
      g.fillRect(LABEL_W, y, this.w - LABEL_W, LANE_H);
      for (const f of lane.files) {
        const x0 = this.x(f.start);
        const x1 = this.x(f.start + f.dur);
        if (x1 < LABEL_W || x0 > this.w) continue;
        const live = this.t >= f.start && this.t < f.start + f.dur;
        const [fill, stroke, fillLive] = METHOD[f.method] || METHOD.audio;
        g.globalAlpha = lane.excluded ? 0.45 : 1;
        g.fillStyle = live ? fillLive : fill;
        g.fillRect(x0, y, x1 - x0, LANE_H);
        this.drawWave(g, f, x0, x1, y);
        const isSel = sel && sel.kind === 'file' && sel.id === f.id;
        g.strokeStyle = isSel ? C.orange : stroke;
        g.lineWidth = isSel ? 2 : 1;
        const inset = isSel ? 1 : 0.5;
        g.strokeRect(Math.round(x0) + inset, y + inset, Math.max(1, Math.round(x1 - x0) - 2 * inset), LANE_H - 2 * inset);
        g.lineWidth = 1;
        if (x1 - x0 > 40) {
          g.save();
          g.beginPath();
          g.rect(x0 + 2, y, x1 - x0 - 4, LANE_H);
          g.clip();
          g.font = `10px ${FONT}`;
          g.textBaseline = 'top';
          g.fillStyle = 'rgba(13,16,20,0.55)';
          const label = f.label;
          const tx = Math.max(x0, LABEL_W) + 6;
          g.fillRect(tx - 3, y + 2, g.measureText(label).width + 6, 13);
          g.fillStyle = '#d3d3d3';
          g.fillText(label, tx, y + 3);
          g.restore();
        }
        g.globalAlpha = 1;
      }
    });
  }

  drawWave(g, f, x0, x1, y) {
    const w = this.waves.get(f.waveKey);
    if (!w || w === 'loading') return;
    const binPx = w.bin * this.pps;
    let lvl = 0;
    while (lvl < w.levels.length - 1 && binPx * 2 ** (lvl + 1) <= 1) lvl += 1;
    const arr = w.levels[lvl];
    const bin = w.bin * 2 ** lvl;
    const mid = y + LANE_H / 2 + 3;
    const amp = (LANE_H - 10) / 2;
    const xa = Math.max(Math.floor(x0), LABEL_W);
    const xb = Math.min(Math.ceil(x1), this.w);
    g.fillStyle = C.wave;
    for (let px = xa; px < xb; px += 1) {
      const ta = this.tAt(px) - f.start - w.start;
      const tb = this.tAt(px + 1) - f.start - w.start;
      let i0 = Math.floor(ta / bin);
      const i1 = Math.max(i0 + 1, Math.ceil(tb / bin));
      if (i1 <= 0 || i0 >= arr.length) continue;
      i0 = Math.max(0, i0);
      let m = 0;
      for (let i = i0; i < Math.min(i1, arr.length); i += 1) if (arr[i] > m) m = arr[i];
      const hgt = Math.max(0.5, Math.sqrt(m / 255) * amp); // sqrt: music stays visible next to claps
      g.fillRect(px, mid - hgt, 1, hgt * 2);
    }
  }

  drawOver() {
    const g = this.octx;
    const { w, h } = this;
    if (!w) return;
    g.clearRect(0, 0, w, h);
    g.save();
    g.beginPath();
    g.rect(LABEL_W, 0, w - LABEL_W, h);
    g.clip();
    const m = this.model;
    // pending in/out marks
    const pIn = m.pendingIn;
    const pOut = m.pendingOut;
    if (pIn != null || pOut != null) {
      const a = pIn ?? Math.min(pOut, this.t);
      const b = pOut ?? Math.max(pIn, this.t);
      g.strokeStyle = C.orange;
      g.setLineDash([4, 3]);
      g.fillStyle = 'rgba(232,151,104,0.08)';
      const x0 = this.x(Math.min(a, b));
      const x1 = this.x(Math.max(a, b));
      g.fillRect(x0, RULER_H + 3, x1 - x0, TAKES_H - 6);
      g.strokeRect(Math.round(x0) + 0.5, RULER_H + 3.5, Math.max(1, Math.round(x1 - x0)), TAKES_H - 7);
      g.setLineDash([]);
      for (const [v, lbl] of [[pIn, 'I'], [pOut, 'O']]) {
        if (v == null) continue;
        const x = Math.round(this.x(v)) + 0.5;
        g.beginPath();
        g.moveTo(x, RULER_H);
        g.lineTo(x, RULER_H + TAKES_H);
        g.stroke();
        g.font = `bold 10px ${FONT}`;
        g.fillStyle = C.orange;
        g.textBaseline = 'top';
        g.fillText(lbl, x + 3, RULER_H + 4);
      }
    }
    // take being drawn
    if (this.drag && this.drag.mode === 'create' && this.drag.moved) {
      const x0 = this.x(Math.min(this.drag.t0, this.drag.t1));
      const x1 = this.x(Math.max(this.drag.t0, this.drag.t1));
      g.fillStyle = 'rgba(232,151,104,0.18)';
      g.fillRect(x0, RULER_H + 3, x1 - x0, TAKES_H - 6);
      g.strokeStyle = C.orange;
      g.strokeRect(Math.round(x0) + 0.5, RULER_H + 3.5, Math.max(1, Math.round(x1 - x0)), TAKES_H - 7);
    }
    // playhead
    const x = Math.round(this.x(this.t)) + 0.5;
    g.strokeStyle = C.orange;
    g.lineWidth = 1;
    g.beginPath();
    g.moveTo(x, 0);
    g.lineTo(x, h);
    g.stroke();
    g.fillStyle = C.orange;
    g.fillRect(x - 4.5, 0, 9, 9);
    g.restore();
  }

  // ---------------------------------------------------------------- hit testing
  zone(y) {
    if (y < RULER_H) return 'ruler';
    if (y < RULER_H + TAKES_H) return 'takes';
    if (y < RULER_H + TAKES_H + GAP) return 'gap';
    const i = Math.floor((y - this.laneY(0)) / (LANE_H + LANE_GAP));
    if (i >= 0 && i < this.model.lanes.length && y - this.laneY(i) <= LANE_H) return `lane:${i}`;
    return 'empty';
  }

  hitTake(x) {
    let best = null;
    for (const tk of this.model.takes) {
      const x0 = this.x(tk.start);
      const x1 = this.x(tk.end);
      if (Math.abs(x - x0) <= EDGE_PX && x1 - x0 > EDGE_PX) return { take: tk, part: 'start' };
      if (Math.abs(x - x1) <= EDGE_PX) return { take: tk, part: 'end' };
      if (x > x0 && x < x1) best = { take: tk, part: 'body' };
    }
    return best;
  }

  hitFile(laneIdx, t) {
    const lane = this.model.lanes[laneIdx];
    if (!lane) return null;
    let hit = null;
    for (const f of lane.files) if (t >= f.start && t < f.start + f.dur) hit = f;
    return hit;
  }

  local(e) {
    const r = this.over.getBoundingClientRect();
    return { x: e.clientX - r.left, y: e.clientY - r.top };
  }

  snap(t, exclude) {
    const fps = this.model.fps || 30;
    const cands = [this.t];
    for (const lane of this.model.lanes) for (const f of lane.files) cands.push(f.start, f.start + f.dur);
    for (const tk of this.model.takes) if (tk !== exclude) cands.push(tk.start, tk.end);
    let best = null;
    let bestPx = SNAP_PX;
    for (const c of cands) {
      const d = Math.abs(this.x(c) - this.x(t));
      if (d < bestPx) { best = c; bestPx = d; }
    }
    if (best != null) return best;
    return Math.round(t * fps) / fps;
  }

  // ---------------------------------------------------------------- pointer
  onDown(e) {
    if (e.button !== 0) return;
    const { x, y } = this.local(e);
    if (x < LABEL_W) return;
    this.over.setPointerCapture(e.pointerId);
    const t = this.tAt(x);
    const zone = this.zone(y);
    if (zone === 'takes') {
      const hit = this.hitTake(x);
      if (hit) {
        this.opts.onSelect({ kind: 'take', id: hit.take.id });
        this.drag = {
          mode: hit.part === 'body' ? 'move' : hit.part, take: hit.take, tStart: t, x0: x,
          orig: { start: hit.take.start, end: hit.take.end }, moved: false,
        };
      } else {
        this.drag = { mode: 'create', t0: this.snap(t), t1: this.snap(t), x0: x, moved: false };
      }
      return;
    }
    if (zone.startsWith('lane:')) {
      const f = this.hitFile(Number(zone.slice(5)), t);
      if (f) this.opts.onSelect({ kind: 'file', id: f.id });
    }
    this.drag = { mode: 'scrub' };
    this.opts.onScrub(t);
  }

  onMove(e) {
    const { x, y } = this.local(e);
    if (!this.drag) {
      this.updateCursor(x, y);
      return;
    }
    const d = this.drag;
    const t = this.tAt(x);
    if (Math.abs(x - (d.x0 ?? x)) > 3) d.moved = true;
    const fps = this.model.fps || 30;
    const minLen = 2 / fps;
    if (d.mode === 'scrub') {
      this.opts.onScrub(t);
    } else if (d.mode === 'create') {
      d.t1 = this.snap(t);
      this.drawOver();
    } else if (d.moved) {
      const tk = d.take;
      if (d.mode === 'move') {
        const len = d.orig.end - d.orig.start;
        let start = d.orig.start + (t - d.tStart);
        const sStart = this.snap(start, tk);
        const sEnd = this.snap(start + len, tk);
        if (Math.abs(sStart - start) <= Math.abs(sEnd - (start + len))) start = sStart;
        else start = sEnd - len;
        tk.start = start;
        tk.end = start + len;
      } else if (d.mode === 'start') {
        tk.start = Math.min(this.snap(t, tk), tk.end - minLen);
      } else if (d.mode === 'end') {
        tk.end = Math.max(this.snap(t, tk), tk.start + minLen);
      }
      this.opts.onTakesChanged(false);
      this.drawBase();
      this.drawOver();
    }
  }

  onUp(e) {
    const d = this.drag;
    this.drag = null;
    if (this.over.hasPointerCapture(e.pointerId)) this.over.releasePointerCapture(e.pointerId);
    if (!d) return;
    if (d.mode === 'create') {
      const a = Math.min(d.t0, d.t1);
      const b = Math.max(d.t0, d.t1);
      if (d.moved && b - a >= 0.2) this.opts.onCreateTake(a, b);
      else {
        this.opts.onSelect(null);
        this.opts.onScrub(this.tAt(d.x0));
      }
      this.drawOver();
    } else if (d.mode !== 'scrub' && d.moved) {
      this.opts.onTakesChanged(true);
    }
  }

  updateCursor(x, y) {
    let cur = 'default';
    if (x >= LABEL_W) {
      const zone = this.zone(y);
      if (zone === 'takes') {
        const hit = this.hitTake(x);
        cur = !hit ? 'crosshair' : hit.part === 'body' ? 'grab' : 'ew-resize';
      } else if (zone.startsWith('lane:') && this.hitFile(Number(zone.slice(5)), this.tAt(x))) {
        cur = 'pointer';
      } else {
        cur = 'col-resize';
      }
    }
    this.over.style.cursor = cur;
  }

  onDbl(e) {
    const { x, y } = this.local(e);
    if (this.zone(y) !== 'takes') return;
    const hit = this.hitTake(x);
    if (hit) this.startRename(hit.take);
  }

  startRename(tk) {
    this.el.querySelector('.rename')?.remove();
    // keep the editor on screen even when the take is scrolled out of view (Enter on a selected take)
    const w = Math.max(120, Math.min(260, this.x(tk.end) - Math.max(LABEL_W, this.x(tk.start))));
    const x0 = Math.max(LABEL_W, Math.min(this.x(tk.start), this.w - w - 4));
    const input = document.createElement('input');
    input.className = 'rename';
    input.value = tk.name;
    input.style.left = `${x0}px`;
    input.style.top = `${RULER_H + 2}px`;
    input.style.width = `${w}px`;
    let done = false;
    const finish = (commit) => {
      if (done) return;
      done = true;
      if (commit && input.value.trim()) this.opts.onRenameTake(tk.id, input.value.trim());
      input.remove();
    };
    input.addEventListener('keydown', (ev) => {
      ev.stopPropagation();
      if (ev.key === 'Enter') finish(true);
      if (ev.key === 'Escape') finish(false);
    });
    input.addEventListener('blur', () => finish(true));
    this.el.append(input);
    input.focus();
    input.select();
  }

  onWheel(e) {
    e.preventDefault();
    this.userView = true;
    const { x } = this.local(e);
    if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
      const d = (Math.abs(e.deltaX) > Math.abs(e.deltaY) ? e.deltaX : e.deltaY);
      this.t0 += d / this.pps;
    } else {
      const tc = this.tAt(Math.max(LABEL_W, x));
      this.pps *= Math.exp(-e.deltaY * 0.0015);
      this.clampView();
      this.t0 = tc - (Math.max(LABEL_W, x) - LABEL_W) / this.pps;
    }
    this.clampView();
    this.drawBase();
    this.drawOver();
  }
}
