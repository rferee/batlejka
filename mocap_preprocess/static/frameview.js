// Frame viewer: one full-quality frame over the whole window, to zoom into. Cut Takes opens
// it when a video tile is clicked; the frame comes from the source file (or FreeMoCap's
// full-size video with Preview on), not from the low-res proxy on screen.
import { h, icon } from './app.js';

const MAX_SCALE = 16;   // ×16 of the frame's own pixels

/**
 * openFrame({url, title, sub}): show the PNG at ``url``. Wheel zooms around the pointer,
 * dragging pans, double-click toggles fit / 100 %, 0 and 1 do the same, Esc closes.
 */
export function openFrame({ url, title, sub }) {
  const prevFocus = document.activeElement;
  const img = h('img', { class: 'fv-img', alt: title, draggable: 'false' });
  const status = h('div', { class: 'fv-status' }, icon('spinner', 'fa-spin'), ' loading the full-quality frame…');
  const zoomLbl = h('span', { class: 'fv-zoom' }, '');
  const stage = h('div', { class: 'fv-stage' }, img, status);
  const closeBtn = h('button', { class: 'btn sm', title: 'Close [Esc]', 'aria-label': 'Close' }, icon('xmark'));
  const fullBtn = h('button', { class: 'btn sm', title: 'Browser full screen', 'aria-label': 'Full screen' }, icon('expand'));
  const fitBtn = h('button', { class: 'btn sm', title: 'Fit to the window [0]' }, 'Fit');
  const oneBtn = h('button', { class: 'btn sm', title: 'Actual pixels [1]' }, '100%');
  const root = h('div', { class: 'frameview', role: 'dialog', 'aria-modal': 'true', 'aria-label': title, tabindex: '-1' },
    h('div', { class: 'fv-bar' },
      h('div', { class: 'fv-title' }, h('b', null, title), sub ? h('span', { class: 'muted-60' }, sub) : null),
      zoomLbl, fitBtn, oneBtn, fullBtn, closeBtn),
    stage,
    h('div', { class: 'fv-hint muted-60' }, 'wheel: zoom · drag: pan · double-click: fit / 100% · Esc: close'));

  let iw = 0, ih = 0, scale = 1, tx = 0, ty = 0, fit = 1, drag = null;
  const apply = () => {
    img.style.transform = `translate(${tx}px, ${ty}px) scale(${scale})`;
    zoomLbl.textContent = iw ? `${Math.round(scale * 100)}%` : '';
  };
  const fitScale = () => {
    const r = stage.getBoundingClientRect();
    return Math.min(r.width / iw, r.height / ih, 1);
  };
  const toFit = () => {
    if (!iw) return;
    const r = stage.getBoundingClientRect();
    fit = fitScale();
    scale = fit;
    tx = (r.width - iw * scale) / 2;
    ty = (r.height - ih * scale) / 2;
    apply();
  };
  /** Zoom to ``s`` keeping the frame point under (mx, my) — stage coordinates — in place. */
  const zoomAt = (s, mx, my) => {
    s = Math.min(MAX_SCALE, Math.max(Math.min(fit, 1) * 0.5, s));
    tx = mx - (mx - tx) * (s / scale);
    ty = my - (my - ty) * (s / scale);
    scale = s;
    apply();
  };
  const local = (e) => {
    const r = stage.getBoundingClientRect();
    return [e.clientX - r.left, e.clientY - r.top];
  };
  const center = () => { const r = stage.getBoundingClientRect(); return [r.width / 2, r.height / 2]; };

  img.addEventListener('load', () => {
    iw = img.naturalWidth;
    ih = img.naturalHeight;
    status.remove();
    img.classList.add('ready');
    toFit();
  });
  img.addEventListener('error', () => {
    status.innerHTML = '';
    status.append(icon('triangle-exclamation'), " couldn't load this frame. The log on the server has the reason.");
    status.classList.add('err');
  });
  stage.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (!iw) return;
    zoomAt(scale * Math.exp(-e.deltaY * 0.0015), ...local(e));
  }, { passive: false });
  stage.addEventListener('pointerdown', (e) => {
    if (e.button !== 0 && e.button !== 1) return;
    e.preventDefault();
    stage.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, y: e.clientY, tx, ty };
    stage.classList.add('grabbing');
  });
  stage.addEventListener('pointermove', (e) => {
    if (!drag) return;
    tx = drag.tx + (e.clientX - drag.x);
    ty = drag.ty + (e.clientY - drag.y);
    apply();
  });
  const endDrag = (e) => {
    drag = null;
    stage.classList.remove('grabbing');
    if (stage.hasPointerCapture(e.pointerId)) stage.releasePointerCapture(e.pointerId);
  };
  stage.addEventListener('pointerup', endDrag);
  stage.addEventListener('pointercancel', endDrag);
  stage.addEventListener('dblclick', (e) => {
    if (!iw) return;
    if (Math.abs(scale - 1) < 0.01) toFit(); else zoomAt(1, ...local(e));
  });

  const onResize = () => { if (iw && Math.abs(scale - fit) < 1e-6) toFit(); else fit = iw ? fitScale() : 1; };
  // while open, the viewer has the keyboard: Cut Takes' shortcuts mustn't fire underneath
  const onKey = (e) => {
    e.stopImmediatePropagation();
    if (e.key === 'Escape') { e.preventDefault(); if (!document.fullscreenElement) close(); }
    else if (e.key === '0') toFit();
    else if (e.key === '1' && iw) zoomAt(1, ...center());
    else if ((e.key === '+' || e.key === '=') && iw) zoomAt(scale * 1.25, ...center());
    else if (e.key === '-' && iw) zoomAt(scale / 1.25, ...center());
  };
  function close() {
    window.removeEventListener('keydown', onKey, true);
    window.removeEventListener('resize', onResize);
    if (document.fullscreenElement === root) document.exitFullscreen().catch(() => {});
    root.remove();
    if (prevFocus && prevFocus.focus) prevFocus.focus();
  }
  closeBtn.addEventListener('click', close);
  fitBtn.addEventListener('click', toFit);
  oneBtn.addEventListener('click', () => { if (iw) zoomAt(1, ...center()); });
  fullBtn.addEventListener('click', () => {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else root.requestFullscreen?.().catch(() => {});
  });
  root.addEventListener('fullscreenchange', () => setTimeout(toFit, 50));
  window.addEventListener('keydown', onKey, true);
  window.addEventListener('resize', onResize);
  document.body.append(root);
  root.focus();
  img.src = url;
  return close;
}
