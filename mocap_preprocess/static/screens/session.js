// Home (Session) screen: the sessions list with an inline "create" form, and the Setup card.
import { setupCard } from '../setup.js';
import {
  POST, S, fmtBytes, go, h, icon, loadSessions,
  openSession, reveal, toast,
} from '../app.js';

/** A session folder name from what was typed (the server validates it too). */
const slugOf = (draft) => draft.trim().replace(/[<>:"/\\|?*]+/g, '').replace(/\s+/g, '-');

const STATE = {   // label, colour of a session's 12px status mark
  empty: ['empty', '#8880F0'], new: ['new', '#E89768'], processed: ['processed', '#d3d3d3'],
  synced: ['synced', '#8880F0'], exported: ['exported', '#97d47d'],
};
function sessionState(s) {
  if (!s.n_source_files) return STATE.empty;
  if (s.exported) return STATE.exported;
  if (s.synced) return STATE.synced;
  if (s.processed) return STATE.processed;
  return STATE.new;
}

// ------------------------------------------------------------------ Session screen
export const SessionScreen = {
  focusNew: false,      // set by the header's "New session…": open the create form
  mode: 'idle',         // 'idle' | 'editing' (the inline create form)
  draft: '',
  mount(root) {
    this.root = h('main', { class: 'page home-page' });
    root.append(this.root);
    this.update();
  },
  update() {
    const r = this.root;
    if (this.focusNew) {
      this.focusNew = false;
      this.mode = 'editing';
      this.wantFocus = true;
    }
    // keep a half-typed name, its caret and the focus across re-renders (jobs poll in the background)
    const input = r.querySelector('.home-create input');
    const hadFocus = input && document.activeElement === input;
    const caret = hadFocus ? [input.selectionStart, input.selectionEnd] : null;
    const top = r.scrollTop;
    r.innerHTML = '';

    const left = h('div', { class: 'home-main' },
      h('div', { class: 'page-head' },
        h('h1', null, 'Sessions'),
        h('p', null, 'Drop footage into ', h('span', { class: 'accent' }, 'sessions/<name>/source/'),
          ', refresh, pick it here. That\'s the whole onboarding.')),
      this.list(),
      h('div', { class: 'home-actions' },
        h('button', { class: 'btn big', onclick: () => loadSessions() }, icon('rotate'), 'Rescan sessions'),
        h('button', { class: 'btn big', disabled: !S.name, onclick: () => reveal('source') }, icon('folder-open'), 'Open folder')),
      S.health ? h('span', { class: 'home-note' }, `sessions folder: ${S.health.sessions_dir}`) : null);
    r.append(left, setupCard());
    r.scrollTop = top;

    const fresh = r.querySelector('.home-create input');
    if (fresh && (hadFocus || this.wantFocus)) {
      this.wantFocus = false;
      fresh.focus();
      if (caret) fresh.setSelectionRange(caret[0], caret[1]);
    }
  },

  list() {
    const box = h('div', { class: 'home-list' });
    if (!S.sessions.length && this.mode === 'idle') box.append(h('div', { class: 'empty' }, 'No sessions yet — create one below.'));
    for (const s of S.sessions) {
      const sel = s.name === S.name;
      const [label, color] = sessionState(s);
      box.append(h('button', {
        type: 'button', class: `home-row ${sel ? 'sel' : ''}`, 'aria-pressed': sel ? 'true' : 'false',
        title: 'Open this session (double-click: open it and go to Process)',
        onclick: () => openSession(s.name),
        ondblclick: () => openSession(s.name).then(() => go('process')),
      },
      h('span', { class: 'mark' }, sel ? icon('check') : null),
      h('span', { class: 'nm' }, h('b', null, s.name), h('span', { class: 'path' }, `sessions/${s.name}/source`)),
      h('span', { class: 'meta' }, `${s.n_source_files} files`),
      h('span', { class: 'meta' }, fmtBytes(s.size)),
      h('span', { class: 'state', style: { color } }, h('span', { class: 'sq', style: { background: color } }), label)));
    }
    box.append(this.mode === 'editing' ? this.form() : h('button', {
      type: 'button', class: 'phantom', onclick: () => { this.mode = 'editing'; this.draft = ''; this.wantFocus = true; this.update(); },
    }, icon('plus'), 'Create new session'));
    return box;
  },

  /** The inline create form: name, live preview of the folder it makes, enter / esc. */
  form() {
    const slug = () => slugOf(this.draft);
    const taken = () => S.sessions.some((s) => s.name.toLowerCase() === slug().toLowerCase());
    const shown = h('b', null);
    const err = h('span', { class: 'err', hidden: true }, 'a session with this name already exists');
    const createBtn = h('button', { type: 'submit', class: 'btn primary' }, icon('plus'), 'Create');
    const refresh = () => {
      shown.textContent = slug() || 'new-session-name';
      err.hidden = !taken();
      createBtn.disabled = !slug() || taken() || this.creating;
    };
    const cancel = () => { this.mode = 'idle'; this.draft = ''; this.update(); };
    const input = h('input', {
      class: 'input', type: 'text', value: this.draft, placeholder: 'new-session-name', id: 'new-session',
      autocomplete: 'off', spellcheck: 'false', 'aria-label': 'New session name',
      oninput: (e) => { this.draft = e.target.value; refresh(); },
      onkeydown: (e) => { if (e.key === 'Escape') { e.preventDefault(); cancel(); } },
    });
    const create = async () => {
      const name = slug();
      if (!name || taken() || this.creating) return;
      this.creating = true;
      refresh();
      try {
        await POST('/api/sessions', { name });
        this.mode = 'idle';
        this.draft = '';
        await loadSessions();
        await openSession(name);
        toast(`Created sessions/${name}/source/ — drop footage there`, 'ok');
      } catch (e) {
        toast(e.message, 'error');
      } finally {
        this.creating = false;
        if (this.mode === 'editing') refresh();
      }
    };
    refresh();
    return h('form', { class: 'home-create', onsubmit: (e) => { e.preventDefault(); create(); } },
      h('div', { class: 'line' }, input, createBtn,
        h('button', { type: 'button', class: 'btn', onclick: cancel }, 'Cancel')),
      h('div', { class: 'preview', 'aria-live': 'polite' },
        h('span', { class: 'dim' }, 'creates'),
        h('span', null, 'sessions/', shown, '/'),
        h('span', null, '└ source/ ', h('span', { class: 'dim' }, '← drop footage here')),
        err),
      h('span', { class: 'home-note' }, 'enter to create · esc to cancel'));
  },
};
