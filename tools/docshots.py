"""Screenshots and short clips of the UI for the docs, with the footage censored.

Drives the installed Edge (Playwright, channel "msedge") against a running app and writes
``docs/img/<name>.png`` for every screen, plus ``.webm`` clips of the moving parts (sync
replay, Cut Takes playback, the skeleton viewer) that ``--gif`` turns into GIFs with ffmpeg.

Every source-video pixel is pixelated before anything is captured: a mosaic canvas is laid
over each ``<video>`` and thumbnail ``<img>`` and redrawn 10× a second, so the clips and the
frame viewer never show recognisable footage. The skeleton viewer draws no footage and is
left alone.

    .tools\\docs-venv\\Scripts\\python tools\\docshots.py --base http://127.0.0.1:8765 --session 2026-10-19
    .tools\\docs-venv\\Scripts\\python tools\\docshots.py ... --gif          # also hero.gif + per-scene gifs

Needs ``pip install playwright`` (no browser download: it uses the Edge you have).
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

from playwright.sync_api import Page, sync_playwright

ROOT = Path(__file__).resolve().parent.parent
MOSAIC_BLOCK = 14          # censor cell size in CSS pixels, and never more than MOSAIC_COLS cells across
MOSAIC_COLS = 16           # so a big element (the frame viewer) is as coarse as a small tile

# Laid over every video and thumbnail: a canvas that shows a coarse mosaic of the element
# under it, redrawn while the page runs. Installed on every navigation (init script).
CENSOR_JS = r"""
(() => {
  if (window.__censorInstalled) return;
  window.__censorInstalled = true;
  const BLOCK = %d, COLS = %d;
  const SEL = 'video, img.poster, .campreview img, .camfile .mini img, .fv-img';
  const overlays = new Map();
  function overlayFor(el) {
    let c = overlays.get(el);
    if (!c) {
      c = document.createElement('canvas');
      c.className = 'censor-mosaic';
      c.style.cssText = 'position:absolute;pointer-events:none;z-index:1;image-rendering:pixelated;';
      overlays.set(el, c);
    }
    return c;
  }
  function tick() {
    for (const el of document.querySelectorAll(SEL)) {
      const parent = el.parentElement;
      if (!parent || el.hidden || el.style.visibility === 'hidden' || el.style.display === 'none') { overlays.get(el)?.remove(); continue; }
      const r = el.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) { overlays.get(el)?.remove(); continue; }
      const c = overlayFor(el);
      const pr = parent.getBoundingClientRect();
      if (getComputedStyle(parent).position === 'static') parent.style.position = 'relative';
      c.style.left = `${r.left - pr.left + parent.scrollLeft}px`;
      c.style.top = `${r.top - pr.top + parent.scrollTop}px`;
      c.style.width = `${r.width}px`;
      c.style.height = `${r.height}px`;
      const cell = Math.max(BLOCK, r.width / COLS, r.height / COLS);
      const cw = Math.max(2, Math.round(r.width / cell)), ch = Math.max(2, Math.round(r.height / cell));
      if (c.width !== cw || c.height !== ch) { c.width = cw; c.height = ch; }
      const g = c.getContext('2d');
      g.imageSmoothingEnabled = false;
      try {
        g.drawImage(el, 0, 0, cw, ch);
      } catch (e) {
        g.fillStyle = '#2a2f36'; g.fillRect(0, 0, cw, ch);
      }
      if (c.parentElement !== parent) parent.appendChild(c);
    }
    for (const [el, c] of overlays) if (!el.isConnected) { c.remove(); overlays.delete(el); }
  }
  setInterval(tick, 100);
})();
""" % (MOSAIC_BLOCK, MOSAIC_COLS)


UNCLIP_CSS = ".app{height:auto!important} body,html{overflow:visible!important;height:auto!important} main.page{overflow:visible!important}"


def shot(page: Page, out: Path, name: str, full: bool = False) -> None:
    """``full``: the whole page, including what main.page would scroll (the app pins the
    body height and scrolls the main column, so full_page alone sees only the viewport)."""
    page.wait_for_timeout(500)
    if full:
        page.add_style_tag(content=UNCLIP_CSS)
        page.wait_for_timeout(300)
    page.screenshot(path=str(out / f"{name}.png"), full_page=full)
    if full:
        page.evaluate("document.querySelectorAll('style').forEach((s) => { if (s.textContent.includes('.app{height:auto!important}')) s.remove(); })")
    print("  ", f"{name}.png")


def goto(page: Page, base: str, session: str, step: str, settle: int = 2500) -> None:
    page.goto(f"{base}/#/{session}/{step}", wait_until="networkidle")
    page.wait_for_timeout(settle)


def capture_screens(page: Page, base: str, session: str, out: Path) -> None:
    page.goto(f"{base}/#/", wait_until="networkidle")
    page.wait_for_timeout(2500)
    shot(page, out, "home")

    goto(page, base, session, "process")
    shot(page, out, "process")
    page.click(".adv-toggle")
    shot(page, out, "process-advanced")

    goto(page, base, session, "cameras")
    shot(page, out, "cameras")

    goto(page, base, session, "sync")
    shot(page, out, "sync")
    shot(page, out, "sync-full", full=True)

    goto(page, base, session, "review", settle=4000)
    shot(page, out, "review")
    takes = page.locator(".take-blk")
    if takes.count():
        takes.first.click()
        page.wait_for_timeout(1500)
        shot(page, out, "review-take")
    page.keyboard.press("k")        # deselect
    page.wait_for_timeout(300)
    # the full-quality frame viewer: click a tile that has footage
    tile = page.locator(".tile.has-frame").first
    if tile.count():
        tile.click()
        page.wait_for_timeout(2500)
        shot(page, out, "frameview")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    goto(page, base, session, "export")
    shot(page, out, "export")

    goto(page, base, session, "calibrate", settle=3000)
    row = page.locator("tr.mc-prow").first
    if row.count() and row.get_attribute("aria-expanded") != "true":     # the active profile opens by itself
        row.click()
        page.wait_for_timeout(800)
    shot(page, out, "calibrate")

    goto(page, base, session, "mocap", settle=3000)
    shot(page, out, "mocap")
    for head in page.locator(".mc-winhead").all():
        if head.get_attribute("aria-expanded") != "true":
            head.click()
            page.wait_for_timeout(200)
    shot(page, out, "mocap-settings", full=True)
    done = processed_take_row(page)
    if done is not None:
        done.locator(".mc-tname").click()
        page.wait_for_timeout(3500)
        page.locator(".mc-tpanel").first.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        shot(page, out, "mocap-take")

    # the job drawer in the header
    page.goto(f"{base}/#/{session}/process", wait_until="networkidle")
    page.wait_for_timeout(1500)
    pill = page.locator(".jobpill")
    if pill.count():
        pill.click()
        page.wait_for_timeout(800)
        shot(page, out, "jobs")


def processed_take_row(page: Page):
    """The first row of the Mocap table whose status is done, unfolding "+ N more takes" first."""
    more = page.locator(".mc-more")
    if more.count() and more.get_attribute("aria-expanded") != "true":
        more.click()
        page.wait_for_timeout(500)
    rows = page.locator("table.mc-table tbody tr")
    for i in range(rows.count()):
        row = rows.nth(i)
        status = row.locator(".mc-st").first
        if status.count() and status.inner_text().strip().split(" ")[0] in ("done", "stale"):   # both have a skeleton
            return row
    return None


def record_scene(context_factory, base: str, session: str, out: Path, name: str, act, size: dict):
    """One clip per scene: a fresh context records its page to webm; ``act(page, clock)``
    performs the scene and returns the recording time (clock()) at which the interesting
    part starts, so the GIF can skip the page load. Returns (webm path, start) or None."""
    tmp = out / "_rec"
    tmp.mkdir(exist_ok=True)
    ctx = context_factory(record_video_dir=str(tmp), record_video_size=size)
    t0 = time.monotonic()
    page = ctx.new_page()
    page.add_init_script(CENSOR_JS)
    try:
        start = act(page, lambda: time.monotonic() - t0) or 0.0
    finally:
        video = page.video
        ctx.close()
    src = Path(video.path()) if video else None
    if not src or not src.exists():
        return None
    dst = out / f"{name}.webm"
    shutil.move(str(src), dst)
    print("  ", dst.name, f"(from {start:.1f} s)")
    return dst, start


def scene_sync(base, session):
    def act(page: Page, clock):
        goto(page, base, session, "sync", settle=2500)
        btn = page.locator(".vcounts button", has_text="Replay")
        start = clock()
        if btn.count():
            btn.click()
        page.wait_for_timeout(9000)
        return start
    return act


def scene_review(base, session):
    def act(page: Page, clock):
        goto(page, base, session, "review", settle=4000)
        takes = page.locator(".take-blk")
        if takes.count() > 1:
            takes.nth(1).click()          # selects the take and seeks to its start
            page.wait_for_timeout(2500)   # the proxies load
        page.keyboard.press("k")
        start = clock()
        page.keyboard.press(" ")          # play
        page.wait_for_timeout(8000)
        page.keyboard.press(" ")
        return start
    return act


def scene_mocap(base, session):
    def act(page: Page, clock):
        goto(page, base, session, "mocap", settle=3000)
        done = processed_take_row(page)
        if done is None:
            return 0.0
        done.locator(".mc-tname").click()
        page.wait_for_timeout(3500)
        page.locator(".mc-tpanel").first.scroll_into_view_if_needed()
        page.wait_for_timeout(500)
        start = clock()
        play = page.locator(".skel-play")
        if play.count():
            play.click()
        page.wait_for_timeout(8000)
        return start
    return act


GIF_WIDTH, GIF_FPS, CLIP_SECONDS = 880, 8, 6.0


def to_gif(src: Path, dst: Path, start: float = 0.0, length: float = CLIP_SECONDS, width: int = GIF_WIDTH, fps: int = GIF_FPS) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg not found: keeping the .webm only", file=sys.stderr)
        return
    vf = f"fps={fps},scale={width}:-2:flags=lanczos,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3"
    subprocess.run([ffmpeg, "-y", "-v", "error", "-ss", f"{start}", "-t", f"{length}", "-i", str(src), "-vf", vf, "-loop", "0", str(dst)], check=True)
    print("  ", dst.name, f"{dst.stat().st_size // 1024} KB")


def hero_gif(clips: list[tuple[Path, float]], dst: Path, width: int = GIF_WIDTH, fps: int = GIF_FPS, per_clip: float = CLIP_SECONDS) -> None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or not clips:
        return
    inputs, parts = [], []
    for i, (c, start) in enumerate(clips):
        inputs += ["-ss", f"{start}", "-t", f"{per_clip}", "-i", str(c)]
        parts.append(f"[{i}:v]fps={fps},scale={width}:-2:flags=lanczos,setsar=1[v{i}]")
    chain = "".join(f"[v{i}]" for i in range(len(clips)))
    graph = ";".join(parts) + f";{chain}concat=n={len(clips)}:v=1:a=0,split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3"
    subprocess.run([ffmpeg, "-y", "-v", "error", *inputs, "-filter_complex", graph, "-loop", "0", str(dst)], check=True)
    print("  ", dst.name, f"{dst.stat().st_size // 1024} KB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--session", required=True, help="the session to show")
    ap.add_argument("--out", default=str(ROOT / "docs" / "img"))
    ap.add_argument("--gif", action="store_true", help="also record the moving parts and make GIFs")
    ap.add_argument("--only-gif", action="store_true", help="skip the screenshots, just the clips and GIFs")
    ap.add_argument("--no-censor", action="store_true", help="skip the mosaic (never for publishing)")
    ap.add_argument("--width", type=int, default=1600)
    ap.add_argument("--height", type=int, default=1000)
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch(channel="msedge", headless=True)

        def context_factory(**kw):
            return browser.new_context(viewport={"width": args.width, "height": args.height},
                                       device_scale_factor=1, **kw)

        t0 = time.time()
        if not args.only_gif:
            ctx = context_factory()
            if not args.no_censor:
                ctx.add_init_script(CENSOR_JS)
            page = ctx.new_page()
            print("screens:")
            capture_screens(page, args.base, args.session, out)
            ctx.close()
        clips = []
        if args.gif or args.only_gif:
            print("clips:")
            size = {"width": args.width, "height": args.height}
            for name, scene in (("sync-replay", scene_sync), ("review-playback", scene_review), ("mocap-skeleton", scene_mocap)):
                rec = record_scene(context_factory if args.no_censor else _censored(context_factory),
                                   args.base, args.session, out, name, scene(args.base, args.session), size)
                if rec:
                    clips.append(rec)
                    to_gif(rec[0], out / f"{name}.gif", start=rec[1])
            hero_gif(clips, out / "hero.gif")
            shutil.rmtree(out / "_rec", ignore_errors=True)
        browser.close()
    print(f"done in {time.time() - t0:.0f} s -> {out}")


def _censored(factory):
    def make(**kw):
        ctx = factory(**kw)
        ctx.add_init_script(CENSOR_JS)
        return ctx
    return make


if __name__ == "__main__":
    main()
