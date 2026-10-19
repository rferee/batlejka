// Multi-camera preview grid: one proxy <video> per camera, CSS-only rotation (R1),
// throttled seeks and a drift-corrected playback (R2, §7.4).

/** Fit a dw×dh media element into a cw×ch box after rotating it by `rot`
 * degrees (CSS, around its centre). Returns the element's unrotated box. */
export function fitRotated(cw, ch, dw, dh, rot) {
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
