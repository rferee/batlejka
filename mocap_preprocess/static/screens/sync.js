// Sync screen.
import { SyncViz, snapshotFromSession, replayFrames } from '../syncviz.js';
import {
  GET, METHOD_COLORS, S, activeJob, basename, cameraById, cancelJob,
  fmtOffset, h, icon, isActive, jobLogs, lastJob,
  pad2, pollSoon, startJob, tip, videoFiles,
  METHOD_LABEL,
} from '../app.js';

// ------------------------------------------------------------------ Sync screen
const SYNC_PHASES = [
  ['audio', 'timestamps', 'Loads each clip\'s audio and its creation_time stamp.'],
  ['links', 'camera clocks', 'Matches audio between cameras to learn how far each camera\'s clock is off from the reference.'],
  ['align', 'audio within ±W', 'Matches every clip\'s audio only within ±W of where its corrected camera clock puts it.'],
  ['search', 'full search', 'Clips whose camera clock couldn\'t be linked are matched against the whole session instead.'],
  ['check', 'checks', 'Flags clips that overlap another clip of the same camera, or sit outside ±W of their camera clock.'],
];
/** What each placement method means; shared with the Cut Takes legend. */
export const METHOD_TIP = {
  reference: 'The one clip every other clip is matched against; its position anchors the timeline.',
  audio: 'Placed by audio: its sound matched other clips within ±W of where its camera clock puts it.',
  search: 'Its camera clock wasn\'t usable, so its audio was matched across the whole session. Usually right, but check it on the timeline.',
  metadata: 'No clear audio match, so it sits where its camera clock puts it. Check it on the timeline.',
  none: 'No audio match and no usable clock, so it has no position yet. Set its offset by hand.',
  manual: 'You set this offset by hand. Sync leaves it alone unless you tick re-sync manual files.',
};
// Offsets list: placements worth a look get their offset coloured, with this in plain words
const OFFSET_TIP = {
  search: 'Placed by full search only; verify on the timeline.',
  metadata: 'Placed by clock only; verify on the timeline.',
  manual: 'Set by hand; Sync keeps it.',
  none: 'Unplaced: no audio match and no usable camera clock.',
};
const ROUGH = '#8880F0';   // clips the audio hasn't placed yet (syncviz draws them blue too)
const mark = (color) => h('span', { class: 'sync-mark', style: { background: color } });
const OFFSETS_SHOWN = 8;   // per camera; the rest behind "+ N more"

function fmtAhead(s) {
  if (s == null) return '—';
  const a = Math.abs(s);
  const sign = s >= 0 ? '+' : '−';
  if (a < 60) return `${sign}${a.toFixed(2)} s`;
  if (a < 3600) return `${sign}${Math.floor(a / 60)}:${pad2(Math.floor(a % 60))} min`;
  return `${sign}${Math.floor(a / 3600)}:${pad2(Math.floor(a / 60) % 60)}:${pad2(Math.floor(a % 60))} h`;
}
export const SyncScreen = {
  resyncManual: false,
  mount(root) {
    this.root = h('main', { class: 'page' });
    root.append(this.root);
    this.vizBox = h('div', { class: 'viz-canvas' });
    this.viz = new SyncViz(this.vizBox);
    this.shown = false;
    this.render();
  },
  unmount() {
    clearTimeout(this.liveTimer);
    this.liveTimer = null;
    this.stopReplay();
    if (this.viz) this.viz.destroy();
    this.viz = null;
  },
  update() { this.render(); },
  async onJobs() {
    if (activeJob('sync')) this.startLive();
    await this.fetchLog();
    this.renderLeftDynamic();
  },
  job() { return activeJob('sync') || lastJob('sync'); },
  async fetchLog() {
    const j = this.job();
    if (!j) return;
    const have = jobLogs.get(j.id) || { seq: 0, lines: [] };
    if (have.seq >= j.log_seq) return;
    try {
      const full = await GET(`/api/jobs/${j.id}?log_since=${have.seq}`);
      have.lines.push(...full.log);
      have.seq = full.log_seq;
      have.status = full.status;
      jobLogs.set(j.id, have);
    } catch { /* pruned */ }
  },
  // the live picture: the job publishes a snapshot, polled faster than the job list
  startLive() {
    if (this.liveTimer || !this.viz) return;
    this.stopReplay();
    const tick = async () => {
      const j = activeJob('sync');
      if (!j || !this.viz) { this.liveTimer = null; return; }
      const have = jobLogs.get(j.id) || { seq: 0, lines: [] };
      try {
        const full = await GET(`/api/jobs/${j.id}?log_since=${have.seq}&live=true`);
        have.lines.push(...full.log);
        have.seq = full.log_seq;
        have.status = full.status;
        jobLogs.set(j.id, have);
        Object.assign(j, { progress: full.progress, message: full.message, log_seq: full.log_seq });
        if (full.live && this.viz) {
          this.lastLive = full.live;
          this.viz.setData(full.live);
        }
        this.renderLeftDynamic();
        if (!isActive(full)) { this.liveTimer = null; pollSoon(); return; }
      } catch { /* pruned */ }
      this.liveTimer = setTimeout(tick, 300);
    };
    this.liveTimer = setTimeout(tick, 0);
  },
  replay() {
    this.stopReplay();
    const frames = replayFrames(snapshotFromSession(S.data, videoFiles()));
    this.replaying = true;
    this.timers = frames.map(([t, snap]) => setTimeout(() => {
      this.replayPhase = snap.phase;
      if (this.viz) this.viz.setData(snap);
      this.renderVizHead();
    }, t));
    this.timers.push(setTimeout(() => { this.replaying = false; this.renderVizHead(); }, frames[frames.length - 1][0] + 60));
    this.renderVizHead();
  },
  stopReplay() {
    for (const t of this.timers || []) clearTimeout(t);
    this.timers = [];
    this.replaying = false;
  },
  render() {
    const r = this.root;
    r.innerHTML = '';
    if (!S.data) return;
    const vids = videoFiles();
    const st = S.data.settings;
    const manual = vids.filter((f) => f.sync?.method === 'manual').length;
    this.vizHead = h('div', { class: 'viz-head' });
    this.progress = h('div', { class: 'viz-progress' });
    this.log = h('div', { class: 'sync-log' });
    r.append(
      // two columns like the Cameras header: what syncing does on the left, running it on the right
      h('div', { class: 'head-row sync-top' },
        h('div', { class: 'page-head' },
          h('h1', null, 'Syncing'),
          h('p', null, 'Sync all the videos against each other with the camera clock and audio correlation.')),
        this.statusHead(vids, manual)),
      h('section', { class: 'card viz-card', 'aria-label': 'Live sync view' }, this.vizHead, this.progress, this.vizBox),
      h('div', { class: 'section-rule' }, h('span', null, 'details')),
      h('div', { class: 'sync-details' },
        h('section', { class: 'card sync-logcard', 'aria-label': 'Log' }, h('h2', { class: 'card-title' }, 'Log'), this.log),
        h('div', { class: 'sync-side' }, this.clocksCard(), this.offsetsCard(vids))));
    if (!activeJob('sync') && !this.replaying && this.viz) {
      this.viz.setData(snapshotFromSession(S.data, vids), { instant: !this.shown });
      this.shown = true;
    }
    if (activeJob('sync')) this.startLive();
    this.renderLeftDynamic();
    this.fetchLog().then(() => this.renderLeftDynamic());
  },
  renderVizHead() {
    if (!this.vizHead || !this.vizHead.isConnected) return;
    const active = activeJob('sync');
    let phase = S.data.state.synced_at ? 'done' : 'idle';
    if (this.replaying) phase = this.replayPhase || 'audio';
    else if (active) phase = this.lastLive?.phase || 'audio';
    const idx = SYNC_PHASES.findIndex(([id]) => id === phase);
    const n = this.viz ? this.viz.counts() : {};
    this.vizHead.innerHTML = '';
    this.vizHead.append(
      h('div', { class: 'vsteps' }, SYNC_PHASES.map(([id, label, desc], i) => {
        const done = phase === 'done' || (idx >= 0 && i < idx);
        return h('span', { class: `vstep${done ? ' done' : ''}${i === idx ? ' on' : ''}` },
          h('b', null, done ? icon('check') : String(i + 1)), tip(label, desc));
      })),
      h('div', { class: 'vcounts' },
        h('span', null, mark('#97d47d'), `${n.clicked || 0} `,
          tip('synced', 'Clips the audio has placed: the reference clip, audio matches within ±W of their camera clock, and full-search matches.')),
        h('span', null, mark(ROUGH), `${n.rough || 0} `,
          tip('rough', 'Clips still at the spot their camera clock or raw timestamp suggests, waiting for an audio match.')),
        n.fallback ? h('span', null, mark(METHOD_COLORS.metadata), `${n.fallback} `,
          tip('by clock', 'Clips with no clear audio match, left where their camera clock puts them. Check these on the timeline.')) : null,
        n.manual ? h('span', null, mark(METHOD_COLORS.manual), `${n.manual} `,
          tip('manual', 'Clips you placed by hand. Sync keeps them where they are unless you tick re-sync manual files.')) : null,
        n.none ? h('span', null, mark(METHOD_COLORS.none), `${n.none} `,
          tip('unplaced', 'Clips with neither an audio match nor a usable camera clock, so they have no position yet.')) : null,
        !active && S.data.sync?.frame != null
          ? h('button', { class: 'btn xs', title: 'Replay this sync', onclick: () => this.replay(), disabled: this.replaying }, icon('rotate-left'), 'Replay')
          : null));
  },
  renderLeftDynamic() {
    if (!this.log || !this.log.isConnected) return;
    const j = this.job();
    const active = activeJob('sync');
    this.runBtn.innerHTML = '';
    this.runBtn.append(...(active ? [icon('xmark'), 'Cancel'] : [icon('wave-square'), S.data.state.synced_at ? 'Sync again' : 'Run sync']));
    this.progress.innerHTML = '';
    if (j && active) {
      const pct = Math.round(j.progress * 100);
      this.progress.append(
        h('div', { class: 'progress-label' }, h('span', null, j.message || 'starting…'), h('span', null, `${pct}%`)),
        h('div', { class: 'progress' }, h('div', { style: { width: `${pct}%` } })));
    }
    this.renderVizHead();
    const lines = j ? (jobLogs.get(j.id)?.lines || []) : [];
    const stick = this.log.scrollTop + this.log.clientHeight >= this.log.scrollHeight - 8;
    this.log.innerHTML = '';
    if (!lines.length) {
      this.log.append(h('span', { class: 'dim' }, j ? '…' : '$ press Run sync — clocks first (timestamps + audio links between cameras), then every clip is matched by audio only within ±W of where its clock says it is.'));
    }
    for (const l of lines) {
      const cls = l.msg.startsWith('done') ? 'l-done' : `l-${l.level}`;
      this.log.append(h('span', { class: cls }, l.msg));
    }
    if (stick) this.log.scrollTop = this.log.scrollHeight;
  },
  async run() {
    const active = activeJob('sync');
    if (active) { cancelJob(active.id); return; }
    this.stopReplay();
    this.lastLive = null;
    await startJob('/sync', { resync_manual: this.resyncManual });
    this.startLive();
    this.renderLeftDynamic();
  },
  clocksCard() {
    const sync = S.data.sync || {};
    const info = sync.cameras || {};
    const cams = S.data.cameras.filter((c) => info[c.id]);
    if (!cams.length) return null;
    const viaLabel = { reference: 'reference', link: 'audio links', search: 'full search', 'median clock': 'assumed (median clock)', 'manual offset': 'your manual offset' };
    const th = (label, desc) => h('th', null, tip(label, desc));
    return h('section', { class: 'card sync-clocks-card', 'aria-label': 'Camera clocks' },
      h('h2', { class: 'card-title' }, 'Camera clocks'),
      h('table', { class: 'sync-clocks' },
        h('thead', null, h('tr', null,
          th('camera', 'The camera group. Hover a name to see how its clock was tied to the reference.'),
          th('vs reference', 'How far this camera\'s clock is ahead (+) or behind (−) the reference camera\'s clock.'),
          th('stamps', 'Whether this camera\'s creation_time marks the start or the end of a recording. A ? means it couldn\'t be measured (clips of equal length), so the nearest clock was assumed.'),
          th('agree', 'Audio links that agree on this camera\'s clock offset within ±1 s, out of all links found.'),
          th('spread', 'How much the agreeing audio links scatter around the clock offset (standard deviation). Small is good.'),
          th('±W', 'How far from its clock position each clip\'s audio is searched: the minimum window, or 3× the spread if larger.'))),
        h('tbody', null, cams.map((c) => {
          const k = info[c.id];
          const isRef = c.id === sync.reference_camera;
          const cls = !k.attached && k.via !== 'manual offset' ? 'warn' : '';
          return h('tr', null,
            h('td', { class: 'ellipsis', title: `${c.name} — ${viaLabel[k.via] || k.via}` }, c.name),
            h('td', { class: cls || (isRef ? 'ok' : ''), title: 'camera clock minus reference clock' }, isRef ? 'reference' : k.attached || k.via === 'manual offset' ? fmtAhead(-k.offset) : 'unknown'),
            h('td', { title: k.mode_basis === 'measured' ? 'told apart by the clock links' : 'not measurable (clips of equal length) — nearest clock assumed' }, `${k.mode}${k.mode_basis === 'measured' ? '' : '?'}`),
            h('td', null, k.votes ? `${k.support}/${k.votes}` : '—'),
            h('td', null, `${k.spread.toFixed(2)} s`),
            h('td', null, `${k.window.toFixed(1)} s`));
        }))));
  },
  offsetsCard(vids) {
    const card = h('section', { class: 'card sync-offsets', 'aria-label': 'Offsets' }, h('h2', { class: 'card-title' }, 'Offsets'));
    const groups = [...S.data.cameras.map((c) => [c, vids.filter((f) => f.camera === c.id)]),
      [{ id: '_', name: 'unassigned' }, vids.filter((f) => !cameraById(f.camera))]];
    this.offsetsOpen = this.offsetsOpen || {};
    for (const [cam, files] of groups) {
      if (!files.length) continue;
      const sorted = files.sort((a, b) => (a.offset ?? 1e12) - (b.offset ?? 1e12));
      const open = !!this.offsetsOpen[cam.id];
      const shown = open ? sorted : sorted.slice(0, OFFSETS_SHOWN);
      const hidden = sorted.slice(OFFSETS_SHOWN);
      const toCheck = hidden.filter((f) => (f.sync?.flags || []).length || !['audio', 'reference'].includes(f.offset == null ? 'none' : f.sync?.method)).length;
      const group = h('div', { class: 'offsets' }, h('span', { class: 'cam-h' }, cam.name),
        shown.map((f) => {
          const m = f.offset == null ? 'none' : (f.sync?.method || 'none');
          const flags = f.sync?.flags || [];
          const conf = f.sync?.score != null ? `${f.sync.score.toFixed(2)} · ${f.sync.ratio != null ? f.sync.ratio.toFixed(1) : '–'}×` : m === 'reference' ? 'ref' : m;
          const title = [`${f.rel} — ${METHOD_LABEL[m] || m}${f.sync?.via ? ` (${f.sync.via})` : ''}`, f.sync?.reason, ...flags].filter(Boolean).join('\n');
          // a plain audio match keeps the default colour; anything else is tinted by its method
          const val = f.offset != null ? fmtOffset(f.offset) : '—';
          return h('div', { class: 'offrow' },
            h('span', { class: 'ellipsis', title }, flags.length ? h('span', { class: 'warn' }, icon('triangle-exclamation'), ' ') : null, basename(f.rel)),
            h('span', { class: 'val' }, OFFSET_TIP[m] ? tip(val, OFFSET_TIP[m], { style: { color: METHOD_COLORS[m] } }) : val),
            h('span', { class: 'conf' }, conf));
        }));
      if (hidden.length) {
        group.append(h('button', {
          class: 'more-toggle', 'aria-expanded': String(open),
          onclick: () => { this.offsetsOpen[cam.id] = !open; this.rerenderOffsets(); },
        }, open ? 'Show fewer' : `+ ${hidden.length} more file${hidden.length === 1 ? '' : 's'}${toCheck ? ` · ${toCheck} to check` : ''}`,
        icon(open ? 'chevron-up' : 'chevron-down')));
      }
      card.append(group);
    }
    card.append(h('div', { class: 'legend' },
      Object.keys(OFFSET_TIP).map((k) => h('span', null, mark(METHOD_COLORS[k]), tip(METHOD_LABEL[k], METHOD_TIP[k])))));
    this.offsetsEl = card;
    return card;
  },
  rerenderOffsets() {
    if (!this.offsetsEl || !this.offsetsEl.isConnected) return;
    this.offsetsEl.replaceWith(this.offsetsCard(videoFiles()));
  },
  /** The status header (like Process's): where syncing stands, with the run button folded in. */
  statusHead(vids, manual) {
    this.runBtn = h('button', { class: 'btn primary sync-run-btn', onclick: () => this.run() });
    const toggle = manual && !activeJob('sync')
      ? h('div', { class: 'toggle', onclick: () => { this.resyncManual = !this.resyncManual; this.render(); } },
        h('span', { class: `check ${this.resyncManual ? 'on' : ''}` }, icon('check')), `re-sync ${manual} manual file(s) too`)
      : null;
    const [ico, text] = this.summary(vids);
    return h('div', { class: 'card sync-summary sync-head' }, ico, h('div', { class: 'sync-head-main' }, text, toggle), this.runBtn);
  },
  summary(vids) {
    if (activeJob('sync')) {
      return [icon('spinner', 'fa-spin muted'), h('span', { class: 'sync-summary-text' }, 'Syncing… ', h('span', { class: 'faint' }, 'clips settle into place below as their audio is matched.'))];
    }
    if (!S.data.state.synced_at) {
      return [icon('clock', 'muted'), h('span', { class: 'sync-summary-text' }, 'Not synced yet. ', h('span', { class: 'faint' }, 'Offsets are provisional (creation time); run sync to place clips by audio.'))];
    }
    const meta = vids.filter((f) => f.sync?.method === 'metadata');
    const search = vids.filter((f) => f.sync?.method === 'search');
    const none = vids.filter((f) => f.offset == null || f.sync?.method === 'none');
    const flagged = vids.filter((f) => (f.sync?.flags || []).length);
    const good = vids.length - meta.length - none.length - search.length;
    const allGood = !meta.length && !none.length && !search.length && !flagged.length;
    return [icon(allGood ? 'check' : 'triangle-exclamation', allGood ? 'ok' : 'warn'),
      h('span', { class: 'sync-summary-text' },
        allGood ? `All ${vids.length} clips aligned.` : `${good}/${vids.length} clips aligned by audio and clock.`,
        search.length ? h('span', { class: 'warn' }, ` ${search.length} placed by full search only, verify on the timeline.`) : null,
        meta.length ? h('span', { class: 'warn' }, ` ${meta.length} placed by clock only, verify on the timeline.`) : null,
        flagged.length ? h('span', { class: 'warn' }, ` ${flagged.length} flagged (see Offsets).`) : null,
        none.length ? h('span', { class: 'err' }, ` ${none.length} unplaced.`) : null)];
  },
};
