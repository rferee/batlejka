// Multi-camera preview grid: one proxy <video> per camera, rotation by CSS only
// (the files are rotated when exported), throttled seeks and a drift-corrected playback.

/** Fit a dw×dh media element into a cw×ch box after rotating it by `rot`
 * degrees (CSS, around its centre). Returns the element's unrotated box. */
function fitRotated(cw, ch, dw, dh, rot) {
  if (!cw || !ch || !dw || !dh) return null;
  const quarter = rot === 90 || rot === 270;
  const rw = quarter ? dh : dw;
  const rh = quarter ? dw : dh;
  const s = Math.min(cw / rw, ch / rh);
  const w = dw * s;
  const h = dh * s;
  return { width: w, height: h, left: (cw - w) / 2, top: (ch - h) / 2 };
}

export function applyFit(el, box, dw, dh, rot) {
  const fit = fitRotated(box.clientWidth, box.clientHeight, dw, dh, rot);
  if (!fit) return;
  el.style.width = `${fit.width}px`;
  el.style.height = `${fit.height}px`;
  el.style.left = `${fit.left}px`;
  el.style.top = `${fit.top}px`;
  el.style.transform = rot ? `rotate(${rot}deg)` : '';
}

const DRIFT = 0.1; // seconds a playing video may drift from the master clock

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

/**
 * cameras: [{id, name, rotation}]
 * files(cameraId): [{id, rel, start, dur, fps, dw, dh, proxyUrl, thumbUrl, preview?, rotation?}]  (proxyUrl null while building)
 * A preview (FreeMoCap's annotated video of a take) wins over the files under it; it comes
 * from the exported take, so it's upright already (rotation 0).
 */
export class Grid {
  constructor(container, opts) {
    this.root = container;
    this.opts = opts;
    this.cells = [];
    this.playing = false;
    this.speed = 1;
    this.t = 0;
    this.audioCam = null;
    this.ro = new ResizeObserver(() => this.layout());
    this.ro.observe(container);
  }

  destroy() {
    this.ro.disconnect();
    for (const c of this.cells) {
      c.video.pause();
      c.video.removeAttribute('src');
      c.video.load();
    }
    this.root.innerHTML = '';
    this.cells = [];
  }

  setCameras(cams) {
    const key = cams.map((c) => c.id).join(',');
    if (key === this.key) {
      // same cells: only names/rotation may have changed
      cams.forEach((cam, i) => {
        const cell = this.cells[i];
        cell.cam = cam;
        cell.name.textContent = cam.name;
      });
      this.layout();
      return;
    }
    this.key = key;
    for (const c of this.cells) {
      c.video.pause();
      c.video.removeAttribute('src');
      c.video.load();
    }
    this.root.innerHTML = '';
    this.cells = [];
    const n = cams.length;
    const cols = n <= 6 ? Math.max(1, n) : Math.ceil(n / 2);
    this.root.style.gridTemplateColumns = `repeat(${cols}, minmax(0, 1fr))`;
    if (!n) {
      this.root.append(el('div', 'placeholder', 'No cameras included — tick some on the left.'));
      return;
    }
    for (const cam of cams) this.cells.push(this.makeCell(cam));
    this.show(this.t);
  }

  makeCell(cam) {
    const tile = el('div', 'tile');
    const video = document.createElement('video');
    video.muted = true;
    video.playsInline = true;
    video.preload = 'auto';
    video.disablePictureInPicture = true;
    video.hidden = true;
    const img = el('img', 'poster');
    img.alt = '';
    img.hidden = true;
    const name = el('span', 'lbl name', cam.name);
    const audio = el('span', 'lbl audio');
    audio.innerHTML = '<i class="fa-solid fa-volume-high"></i>';
    audio.hidden = true;
    const file = el('span', 'lbl file ellipsis', '—');
    const frame = el('span', 'lbl frame', '');
    const nofoot = el('div', 'nofoot', 'no footage');
    const building = el('div', 'building', 'proxy building…');
    building.hidden = true;
    tile.append(video, img, nofoot, building, name, audio, file, frame);
    this.root.append(tile);
    const cell = { cam, tile, video, img, name, audio, file, frame, nofoot, building, cur: null, ready: false, pending: null, mode: 'none' };
    // a click opens the frame on screen at full quality (opts.onTile)
    tile.addEventListener('click', () => {
      if (cell.cur && this.opts.onTile) this.opts.onTile(cell.cam, cell.cur, this.t - cell.cur.start);
    });
    video.addEventListener('loadedmetadata', () => {
      cell.ready = true;
      cell.vw = video.videoWidth;
      cell.vh = video.videoHeight;
      this.fitCell(cell);
      const want = cell.pending;
      cell.pending = null;
      if (want != null) this.seek(cell, want);
      if (this.playing) video.play().catch(() => {});
    });
    video.addEventListener('seeked', () => {
      if (cell.pending != null) {
        const p = cell.pending;
        cell.pending = null;
        this.seek(cell, p);
      }
    });
    video.addEventListener('error', () => { cell.ready = false; });
    return cell;
  }

  fitCell(cell) {
    const f = cell.cur;
    if (!f) return;
    const dw = (cell.mode === 'video' && cell.vw) || f.dw;
    const dh = (cell.mode === 'video' && cell.vh) || f.dh;
    const media = cell.mode === 'video' ? cell.video : cell.img;
    applyFit(media, cell.tile, dw, dh, f.rotation ?? (cell.cam.rotation || 0));
  }

  layout() {
    for (const c of this.cells) this.fitCell(c);
  }

  covering(cam, t) {
    let best = null;
    for (const f of this.opts.files(cam.id)) {
      if (t >= f.start && t < f.start + f.dur && (!best || !!f.preview > !!best.preview
        || (!!f.preview === !!best.preview && f.start > best.start))) best = f;
    }
    return best;
  }

  /** Throttled seek: while a seek is in flight only the latest target is kept. */
  seek(cell, ft) {
    const v = cell.video;
    if (!cell.ready) {
      cell.pending = ft;
      return;
    }
    if (isFinite(v.duration)) ft = Math.min(ft, Math.max(0, v.duration - 0.001));
    ft = Math.max(0, ft);
    if (v.seeking) {
      cell.pending = ft;
      return;
    }
    if (Math.abs(v.currentTime - ft) < 0.002) return;
    v.currentTime = ft;
  }

  setAudio(camId) {
    this.audioCam = camId;
    for (const c of this.cells) {
      c.video.muted = c.cam.id !== camId;
      c.audio.hidden = c.cam.id !== camId;
    }
  }

  /** Point every cell at session time t (switching sources as needed). */
  show(t) {
    this.t = t;
    for (const cell of this.cells) this.showCell(cell, t);
  }

  showCell(cell, t) {
    const f = this.covering(cell.cam, t);
    const want = !f ? 'none' : f.proxyUrl ? 'video' : 'thumb';
    if (!f || f.id !== (cell.cur && cell.cur.id) || want !== cell.mode || (f && want === 'video' && f.proxyUrl !== cell.src)) {
      this.switchCell(cell, f, want);
    }
    if (!f) return;
    const ft = t - f.start;
    if (cell.mode === 'video') {
      const v = cell.video;
      if (this.playing) {
        if (cell.ready && !v.seeking && Math.abs(v.currentTime - ft) > DRIFT * Math.max(1, this.speed)) this.seek(cell, ft);
        if (cell.ready && v.paused && !v.seeking && ft < (v.duration || Infinity)) v.play().catch(() => {});
      } else {
        this.seek(cell, ft);
      }
    }
    cell.frame.textContent = `f${Math.max(0, Math.floor(ft * (f.fps || 30) + 1e-6))}`;
  }

  switchCell(cell, f, mode) {
    const v = cell.video;
    cell.cur = f;
    cell.mode = mode;
    cell.nofoot.hidden = !!f;
    cell.tile.classList.toggle('has-frame', !!(f && this.opts.onTile));
    cell.building.hidden = mode !== 'thumb';
    cell.file.textContent = f ? f.rel.split('/').pop() : '—';
    cell.file.title = f ? f.rel : '';
    if (!f) cell.frame.textContent = '';
    if (mode === 'video') {
      cell.img.hidden = true;
      v.hidden = false;
      if (cell.src !== f.proxyUrl) {
        cell.ready = false;
        cell.vw = cell.vh = 0;
        cell.src = f.proxyUrl;
        v.src = f.proxyUrl;
        v.playbackRate = this.speed;
        v.muted = cell.cam.id !== this.audioCam;
      }
    } else {
      if (!v.paused) v.pause();
      v.hidden = true;
      if (mode === 'thumb') {
        cell.img.hidden = false;
        if (cell.img.dataset.src !== f.thumbUrl) {
          cell.img.dataset.src = f.thumbUrl;
          cell.img.src = f.thumbUrl;
        }
      } else {
        cell.img.hidden = true;
      }
    }
    this.fitCell(cell);
  }

  play(t, speed) {
    this.playing = true;
    this.speed = speed;
    for (const cell of this.cells) {
      cell.video.playbackRate = speed;
      this.showCell(cell, t);
      if (cell.mode === 'video' && cell.ready) {
        this.seek(cell, t - cell.cur.start);
        cell.video.play().catch(() => {});
      }
    }
  }

  tick(t) {
    this.t = t;
    for (const cell of this.cells) this.showCell(cell, t);
  }

  pause(t) {
    this.playing = false;
    for (const cell of this.cells) if (!cell.video.paused) cell.video.pause();
    this.show(t);
  }

  setSpeed(speed) {
    this.speed = speed;
    for (const cell of this.cells) cell.video.playbackRate = speed;
  }
}
