// Calibrate screen: FreeMoCap calibration profiles. Each profile is one ChArUco board setup
// (calibration take, board, square size, ground plane) run with FreeMoCap 1.8.2; the active
// one is used for every take on the Mocap step unless a take picks another.
import {
  h, icon, tip, S, GET, api, sUrl, toast, confirmModal, activeJob, go, fmtDur,
} from '../app.js';
import {
  COLORS, camName, square, fmtStamp, boardText, errText, ERR_TIP, profileStatus, notReadyCard, postMocapJob,
} from '../mocap.js';

const BOARDS = ['7x5', '5x3'];
const TIPS = {
  name: 'A name for this board setup. It names the calibration file (output/calibrations/<name>.toml) and is what the Mocap step lists; spaces and symbols become dashes.',
  take: 'The exported take in which every camera filmed the ChArUco board. FreeMoCap finds the board in its videos to work out where each camera is and how its lens bends the image.',
  board: 'Which printed ChArUco board was filmed: FreeMoCap\'s 7x5 or 5x3 squares. It must match the printout, or the board isn\'t detected.',
  square: 'Edge length of one black square on the printed board, measured with a ruler. It sets the scale: every 3D output is in millimetres because of it, so a wrong value scales the whole skeleton.',
  floor: 'Tick it when the board lay flat on the floor: FreeMoCap then uses it as the ground, so z = 0 is the floor and z points up. Unticked, camera 0 is the origin.',
};
const slug = (s) => String(s || '').trim().replace(/[^A-Za-z0-9._-]+/g, '-').replace(/^[-.]+|[-.]+$/g, '').slice(0, 60);

export const CalibrateScreen = {
  data: null,           // GET /mocap
  name: null,           // session the data belongs to
  form: null,           // new-profile form {name, take_id, board, square, floor}
  open: new Set(),      // expanded profile ids

  mount(root) {
    this.root = h('main', { class: 'page mc-page' });
    this.root.addEventListener('focusout', () => setTimeout(() => { if (this.deferred && !this.typing()) this.render(); }, 60));
    root.append(this.root);
    this.render();
    this.load();
  },
  unmount() {},
  sessionChanged() {
    Object.assign(this, { data: null, name: null, form: null, open: new Set(), error: null });
  },
  update() { this.load(); },
  onJobs() {
    const job = activeJob('mocap_calibrate');
    const shown = (this.data?.profiles || []).some((p) => p.status === 'running');
    if (!!job !== shown) this.load();        // a calibration started or ended: re-read the profiles
    else this.renderProgress();
  },
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
      this.form = null;
      this.open = new Set(d.active_profile ? [d.active_profile] : []);
    }
    this.data = d;
    for (const p of d.profiles || []) if (p.status === 'running') this.open.add(p.id);   // its progress shows inside
    const ids = d.takes.map((t) => t.take_id);
    if (!this.form) this.form = { name: '', take_id: d.suggested_calibration_take || ids[0] || null, board: '5x3', square: '', floor: true };
    else if (!ids.includes(this.form.take_id)) this.form.take_id = d.suggested_calibration_take || ids[0] || null;
  },
  take(id) { return (this.data?.takes || []).find((t) => t.take_id === id); },
  busy() {
    return activeJob('mocap_calibrate') ? 'another profile is calibrating'
      : activeJob('mocap_process') ? 'wait for mocap processing to finish' : '';
  },

  // ---------------------------------------------------------------- actions
  /** The form makes the profile and calibrates it straight away; it isn't kept if the run can't start. */
  async calibrate() {
    const f = this.form;
    const name = slug(f.name);
    if (!name || !(Number(f.square) > 0)) return;
    const t = this.take(f.take_id);
    const job = await postMocapJob('/mocap/calibrate', {
      name, take_id: f.take_id, board: f.board, square_mm: Number(f.square), groundplane: f.floor,
    }, 'Delete and calibrate', () => t?.name || f.take_id);
    if (!job) return;
    this.form = { ...this.form, name: '', square: '', board: '5x3', floor: true };
    this.load();
  },
  /** Another try at a profile's settings: they go into the form, under a new name. */
  duplicate(p) {
    const names = new Set((this.data.profiles || []).map((q) => q.name));
    let name = `${p.name}-2`;
    for (let k = 3; names.has(name); k += 1) name = `${p.name}-${k}`;
    const ids = this.data.takes.map((t) => t.take_id);
    this.form = {
      name, take_id: ids.includes(p.take_id) ? p.take_id : this.form.take_id,
      board: p.board, square: p.square_mm ?? '', floor: !!p.groundplane,
    };
    this.render();
    const input = this.root.querySelector('.mc-left input[type=text]');
    if (input) { input.focus(); input.select(); }
  },
  async run(p) {
    const t = this.take(p.take_id);
    const job = await postMocapJob(`/mocap/profiles/${encodeURIComponent(p.id)}/calibrate`, {}, 'Delete and calibrate', () => t?.name || p.take_id);
    if (job) this.load();
  },
  async simple(path, method = 'POST') {
    try {
      const d = await api(method, sUrl(path));
      this.adopt(d, this.name);
      if (d.profile) this.open.add(d.profile);
      this.render();
    } catch (e) { toast(e.message, 'error'); }
  },
  async remove(p) {
    const ok = await confirmModal({
      title: `Delete ${p.name}?`,
      body: p.used_by ? `${p.used_by} take(s) picked this profile; they go back to the active one. Its calibration file is deleted.`
        : 'Its calibration file is deleted. Takes already processed with it keep their results.',
      ok: 'Delete profile',
    });
    if (ok) this.simple(`/mocap/profiles/${encodeURIComponent(p.id)}`, 'DELETE');
  },

  // ---------------------------------------------------------------- rendering
  render() {
    const r = this.root;
    if (!r) return;
    this.deferred = false;
    const keep = [r.scrollTop, this.tableBox?.scrollLeft || 0];
    r.innerHTML = '';
    this.tableBox = null;
    this.progressBoxes = new Map();
    this.runningCell = null;
    const head = h('div', { class: 'page-head' },
      h('h1', null, 'Calibration'),
      h('p', null, 'Create and edit calibration profiles for this session.'));
    const d = this.data;
    if (!S.name) return;
    if (!d) {
      r.append(head, h('div', { class: 'empty' }, this.error ? `Couldn't load the calibration profiles: ${this.error}` : [icon('spinner', 'fa-spin'), ' loading…']));
      return;
    }
    if (!d.freemocap?.ready) {
      r.append(head, notReadyCard(d.freemocap || {}));
      return;
    }
    if (!d.takes.length) {
      r.append(head, h('div', { class: 'empty' }, 'No exported takes yet — export the calibration take first. ',
        h('button', { class: 'btn sm', onclick: () => go('export') }, 'Open Export', icon('arrow-right'))));
      return;
    }
    r.append(h('div', { class: 'mc-cols' },
      h('div', { class: 'mc-left' }, head, this.newCard()),
      h('div', { class: 'mc-right' }, this.profilesTable())));
    this.renderProgress();
    r.scrollTop = keep[0];
    if (this.tableBox) this.tableBox.scrollLeft = keep[1];
  },

  takeOptions(cur) {
    return this.data.takes.map((t) => h('option', { value: t.take_id, selected: t.take_id === cur },
      `${t.name} · ${t.cameras.length} cams · ${fmtDur(t.duration)}`));
  },
  seg(cur, pick, disabled = false) {
    return h('div', { class: 'seg', role: 'group', 'aria-label': 'Board' },
      BOARDS.map((b) => h('button', { type: 'button', class: b === cur ? 'on' : '', 'aria-pressed': String(b === cur), disabled, onclick: () => pick(b) }, b)));
  },
  floorToggle(on, flip, disabled = false) {
    return h('div', { class: `toggle ${disabled ? 'disabled' : ''}`, onclick: () => { if (!disabled) flip(); } },
      h('span', { class: `check ${on ? 'on' : ''}`, role: 'checkbox', 'aria-checked': String(!!on), 'aria-disabled': String(disabled) }, icon('check')),
      tip('Board flat on the floor', TIPS.floor));
  },

  newCard() {
    const f = this.form;
    const name = slug(f.name);
    const taken = !!name && (this.data.profiles || []).some((p) => p.name === name);
    const why = () => this.busy() || (!slug(f.name) ? 'name the profile' : !(Number(f.square) > 0) ? 'enter the square size' : '');
    const createBtn = h('button', { class: 'btn primary mc-full', onclick: () => this.calibrate() }, icon('crosshairs'), 'Calibrate');
    const takenNote = h('span', { class: 'small err', hidden: !taken }, 'a profile with this name already exists');
    const refresh = () => {
      const n = slug(f.name);
      const t = !!n && (this.data.profiles || []).some((p) => p.name === n);
      createBtn.disabled = t || !!why();
      createBtn.title = t ? '' : why();
      takenNote.hidden = !t;
      slugNote.textContent = n && n !== f.name.trim() ? `saved as ${n}` : '';
      slugNote.hidden = !slugNote.textContent;
    };
    const slugNote = h('span', { class: 'note mc-note' });
    const card = h('section', { class: 'card mc-card', 'aria-label': 'New calibration profile' },
      h('h2', { class: 'card-title' }, 'New profile'),
      h('label', { class: 'field' }, tip('Name', TIPS.name),
        h('input', {
          class: 'input', type: 'text', value: f.name, placeholder: 'e.g. floor-5x3', autocomplete: 'off', spellcheck: 'false',
          oninput: (e) => { f.name = e.target.value; refresh(); },
          onkeydown: (e) => { if (e.key === 'Enter' && !createBtn.disabled) this.calibrate(); },
        })),
      slugNote,
      h('label', { class: 'field' }, tip('Take', TIPS.take),
        h('select', { class: 'input', onchange: (e) => { f.take_id = e.target.value; } }, this.takeOptions(f.take_id))),
      h('div', { class: 'field', role: 'group' }, tip('Board', TIPS.board), this.seg(f.board, (b) => { f.board = b; this.render(); })),
      h('label', { class: 'field' }, tip('Square (mm)', TIPS.square),
        h('input', {
          class: 'input', type: 'number', min: '0', step: '0.1', placeholder: 'measure it', value: f.square,
          oninput: (e) => { f.square = e.target.value; refresh(); },
        })),
      this.floorToggle(f.floor, () => { f.floor = !f.floor; this.render(); }),
      takenNote,
      createBtn);
    refresh();
    return card;
  },

  profilesTable() {
    const profs = this.data.profiles || [];
    const cal = activeJob('mocap_calibrate');
    const body = [];
    for (const p of profs) {
      const open = this.open.has(p.id);
      const st = profileStatus(p, cal);
      const t = this.take(p.take_id);
      const cams = p.calibrated ? `${(p.cameras || []).length}/${t ? t.cameras.length : (p.cameras || []).length}` : (t ? String(t.cameras.length) : '—');
      body.push(h('tr', {
        class: `mc-prow ${open ? 'open' : ''}`, tabindex: '0', 'aria-expanded': String(open),
        onclick: () => { if (open) this.open.delete(p.id); else this.open.add(p.id); this.render(); },
        onkeydown: (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.currentTarget.click(); } },
      },
      h('td', { class: 'ck' }, icon(open ? 'chevron-down' : 'chevron-right', 'mc-chev')),
      h('td', { class: 'mc-pname' }, p.name),
      this.statusCell(p, st),
      h('td', null, boardText(p)),
      h('td', null, cams),
      h('td', null, p.calibrated ? errText(p) : '—'),
      h('td', null, p.calibrated ? fmtStamp(p.created_at) : '—'),
      h('td', { class: 'mc-active' }, p.active ? 'active' : '')));
      if (open) body.push(h('tr', { class: 'mc-pdetail' }, h('td', { colspan: '8' }, this.detail(p))));
    }
    this.tableBox = h('div', { class: 'tablewrap mc-tablewrap' }, h('table', { class: 'ftable mc-ptable' },
      h('thead', null, h('tr', null,
        h('th', { class: 'ck' }, h('span', { class: 'mc-sr' }, 'Expand')), h('th', null, 'profile'), h('th', null, 'status'),
        h('th', null, 'board'), h('th', null, 'cameras'), h('th', null, 'error'), h('th', null, 'calibrated'),
        h('th', null, h('span', { class: 'mc-sr' }, 'Active')))),
      h('tbody', null, profs.length ? body
        : h('tr', null, h('td', { colspan: '8', class: 'mc-empty' }, 'No profiles yet — calibrate one on the left.')))));
    return this.tableBox;
  },

  detail(p) {
    if (p.status === 'running') {        // just its progress while it calibrates
      const box = h('div', { class: 'col mc-pprogress', style: { gap: '6px' } });
      this.progressBoxes.set(p.id, box);
      return h('div', { class: 'mc-pbody' }, box);
    }
    const busy = this.busy();
    const t = this.take(p.take_id);
    const out = h('div', { class: 'mc-dcol mc-result' });
    if (p.calibrated) {
      const used = { ...p, ...(p.calibrated_with || {}) };
      out.append(h('dl', { class: 'mc-dl' },
        h('dt', null, tip('error', ERR_TIP)), h('dd', null, `${errText(p)} reprojection`),
        h('dt', null, 'take'), h('dd', null, p.take_name || p.take_id),
        h('dt', null, 'cameras'), h('dd', null, h('div', { class: 'mc-chips' }, (p.cameras || []).map((c) => h('span', { class: 'tag' }, camName(c))))),
        h('dt', null, 'board'), h('dd', null, boardText(used)),
        h('dt', null, 'date'), h('dd', null, fmtStamp(p.created_at, true)),
        h('dt', null, 'FreeMoCap'), h('dd', null, p.freemocap || '—')));
      const g = p.groundplane_result;
      if (g && g.requested && g.success === false) out.append(h('span', { class: 'small warn' }, `Ground plane alignment failed, camera 0 is the origin: ${g.error || ''}`));
      if (p.settings_changed) out.append(h('span', { class: 'small warn' }, 'Its board settings differ from this calibration — duplicate it to calibrate them.'));
      if (p.take_reexported) out.append(h('span', { class: 'small warn' }, 'The calibration take was re-exported since — duplicate the profile to calibrate it again.'));
      if (p.reprojection_error_px != null && p.reprojection_error_px >= 2) out.append(h('span', { class: 'small warn' }, 'Above 2 px: check the square size, that every camera saw the board, and that no camera moved.'));
    } else {
      out.append(h('dl', { class: 'mc-dl' },
        h('dt', null, 'take'), h('dd', null, t ? t.name : (p.take_name || p.take_id || '—')),
        h('dt', null, 'board'), h('dd', null, boardText(p))));
      if (p.status === 'failed') {
        out.append(h('span', { class: 'small err mc-note' }, 'The calibration failed. The log has the details; run it again, or duplicate it to change the board settings.'),
          p.error ? h('span', { class: 'note mc-errline', title: p.error }, p.error) : null);
      } else {
        out.append(h('span', { class: 'small mc-note', style: { color: COLORS.blue } }, 'Not calibrated yet.'));
      }
      if (!t) out.append(h('span', { class: 'small err' }, "Its calibration take isn't exported any more — duplicate it and pick another take."));
    }
    const why = busy || (!t ? "its take isn't exported" : '');
    out.append(h('div', { class: 'mc-dbtns' },
      !p.calibrated ? h('button', { class: 'btn sm primary', disabled: !!why, title: why, onclick: () => this.run(p) },
        icon('crosshairs'), p.status === 'failed' ? 'Run again' : 'Calibrate') : null,
      p.calibrated && !p.active ? h('button', { class: 'btn sm', disabled: !!busy, onclick: () => this.simple(`/mocap/profiles/${encodeURIComponent(p.id)}/activate`) }, 'Set active') : null,
      h('button', { class: 'btn sm', title: 'Put its settings in the New profile form', onclick: () => this.duplicate(p) }, icon('copy'), 'Duplicate'),
      h('button', { class: 'btn sm danger', disabled: p.active, title: p.active ? 'make another profile active first' : '', onclick: () => this.remove(p) }, icon('trash'), 'Delete')));
    return h('div', { class: 'mc-pbody' }, out);
  },

  statusCell(p, st) {
    const td = h('td', null, h('span', { class: 'mc-st', style: { color: st.color } }, square(st.color), st.text));
    if (p.status === 'running') this.runningCell = { td, p };
    return td;
  },
  renderProgress() {
    if (!this.progressBoxes) return;
    const job = activeJob('mocap_calibrate');
    const rc = this.runningCell;
    if (rc && rc.td.isConnected) {
      const st = profileStatus(rc.p, job);
      rc.td.innerHTML = '';
      rc.td.append(h('span', { class: 'mc-st', style: { color: st.color } }, square(st.color), st.text));
    }
    for (const box of this.progressBoxes.values()) {
      if (!box.isConnected) continue;
      box.innerHTML = '';
      const f = job ? Math.max(0, Math.min(1, job.progress || 0)) : 0;
      box.append(
        h('div', { class: 'progress-label' }, h('span', { class: 'ellipsis' }, job?.message || 'starting FreeMoCap…'),
          tip(`${Math.round(f * 100)}%`, 'Board detection runs on every frame of every camera; a 2-minute take can take 10–15 minutes.')),
        h('div', { class: 'progress thin' }, h('div', { style: { width: `${f * 100}%` } })));
    }
  },
};
