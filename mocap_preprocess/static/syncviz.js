// Live sync view: one lane per camera. A clip starts blue at its rough
// placement (its raw timestamp, then its camera-clock prediction once the clock
// is solved) and turns green when the audio clicks it into place (orange: only
// its camera clock or a full search placed it — check those). Bars ease
// toward their targets, the view zooms with them, and a landing flashes. Below
// the lanes: per camera pair, the clock votes and the cluster that won.

const FONT = '"JetBrains Mono", ui-monospace, monospace';
const LABEL_W = 156;
const RULER_H = 26;
const LANE_H = 32;
const LANE_GAP = 6;
const PARK_W = 96;          // clips without any timestamp wait here (column shown only when needed)
const VOTE_H = 20;
const VLABEL_W = 250;      // pair names in the votes panel
const VOTE_HEAD = 22;
const VOTE_SPAN = 4;        // vote strip shows ±4 s around the consensus
const PAD = 14;
const TAU = 0.32;           // easing time constant, s
const FLASH_MS = 900;

const C = {
  orange: '#E89768', green: '#97d47d', red: '#E56767', blue: '#8880F0',
  rough: '#8880F0',            // not placed by the audio yet
  text: '#d3d3d3', white: '#f1f1f1', dim: '#8f8f8f', rule: '#4f4f4f',
  muted: 'rgba(255,255,255,0.6)',
  lane: 'rgba(255,255,255,0.05)', hair: 'rgba(255,255,255,0.10)', band: 'rgba(255,255,255,0.10)',
};
const rgba = (hex, a) => {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${n >> 16},${(n >> 8) & 255},${n & 255},${a})`;
};
const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400];
const pad2 = (n) => String(n).padStart(2, '0');

/** Timeline time, 0 being the start of the session: m:ss, h:mm:ss past an hour, tenths when fine. */
function tickLabel(t, fine = false) {
  const r = fine ? Math.round(Math.abs(t) * 10) / 10 : Math.floor(Math.abs(t) + 1e-6);
  const sec = fine ? (r % 60).toFixed(1).padStart(4, '0') : pad2(r % 60);
  const h = Math.floor(r / 3600);
  const s = h ? `${h}:${pad2(Math.floor(r / 60) % 60)}:${sec}` : `${Math.floor(r / 60)}:${sec}`;
  return t < 0 && r > 0 ? `-${s}` : s;
}

/** What a clip looks like, from its state in the snapshot. */
function look(c) {
  if (c.pos != null) {
    switch (c.method) {
      case 'reference':
      case 'audio': return { fill: rgba(C.green, 0.35), stroke: C.green, dash: null, placed: true };
      case 'search': return { fill: rgba(C.orange, 0.18), stroke: C.orange, dash: [4, 3], placed: true };
      case 'manual': return { fill: rgba(C.blue, 0.22), stroke: C.blue, dash: null, placed: true };
      case 'metadata': return { fill: rgba(C.orange, 0.35), stroke: C.orange, dash: null, placed: true };
      case 'none': return { fill: rgba(C.red, 0.16), stroke: rgba(C.red, 0.85), dash: null, placed: true };
      default: return { fill: rgba(C.green, 0.35), stroke: C.green, dash: null, placed: true };
    }
  }
  if (c.kept) return { fill: rgba(C.blue, 0.10), stroke: rgba(C.blue, 0.6), dash: [3, 3], placed: false };
  if (c.pred != null) return { fill: rgba(C.rough, 0.22), stroke: rgba(C.rough, 0.9), dash: null, placed: false };
  return { fill: rgba(C.rough, 0.07), stroke: rgba(C.rough, 0.55), dash: [3, 3], placed: false };
}

export class SyncViz {
  constructor(container) {
    this.el = container;
    this.canvas = document.createElement('canvas');
    this.el.append(this.canvas);
    this.ctx = this.canvas.getContext('2d');
    this.clips = new Map();     // id -> animated state
    this.cams = [];
    this.pairs = [];
    this.voteSeen = new Map();  // pair key -> {n, at: [times]}
    this.view = null;           // [t0, t1] shown
    this.hover = null;
    this.live = false;
    this.raf = 0;
    this.last = performance.now();
    this.canvas.addEventListener('mousemove', (e) => this.onMove(e));
    this.canvas.addEventListener('mouseleave', () => { this.hover = null; this.kick(); });
    this.ro = new ResizeObserver(() => this.resize());
    this.ro.observe(this.el);
    if (document.fonts) document.fonts.ready.then(() => this.kick());
  }

  destroy() {
    cancelAnimationFrame(this.raf);
    this.ro.disconnect();
    this.el.innerHTML = '';
  }

  /** snapshot: {phase, cams:[{id,name,state,ahead,mode,basis}], clips:[{id,cam,name,dur,raw,pred,pos,method,w,active,kept}], pairs:[{a,b,name,votes,center,support,ok}]} */
  setData(snap, { instant = false } = {}) {
    this.phase = snap.phase;
    // positions are on the reference clock; a finished sync says where session time 0 sits on it
    this.origin = typeof snap.frame === 'number' ? -snap.frame : null;
    const lanes = snap.cams.filter((c) => snap.clips.some((x) => x.cam === c.id));
    if (snap.clips.some((x) => !x.cam || !lanes.some((c) => c.id === x.cam))) lanes.push({ id: null, name: 'unassigned', state: 'none' });
    const relayout = lanes.length !== this.cams.length || (snap.pairs || []).length !== this.pairs.length;
    this.cams = lanes;
    this.pairs = (snap.pairs || []).filter((p) => p.votes && p.votes.length)
      .sort((a, b) => (b.ok === true) - (a.ok === true) || b.support - a.support).slice(0, 14);
    const now = performance.now();
    const seen = new Set();
    for (const c of snap.clips) {
      seen.add(c.id);
      const target = c.pos ?? c.pred ?? c.raw;
      let s = this.clips.get(c.id);
      if (!s) {
        s = { x: target, vx: 0, flash: 0, wasPlaced: false };
        this.clips.set(c.id, s);
      }
      const lk = look(c);
      if (lk.placed && !s.wasPlaced && !instant && c.method !== 'none') s.flash = now;
      s.wasPlaced = lk.placed;
      Object.assign(s, c, { target, lk });
      if (instant || s.x == null) s.x = target;
    }
    for (const id of [...this.clips.keys()]) if (!seen.has(id)) this.clips.delete(id);
    // a jump to another frame (session time ↔ reference clock, or hours away) snaps instead of easing
    if (this.view) {
      const tv = this.targetView();
      const span = Math.max(this.view[1] - this.view[0], tv[1] - tv[0]);
      const far = Math.abs((tv[0] + tv[1]) / 2 - (this.view[0] + this.view[1]) / 2) > 6 * span;
      if (far || (tv[0] > 1e8) !== (this.view[0] > 1e8)) instant = true;
    }
    if (instant) for (const s of this.clips.values()) { s.x = s.target; s.vx = 0; s.flash = 0; }
    this.parkedAny = [...this.clips.values()].some((s) => s.target == null);
    for (const p of this.pairs) {
      const key = `${p.a}|${p.b}`;
      const v = this.voteSeen.get(key) || { n: 0, at: [] };
      while (v.at.length < p.votes.length) v.at.push(instant ? 0 : now);
      v.at.length = p.votes.length;
      this.voteSeen.set(key, v);
    }
    if (instant) this.view = null;
    if (relayout) this.resize();
    this.kick();
  }

  counts() {
    const n = { clicked: 0, rough: 0, fallback: 0, manual: 0, none: 0 };
    for (const s of this.clips.values()) {
      if (s.kept || s.method === 'manual') n.manual += 1;
      else if (s.pos == null) n.rough += 1;
      else if (s.method === 'metadata') n.fallback += 1;
      else if (s.method === 'none') n.none += 1;
      else n.clicked += 1;
    }
    return n;
  }

  // ---------------------------------------------------------------- geometry
  get height() {
    const lanes = RULER_H + Math.max(1, this.cams.length) * (LANE_H + LANE_GAP) + PAD;
    const votes = this.pairs.length ? VOTE_HEAD + this.pairs.length * VOTE_H + PAD : 0;
    return lanes + votes;
  }
  get plotW() { return Math.max(50, this.w - LABEL_W - (this.parkedAny ? PARK_W : 0) - PAD); }
  x(t) { return LABEL_W + ((t - this.view[0]) / (this.view[1] - this.view[0])) * this.plotW; }
  laneY(i) { return RULER_H + i * (LANE_H + LANE_GAP); }

  resize() {
    const w = this.el.clientWidth;
    const h = this.height;
    const dpr = window.devicePixelRatio || 1;
    this.canvas.style.width = `${w}px`;
    this.canvas.style.height = `${h}px`;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    this.w = w;
    this.h = h;
    this.kick();
  }

  targetView() {
    let lo = Infinity;
    let hi = -Infinity;
    for (const s of this.clips.values()) {
      if (s.target == null) continue;
      lo = Math.min(lo, s.target);
      hi = Math.max(hi, s.target + s.dur);
    }
    if (!isFinite(lo)) return [0, 60];
    const pad = Math.max(2, (hi - lo) * 0.03);
    return [lo - pad, hi + pad];
  }

  // ---------------------------------------------------------------- animation
  kick() {
    if (!this.raf) this.raf = requestAnimationFrame((t) => this.frame(t));
  }

  frame(now) {
    this.raf = 0;
    if (!this.w) return;
    const dt = Math.min(0.1, (now - this.last) / 1000);
    this.last = now;
    const k = 1 - Math.exp(-dt / TAU);
    let moving = false;
    const tv = this.targetView();
    if (!this.view) this.view = tv;
    else {
      const span = this.view[1] - this.view[0];
      for (let i = 0; i < 2; i += 1) {
        const d = tv[i] - this.view[i];
        if (Math.abs(d) > span * 1e-4) { this.view[i] += d * k; moving = true; } else this.view[i] = tv[i];
      }
    }
    const pxPerS = this.plotW / (this.view[1] - this.view[0]);
    for (const s of this.clips.values()) {
      if (s.target == null || s.x == null) { s.vx = 0; continue; }
      const d = s.target - s.x;
      if (Math.abs(d) * pxPerS > 0.3) {
        const step = d * k;
        s.vx = step / Math.max(dt, 1e-3);
        s.x += step;
        moving = true;
      } else {
        s.x = s.target;
        s.vx = 0;
      }
      if (s.flash && now - s.flash < FLASH_MS) moving = true;
      if (s.active) moving = true;
    }
    for (const v of this.voteSeen.values()) if (v.at.some((t) => now - t < 500)) moving = true;
    this.draw(now);
    if (moving) this.kick();
  }

  // ---------------------------------------------------------------- drawing
  draw(now) {
    const g = this.ctx;
    g.clearRect(0, 0, this.w, this.h);
    g.font = `12px ${FONT}`;
    g.textBaseline = 'middle';
    const t0 = this.timeOrigin();
    this.drawRuler(g, t0);
    const laneOf = new Map(this.cams.map((c, i) => [c.id, i]));
    this.cams.forEach((c, i) => this.drawLane(g, c, i));
    const parked = [];
    const pxPerS = this.plotW / (this.view[1] - this.view[0]);
    g.save();
    g.beginPath();
    g.rect(LABEL_W, 0, this.plotW, this.h);
    g.clip();
    for (const s of this.clips.values()) {
      const li = laneOf.has(s.cam) ? laneOf.get(s.cam) : laneOf.get(null);
      if (li == null) continue;
      if (s.x == null) { parked.push([s, li]); continue; }
      this.drawClip(g, s, li, now, pxPerS);
    }
    g.restore();
    this.drawParked(g, parked, now);
    if (this.pairs.length) this.drawVotes(g, now);
    if (this.hover) this.drawHover(g, t0);
  }

  /** Where session time 0 sits: known once a sync finished, else the earliest clip on the timeline. */
  timeOrigin() {
    if (this.origin != null) return this.origin;
    let lo = Infinity;
    for (const s of this.clips.values()) if (s.target != null && s.target < lo) lo = s.target;
    return Number.isFinite(lo) ? lo : 0;
  }

  drawRuler(g, t0) {
    const span = this.view[1] - this.view[0];
    const step = STEPS.find((s) => (s / span) * this.plotW >= 90) || 86400;
    g.fillStyle = C.muted;
    g.strokeStyle = C.hair;
    g.lineWidth = 1;
    g.textAlign = 'left';
    const first = t0 + Math.ceil((this.view[0] - t0) / step) * step;
    const bottom = RULER_H + this.cams.length * (LANE_H + LANE_GAP);
    for (let t = first; t <= this.view[1]; t += step) {
      const x = Math.round(this.x(t)) + 0.5;
      if (x < LABEL_W || x > LABEL_W + this.plotW) continue;
      g.beginPath();
      g.moveTo(x, RULER_H - 6);
      g.lineTo(x, bottom);
      g.stroke();
      g.fillText(tickLabel(t - t0), x + 4, RULER_H - 13);
    }
    g.fillStyle = C.muted;
    g.fillText('session time', 0, RULER_H - 13);
  }

  drawLane(g, c, i) {
    const y = this.laneY(i);
    g.fillStyle = C.lane;
    g.fillRect(LABEL_W, y, this.plotW, LANE_H);
    g.textAlign = 'left';
    g.fillStyle = C.text;
    g.font = `700 12px ${FONT}`;
    g.fillText(ellipsize(g, c.name, LABEL_W - 12), 0, y + 9);
    g.font = `12px ${FONT}`;
    let sub = '';
    let col = C.muted;
    if (c.state === 'reference') { sub = 'reference clock'; col = C.green; }
    else if (c.ahead != null && (c.state === 'link' || c.state === 'search')) {
      sub = `${c.ahead > -0.005 ? '+' : '−'}${fmtSecs(Math.abs(c.ahead))} · ${c.mode === 'end' ? 'end' : 'start'}`;
      col = C.green;
    } else if (c.state === 'median clock' || c.state === 'manual offset') { sub = c.state === 'manual offset' ? 'clock from manual' : 'clock assumed'; col = C.orange; }
    else if (c.state === 'pending') { sub = 'clock ?'; col = C.rough; }
    else if (c.state === 'none') { sub = c.id ? 'no timestamps' : ''; }
    g.fillStyle = col;
    g.fillText(sub, 0, y + 24);
    g.font = `12px ${FONT}`;
  }

  drawClip(g, s, li, now, pxPerS) {
    const y = this.laneY(li) + 3;
    const hgt = LANE_H - 6;
    const x0 = this.x(s.x);
    const x1 = Math.max(x0 + 2, this.x(s.x + s.dur));
    if (x1 < LABEL_W - 40 || x0 > LABEL_W + this.plotW + 40) return;
    const lk = s.lk;
    // the ±W search window while the audio is being matched
    if (s.active && s.pos == null && s.pred != null && this.phase === 'align') {
      const wx0 = this.x(s.pred - s.w);
      const wx1 = this.x(s.pred + s.dur + s.w);
      g.fillStyle = rgba(C.rough, 0.07);
      g.fillRect(wx0, y - 2, wx1 - wx0, hgt + 4);
      g.fillStyle = rgba(C.rough, 0.6);
      g.fillRect(wx0, y - 2, 1, hgt + 4);
      g.fillRect(wx1 - 1, y - 2, 1, hgt + 4);
    }
    // motion streak
    const vpx = s.vx * pxPerS;
    if (Math.abs(vpx) > 40) {
      const len = Math.min(160, Math.abs(vpx) * 0.12);
      const tail = vpx > 0 ? x0 - len : x1 + len;
      const head = vpx > 0 ? x0 : x1;
      const grad = g.createLinearGradient(tail, 0, head, 0);
      grad.addColorStop(0, rgba(lk.placed ? C.green : C.rough, 0));
      grad.addColorStop(1, rgba(lk.placed ? C.green : C.rough, 0.35));
      g.fillStyle = grad;
      g.fillRect(Math.min(tail, head), y + hgt / 2 - 2, Math.abs(head - tail), 4);
    }
    let fill = lk.fill;
    let stroke = lk.stroke;
    if (s.active && !lk.placed) {
      const p = 0.5 + 0.5 * Math.sin(now / 160);
      fill = rgba(C.rough, 0.18 + 0.22 * p);
      stroke = C.rough;
    }
    if (this.hover && this.hover.id === s.id) fill = lk.placed ? rgba(C.green, 0.45) : rgba(C.rough, 0.4);
    g.fillStyle = fill;
    g.fillRect(x0, y, x1 - x0, hgt);
    g.strokeStyle = stroke;
    g.lineWidth = 1;
    g.setLineDash(lk.dash || []);
    g.strokeRect(Math.round(x0) + 0.5, y + 0.5, Math.max(1, Math.round(x1 - x0) - 1), hgt - 1);
    g.setLineDash([]);
    if (x1 - x0 > 70) {
      g.fillStyle = lk.placed ? C.white : rgba(C.rough, 0.95);
      g.textAlign = 'left';
      g.font = `700 12px ${FONT}`;
      g.fillText(ellipsize(g, s.name, x1 - x0 - 10), x0 + 5, y + hgt / 2 + 0.5);
      g.font = `12px ${FONT}`;
    }
    // it clicked: a green flash that opens up and fades
    if (s.flash) {
      const t = (now - s.flash) / FLASH_MS;
      if (t < 1) {
        const grow = 2 + 7 * t;
        g.strokeStyle = rgba(s.method === 'metadata' ? C.orange : C.green, 0.9 * (1 - t));
        g.lineWidth = 2;
        g.strokeRect(x0 - grow, y - grow, x1 - x0 + 2 * grow, hgt + 2 * grow);
        g.fillStyle = rgba(s.method === 'metadata' ? C.orange : C.green, 0.35 * (1 - t));
        g.fillRect(x0, y, x1 - x0, hgt);
        g.lineWidth = 1;
      } else s.flash = 0;
    }
  }

  drawParked(g, parked, now) {
    const x0 = LABEL_W + this.plotW + 12;
    const w = PARK_W - 16;
    if (!parked.length) return;
    g.fillStyle = C.muted;
    g.textAlign = 'left';
    g.font = `12px ${FONT}`;
    g.fillText('no timestamp', x0, RULER_H - 13);
    const perLane = new Map();
    for (const [s, li] of parked) {
      const n = perLane.get(li) || 0;
      perLane.set(li, n + 1);
      const y = this.laneY(li) + 3 + n * 3;
      const p = s.active ? 0.5 + 0.5 * Math.sin(now / 160) : 0;
      g.fillStyle = rgba(C.rough, 0.08 + 0.2 * p);
      g.fillRect(x0, y, w, LANE_H - 6 - n * 3);
      g.setLineDash([3, 3]);
      g.strokeStyle = rgba(C.rough, 0.6);
      g.strokeRect(x0 + 0.5, y + 0.5, w - 1, LANE_H - 7 - n * 3);
      g.setLineDash([]);
    }
    g.font = `12px ${FONT}`;
  }

  drawVotes(g, now) {
    const top = RULER_H + this.cams.length * (LANE_H + LANE_GAP) + PAD;
    const sx0 = VLABEL_W;
    const sw = Math.max(120, Math.min(this.w - VLABEL_W - 200, 560));
    const X = (d) => sx0 + ((d + VOTE_SPAN) / (2 * VOTE_SPAN)) * sw;
    g.fillStyle = C.muted;
    g.font = `12px ${FONT}`;
    g.textAlign = 'left';
    g.fillText(`clock votes — implied clock difference per match, around the winning cluster (−${VOTE_SPAN} s … 0 … +${VOTE_SPAN} s)`, 0, top + 8);
    this.pairs.forEach((p, i) => {
      const y = top + VOTE_HEAD + i * VOTE_H;
      const cy = y + VOTE_H / 2;
      const col = p.ok === true ? C.green : p.ok === false ? C.dim : C.rough;
      g.textAlign = 'left';
      g.fillStyle = p.ok === false ? C.dim : C.text;
      g.fillText(ellipsize(g, p.name, VLABEL_W - 16), 0, cy);
      g.fillStyle = C.lane;
      g.fillRect(sx0, y + 2, sw, VOTE_H - 4);
      g.fillStyle = C.band;
      g.fillRect(X(-1), y + 2, X(1) - X(-1), VOTE_H - 4);
      g.fillStyle = rgba(col, 0.5);
      g.fillRect(Math.round(X(0)), y + 2, 1, VOTE_H - 4);
      const seen = this.voteSeen.get(`${p.a}|${p.b}`) || { at: [] };
      let left = 0;
      let right = 0;
      p.votes.forEach((v, j) => {
        const d = p.center == null ? 0 : v - p.center;
        if (d < -VOTE_SPAN) { left += 1; return; }
        if (d > VOTE_SPAN) { right += 1; return; }
        const age = (now - (seen.at[j] || 0)) / 500;
        const r = age < 1 ? 4 + 3 * (1 - age) : 4;     // half the side of a square mark (8 px at rest)
        g.fillStyle = Math.abs(d) <= 1 ? col : rgba(C.text, 0.45);
        g.fillRect(Math.round(X(d) - r), Math.round(cy + ((j * 7) % 9) - 4 - r), 2 * r, 2 * r);
      });
      g.font = `12px ${FONT}`;
      g.fillStyle = C.muted;
      g.textAlign = 'left';
      if (left) g.fillText(`◂${left}`, sx0 + 4, cy);
      if (right) { g.textAlign = 'right'; g.fillText(`${right}▸`, sx0 + sw - 4, cy); g.textAlign = 'left'; }
      const tail = `${p.support}/${p.votes.length} agree${p.center != null ? ` · ${p.center > -0.005 ? '+' : '−'}${fmtSecs(Math.abs(p.center))}` : ''}${left + right ? ` · ${left + right} far off` : ''}`;
      g.fillStyle = p.ok === false ? C.dim : col;
      g.fillText(tail, sx0 + sw + 8, cy);
    });
    g.font = `12px ${FONT}`;
  }

  drawHover(g, t0) {
    const s = this.clips.get(this.hover.id);
    if (!s || s.x == null) return;
    const state = s.pos != null ? (s.method || 'placed') : s.pred != null ? 'rough: camera clock' : s.raw != null ? 'rough: raw timestamp' : 'no timestamp';
    const at = tickLabel(s.x - t0, true);
    const text = `${s.name} · ${at} · ${state}`;
    g.font = `12px ${FONT}`;
    const tw = g.measureText(text).width + 14;
    let x = this.hover.mx + 12;
    if (x + tw > this.w) x = this.hover.mx - tw - 12;
    const y = Math.max(2, this.hover.my - 26);
    g.fillStyle = 'rgba(13,16,20,0.94)';
    g.fillRect(x, y, tw, 20);
    g.strokeStyle = C.rule;
    g.strokeRect(x + 0.5, y + 0.5, tw - 1, 19);
    g.fillStyle = C.white;
    g.textAlign = 'left';
    g.fillText(text, x + 7, y + 10.5);
  }

  onMove(e) {
    const r = this.canvas.getBoundingClientRect();
    const mx = e.clientX - r.left;
    const my = e.clientY - r.top;
    let hit = null;
    if (this.view) {
      this.cams.forEach((c, i) => {
        const y = this.laneY(i);
        if (my < y || my > y + LANE_H) return;
        for (const s of this.clips.values()) {
          if ((s.cam ?? null) !== c.id || s.x == null) continue;
          const x0 = this.x(s.x);
          const x1 = Math.max(x0 + 3, this.x(s.x + s.dur));
          if (mx >= x0 - 2 && mx <= x1 + 2) hit = s;
        }
      });
    }
    const was = this.hover && this.hover.id;
    this.hover = hit ? { id: hit.id, mx, my } : null;
    if (hit || was) this.kick();
  }
}

function fmtSecs(s) {
  if (s < 60) return `${s.toFixed(s < 10 ? 2 : 1)} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}:${pad2(Math.floor(s % 60))} min`;
  return `${Math.floor(m / 60)}:${pad2(m % 60)}:${pad2(Math.floor(s % 60))} h`;
}

function ellipsize(g, text, w) {
  if (g.measureText(text).width <= w) return text;
  let lo = 0;
  let hi = text.length;
  while (lo < hi) {
    const mid = (lo + hi + 1) >> 1;
    if (g.measureText(text.slice(0, mid) + '…').width <= w) lo = mid; else hi = mid - 1;
  }
  return text.slice(0, lo) + '…';
}

/** A snapshot-shaped picture of a finished sync, rebuilt from the session data. */
export function snapshotFromSession(data, files) {
  const sync = data.sync || {};
  const camsInfo = sync.cameras || {};
  const frame = typeof sync.frame === 'number' ? sync.frame : null;
  const cams = data.cameras.map((c) => {
    const k = camsInfo[c.id];
    if (!k) return { id: c.id, name: c.name, state: 'none' };
    return { id: c.id, name: c.name, state: k.via, ahead: -k.offset, mode: k.mode, basis: k.mode_basis };
  });
  const clips = files.map((f) => {
    const k = camsInfo[f.camera];
    const ct = f.probe.creation_time;
    const dur = f.probe.duration || 0;
    const pred = frame != null && k && typeof k.k === 'number' && ct != null ? ct - (k.mode === 'end' ? dur : 0) + k.k : null;
    return {
      id: f.id, cam: f.camera, name: f.rel.split('/').pop(), dur,
      raw: frame != null ? ct : null, pred, w: k ? k.window : 1.5,
      pos: f.offset == null ? null : f.offset - (frame || 0), method: f.offset == null ? 'none' : (f.sync?.method || 'none'),
      active: false, kept: f.sync?.method === 'manual',
    };
  });
  const name = (id) => data.cameras.find((c) => c.id === id)?.name || id;
  const pairs = (sync.pairs || []).map((p) => ({ ...p, name: `${name(p.a)} ↔ ${p.b ? name(p.b) : 'placed cameras'}` }));
  return { phase: data.state.synced_at ? 'done' : 'idle', cams, clips, pairs, frame };
}

/** Replays a finished sync from its snapshot: raw timestamps → camera clocks → audio clicks. */
export function replayFrames(snap) {
  const frames = [];
  const at = (t, s) => frames.push([t, s]);
  const strip = (c, keep) => ({ ...c, pos: keep.pos ? c.pos : null, pred: keep.pred ? c.pred : null, method: keep.pos ? c.method : null, active: false });
  const camOrder = snap.cams.map((c) => c.id);
  const pending = snap.cams.map((c) => (c.state === 'none' ? c : { ...c, state: 'pending', ahead: null }));
  at(0, { ...snap, phase: 'audio', cams: pending, pairs: [], clips: snap.clips.map((c) => strip(c, {})) });
  let t = 900;
  const known = new Set();
  camOrder.forEach((id) => {
    known.add(id);
    const cams = snap.cams.map((c) => (known.has(c.id) || c.state === 'none' ? c : pending.find((p) => p.id === c.id)));
    at(t, { ...snap, phase: 'links', cams, pairs: snap.pairs, clips: snap.clips.map((c) => strip(c, { pred: known.has(c.cam) })) });
    t += 380;
  });
  t += 500;
  const order = snap.clips.filter((c) => c.pos != null).sort((a, b) => a.pos - b.pos);
  const landed = new Set();
  const step = Math.max(25, Math.min(120, 4000 / Math.max(1, order.length)));
  const activeAll = (c) => ({ ...strip(c, { pred: true }), active: c.pred != null });
  at(t, { ...snap, phase: 'align', clips: snap.clips.map(activeAll) });
  t += 400;
  for (let i = 0; i < order.length; i += 1) {
    landed.add(order[i].id);
    if (i % 2 === 1 || i === order.length - 1) {
      const set = new Set(landed);
      at(t, { ...snap, phase: 'align', clips: snap.clips.map((c) => (set.has(c.id) ? c : activeAll(c))) });
      t += step * 2;
    }
  }
  at(t + 200, snap);
  return frames;
}
