// Process screen.
import { gpuCap } from '../setup.js';
import {
  PUT, S, activeJob, applySession, basename, cameraById, fileById, fmtDate,
  fmtDur, fpsFloat, fpsLabel, h, icon, render,
  sUrl, startJob, tip, toast, videoFiles,
} from '../app.js';

// ------------------------------------------------------------------ Process screen
const SAMPLE_RATES = [8000, 16000, 22050, 44100, 48000];
const PROXY_SIZES = [480, 640, 960, 1280];
const SHOWN = 16;   // file rows before "+ N more files"

async function putSettings(patch) {
  try {
    applySession(await PUT(sUrl('/settings'), patch));
  } catch (e) { toast(e.message, 'error'); render(); }
}

const makeOf = (p) => [p?.make, p?.model].filter(Boolean).join(' ');
// file table columns: [key, header, sort value or null (not sortable), description (unsortable ones)]
const COLUMNS = [
  ['rel', 'source/', (f) => f.rel.toLowerCase()],
  ['camera', 'camera', (f) => (cameraById(f.camera)?.name || '￿').toLowerCase()],
  ['length', 'length', (f) => f.probe?.duration ?? -1],
  ['res', 'resolution', (f) => (f.probe?.video ? f.probe.video.display_width * f.probe.video.display_height : -1)],
  ['codec', 'codec', (f) => f.probe?.video?.codec || ''],
  ['fps', 'fps (avg)', (f) => (f.probe?.video ? fpsFloat(f.probe.video.avg_fps) || 0 : -1)],
  ['vfr', 'vfr', null, 'Variable frame rate suspected: the file\'s average frame rate differs from its nominal rate.'],
  ['audio', 'audio', (f) => (f.probe?.audio ? `${f.probe.audio.codec} ${String(f.probe.audio.sample_rate || 0).padStart(6, '0')}` : '')],
  ['rot', 'rot', (f) => f.probe?.video?.rotation_meta || 0],
  ['created', 'created', (f) => f.probe?.creation_time ?? -1],
  ['make', 'make / model', (f) => makeOf(f.probe).toLowerCase() || '￿'],
  ['assets', 'assets', null, 'What processing has made for each file: T thumbnail, A sync audio, W waveform, P scrub proxy.'],
];

export const ProcessScreen = {
  advOpen: false,
  customFps: false,
  sortKey: 'rel',
  sortDir: 'asc',
  showAll: false,
  mount(root) {
    this.root = h('main', { class: 'page' });
    this.left = h('div', { class: 'proc-left' });
    this.right = h('div', { class: 'proc-right' });
    this.root.append(h('div', { class: 'proc-split' }, this.left, this.right));
    root.append(this.root);
    this.update();
  },
  sessionChanged() { this.showAll = false; },
  onJobs() { this.renderActions(); },
  update() {
    if (!S.data) {
      this.left.innerHTML = '';
      this.right.innerHTML = '';
      this.left.append(h('div', { class: 'empty' }, 'Pick a session first.'));
      return;
    }
    if (!this.left.contains(document.activeElement) || !this.form) this.renderLeft();
    else this.renderActions();
    this.renderRight();
  },
  renderLeft() {
    const st = S.data.settings;
    const L = this.left;
    L.innerHTML = '';
    L.append(h('div', { class: 'page-head' },
      h('h1', null, 'Process Videos'),
      h('p', null, 'Creates thumbnails and proxies, and extracts audio tracks for sync.')));

    const vids = videoFiles();
    const atTarget = vids.filter((f) => f.probe.video.nominal_fps === st.target_fps).length;
    const std = S.data.standard_rates.map((r) => r.value);
    const custom = this.customFps || !std.includes(st.target_fps);
    const fpsSel = h('select', {
      class: 'input',
      onchange: (e) => {
        if (e.target.value === 'custom') { this.customFps = true; this.renderLeft(); return; }
        this.customFps = false;
        putSettings({ target_fps: e.target.value });
      },
    },
    S.data.standard_rates.map((r) => h('option', { value: r.value, selected: !custom && r.value === st.target_fps }, `${r.label} fps  (${r.value})`)),
    h('option', { value: 'custom', selected: custom }, 'custom…'));
    const fpsCustom = custom ? h('input', {
      class: 'input', value: st.target_fps, placeholder: 'e.g. 30000/1001',
      onchange: (e) => putSettings({ target_fps: e.target.value }),
    }) : null;

    const form = h('div', { class: 'card' },
      h('label', { class: 'field' },
        tip('Target framerate', 'Every exported take is conformed to this rate; ffmpeg\'s fps filter drops or duplicates frames of files at other rates. Picked from the files\' metadata unless you set it.'),
        fpsSel, fpsCustom,
        h('span', { class: 'note' },
          `metadata: ${atTarget}/${vids.length} files @ ${fpsLabel(st.target_fps)}`,
          st.target_fps_user_set ? ' · set by you · ' : '',
          st.target_fps_user_set ? h('a', { href: '#', onclick: (e) => { e.preventDefault(); this.customFps = false; putSettings({ target_fps_user_set: false }); } }, 'reset') : null)),
      h('label', { class: 'field' },
        tip('Sync sample rate', 'The rate each clip\'s audio is resampled to for cross-correlation. 8 kHz is the default and plenty for sync; higher rates are slower.'),
        h('select', { class: 'input', onchange: (e) => putSettings({ sync_sample_rate: Number(e.target.value) }) },
          SAMPLE_RATES.map((r) => h('option', { value: r, selected: r === st.sync_sample_rate }, `${r} Hz`)))),
      h('label', { class: 'field' },
        tip('Scrub proxy size', 'Long side of the low-res copy made of each video for scrubbing and preview. Exports always read the original files.'),
        h('select', { class: 'input', onchange: (e) => putSettings({ proxy_long_side: Number(e.target.value) }) },
          PROXY_SIZES.map((r) => h('option', { value: r, selected: r === st.proxy_long_side }, `${r} px long side`)))),
      (() => {
        const dec = gpuCap('decode');
        const off = dec && !dec.ok;    // the Setup panel's test failed: it would only fall back to the CPU
        return h('div', {
          class: `toggle ${off ? 'disabled' : ''}`, title: off ? `GPU decode is unavailable: ${dec.error}` : '',
          onclick: () => { if (!off || st.hwaccel) putSettings({ hwaccel: !st.hwaccel }); },
        }, h('span', { class: `check ${st.hwaccel ? 'on' : ''}` }, icon('check')),
        tip('GPU decode (-hwaccel cuda) for proxies and export', 'Decodes the source videos on the NVIDIA GPU while making proxies and exporting. A file that fails on the GPU is retried on the CPU.'),
        off ? h('span', { class: 'note' }, '(unavailable on this machine)') : null);
      })(),
      h('button', { class: 'adv-toggle', onclick: () => { this.advOpen = !this.advOpen; this.renderLeft(); } },
        icon(this.advOpen ? 'chevron-down' : 'chevron-right'), 'Advanced sync thresholds'),
      this.advOpen ? [
        h('label', { class: 'field' }, tip('Min score', 'Lowest normalized correlation peak (0–1) that counts as an audio match. Inside the ±W window a quarter of it is enough; the ratio does the work.'),
          h('input', { class: 'input', type: 'number', step: '0.01', min: '0', max: '1', value: st.sync_min_score, onchange: (e) => putSettings({ sync_min_score: Number(e.target.value) }) })),
        h('label', { class: 'field' }, tip('Min ratio', 'How many times higher the best correlation peak must be than the best other peak more than 0.1 s away. Guards against repeating sounds such as looping music.'),
          h('input', { class: 'input', type: 'number', step: '0.1', min: '1', value: st.sync_min_ratio, onchange: (e) => putSettings({ sync_min_ratio: Number(e.target.value) }) })),
        h('label', { class: 'field' }, tip('Window ±W (min, s)', 'Each clip is matched only within ±W of where its camera clock puts it, so looping music can\'t pull it a bar off. W is this value, or 3× the camera\'s clock spread if larger.'),
          h('input', { class: 'input', type: 'number', step: '0.1', min: '0.2', max: '30', value: st.sync_window ?? 1.5, onchange: (e) => putSettings({ sync_window: Number(e.target.value) }) }))] : null,
      this.processBtn = h('button', { class: 'btn primary big proc-run', onclick: () => startJob('/process') }));
    this.form = form;
    L.append(form);
    this.actions = h('div', { class: 'col', style: { gap: '12px' } });
    L.append(this.actions);
    this.renderActions();
  },
  renderActions() {
    if (!this.actions || !S.data) return;
    const A = this.actions;
    A.innerHTML = '';
    const scanning = activeJob('scan');
    const processing = activeJob('process');
    const proxies = activeJob('proxies');
    const n = videoFiles().length;
    if (this.processBtn) {   // lives in the settings card; its state follows the jobs
      this.processBtn.disabled = !!processing || !n;
      this.processBtn.replaceChildren(icon('gears'), S.data.state.processed_at ? 'Process again' : 'Process source files');
    }
    if (this.rescanBtn) this.rescanBtn.disabled = !!scanning;
    for (const j of [scanning, processing, proxies]) {
      if (!j) continue;
      A.append(h('div', { class: 'col', style: { gap: '6px' } },
        h('div', { class: 'progress-label' }, h('span', { class: 'ellipsis' }, `${j.label} · ${j.message || '…'}`), h('span', null, `${Math.round(j.progress * 100)}%`)),
        h('div', { class: 'progress' }, h('div', { style: { width: `${j.progress * 100}%` } }))));
    }
    if (!n && !scanning) A.append(h('p', { class: 'note' }, 'No video files found in source/ yet.'));
  },
  renderRight() {
    const R = this.right;
    const wrap = R.querySelector('.proc-tablewrap');
    const keepX = wrap ? wrap.scrollLeft : 0;   // re-rendering must not jump the table or the page back
    const keepY = this.root.scrollTop;
    R.innerHTML = '';
    const fc = S.data.fps_check;
    const signs = {
      ok: icon('check', 'proc-sign ok'), warn: icon('triangle-exclamation', 'proc-sign warn'),
      error: icon('circle-xmark', 'proc-sign err'), empty: icon('folder-open', 'proc-sign muted'),
    };
    const cams = S.data.cameras.length;
    R.append(h('div', { class: 'proc-banner' },
      activeJob('scan') ? icon('spinner', 'fa-spin proc-sign muted') : signs[fc.level],
      h('div', { class: 'lines' },
        h('span', { class: 'headline' }, fc.summary,
          cams ? h('span', null, ` — ${cams} camera${cams === 1 ? '' : 's'} detected`) : null,
          h('span', null, ` · target ${fc.target_label} fps`)),
        fc.items.map((it) => h('span', { class: 'item' },
          h('span', { class: 'mark', style: { background: it.level === 'error' ? '#E56767' : '#E89768' } }),
          h('span', null, h('b', null, it.title), ' — ', it.detail,
            h('span', { class: 'muted-60' }, ' · ', it.files.slice(0, 6).map((id) => basename(fileById(id)?.rel || id)).join(', '),
              it.files.length > 6 ? ` +${it.files.length - 6}` : ''))))),
      this.rescanBtn = h('button', { class: 'btn sm proc-rescan', disabled: !!activeJob('scan'), onclick: () => startJob('/scan') },
        icon('magnifying-glass'), 'Re-scan')));

    if (!S.data.files.length) {
      R.append(h('div', { class: 'empty' }, 'source/ is empty. Drop the raw clips there (subfolders per camera are fine) and hit Re-scan.'));
      return;
    }
    const col = COLUMNS.find((c) => c[0] === this.sortKey) || COLUMNS[0];
    const sign = this.sortDir === 'asc' ? 1 : -1;
    const files = [...S.data.files].sort((a, b) => {
      const x = col[2](a);
      const y = col[2](b);
      const c = x < y ? -1 : x > y ? 1 : 0;
      return c * sign || a.rel.localeCompare(b.rel);
    });
    const tgt = S.data.settings.target_fps;
    const shown = this.showAll ? files : files.slice(0, SHOWN);
    const rows = shown.map((f) => {
      const p = f.probe;
      const v = p && p.video;
      const cam = cameraById(f.camera);
      const make = makeOf(p);
      return h('tr', null,
        h('td', { class: 'nm', title: f.rel }, f.rel),
        h('td', null, cam ? cam.name : h('span', { class: 'muted-60' }, '—')),
        h('td', null, p ? fmtDur(p.duration) : '—'),
        h('td', null, v ? `${v.display_width}×${v.display_height}` : h('span', { class: 'tag err' }, 'no video')),
        h('td', null, v ? v.codec : '—'),
        h('td', null, v ? h('span', { class: v.nominal_fps === tgt ? '' : 'warn' }, fpsLabel(v.nominal_fps),
          h('span', { class: 'muted-60' }, ` (${fpsFloat(v.avg_fps).toFixed(3)})`)) : '—'),
        h('td', null, v && v.vfr_suspect ? h('span', { class: 'tag warn' }, 'VFR') : h('span', { class: 'muted-60' }, 'N/A')),
        h('td', null, p ? (p.audio ? `${p.audio.codec} ${Math.round((p.audio.sample_rate || 0) / 1000)}k` : h('span', { class: 'tag warn' }, 'none')) : '—'),
        h('td', null, v && v.rotation_meta ? `${v.rotation_meta}°` : h('span', { class: 'muted-60' }, '0°')),
        h('td', null, p ? fmtDate(p.creation_time) : '—'),
        h('td', { class: 'ellipsis', style: { maxWidth: '160px' }, title: make }, make || h('span', { class: 'muted-60' }, '—')),
        h('td', null, f.probe_error ? h('span', { class: 'tag err', title: f.probe_error }, 'probe failed') : h('span', { class: 'proc-assets' },
          h('span', { class: f.assets.thumb ? 'on' : '', title: 'thumbnail' }, 'T'),
          h('span', { class: f.assets.audio ? 'on' : '', title: 'sync audio' }, 'A'),
          h('span', { class: f.assets.wave ? 'on' : '', title: 'waveform' }, 'W'),
          h('span', { class: f.assets.proxy ? 'on' : '', title: 'scrub proxy' }, 'P'))));
    });
    const head = COLUMNS.map(([key, label, get, desc]) => {
      if (!get) return h('th', { scope: 'col' }, desc ? tip(label, desc) : label);
      const on = key === this.sortKey;
      const dir = on ? this.sortDir : null;
      return h('th', { scope: 'col', 'aria-sort': on ? (dir === 'asc' ? 'ascending' : 'descending') : 'none' },
        h('button', {
          type: 'button', class: `proc-sort ${on ? 'on' : ''}`, title: `Sort by ${label}`,
          onclick: () => {
            this.sortDir = on && dir === 'asc' ? 'desc' : 'asc';
            this.sortKey = key;
            this.renderRight();
          },
        }, label, icon(dir === 'asc' ? 'sort-up' : dir === 'desc' ? 'sort-down' : 'sort')));
    });
    const table = h('div', { class: 'proc-tablewrap' }, h('table', { class: 'ftable' },
      h('thead', null, h('tr', null, head)),
      h('tbody', null, rows)));
    R.append(table);
    table.scrollLeft = keepX;
    this.root.scrollTop = keepY;
    const hidden = files.length - SHOWN;
    if (hidden > 0) {
      R.append(h('button', {
        type: 'button', class: 'proc-more', 'aria-expanded': String(this.showAll),
        onclick: () => { this.showAll = !this.showAll; this.renderRight(); },
      }, this.showAll ? 'Show fewer' : `+ ${hidden} more file${hidden === 1 ? '' : 's'}`, icon(this.showAll ? 'chevron-up' : 'chevron-down')));
    }
  },
};
