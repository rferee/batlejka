"""Screenshots and short clips of the UI for the docs, with the footage censored.

Drives the installed Edge (Playwright, channel "msedge") against a running app and writes
``docs/img/<name>.png`` for every screen, plus ``.webm`` clips of the moving parts (sync
replay, Cut Takes playback, the skeleton viewer) that ``--gif`` turns into GIFs with ffmpeg.

Every source-video pixel is pixelated before anything is captured: a mosaic canvas is laid
over each ``<video>`` and thumbnail ``<img>`` and redrawn 10× a second, so the clips and the
frame viewer never show recognisable footage. The skeleton viewer draws no footage and is
left alone.

Screenshots are taken at 1920×1080 and set in a floating window with a drop shadow
(``--plain`` keeps the raw capture).

    .tools\\docs-venv\\Scripts\\python tools\\docshots.py --base http://127.0.0.1:8765 --session 2026-10-19
    .tools\\docs-venv\\Scripts\\python tools\\docshots.py ... --gif          # also hero.gif + per-scene gifs
    .tools\\docs-venv\\Scripts\\python tools\\docshots.py ... --only-gif --third goofy_aaah_dance:28-120 --first come_at_me:50-90:15

Needs ``pip install playwright`` (no browser download: it uses the Edge you have).
"""
from __future__ import annotations

import argparse
import base64
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
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

# The floating window around a screenshot: a soft gradient behind, rounded corners, a hairline
# and a drop shadow. The capture is embedded as a data URL and the wrapper is screenshotted.
FRAME_HTML = """<!doctype html><html><head><meta charset="utf-8"><style>
  html, body { margin: 0; background: #0c0e12; }
  .wrap { display: inline-block; padding: 56px 72px 84px;
          background: radial-gradient(1100px 620px at 28%% 0%%, #333b4a 0%%, #171a20 58%%, #0c0e12 100%%); }
  .win { border-radius: 14px; overflow: hidden; outline: 1px solid rgba(255,255,255,.09);
         box-shadow: 0 28px 70px rgba(0,0,0,.62), 0 6px 18px rgba(0,0,0,.45); }
  .win img { display: block; width: %dpx; height: %dpx; }
</style></head><body><div class="wrap"><div class="win"><img src="data:image/png;base64,%s"></div></div></body></html>"""


class Shooter:
    """Takes the screenshots; with a ``frame`` page they come out as floating windows."""

    def __init__(self, out: Path, frame: Page | None):
        self.out = out
        self.frame = frame

    def shot(self, page: Page, name: str, full: bool = False) -> None:
        """``full``: the whole page, including what main.page would scroll (the app pins the
        body height and scrolls the main column, so full_page alone sees only the viewport)."""
        page.wait_for_timeout(500)
        if full:
            page.add_style_tag(content=UNCLIP_CSS)
            page.wait_for_timeout(300)
        dst = self.out / f"{name}.png"
        png = page.screenshot(full_page=full)
        if full:
            page.evaluate("document.querySelectorAll('style').forEach((s) => { if (s.textContent.includes('.app{height:auto!important}')) s.remove(); })")
        if self.frame is not None:
            png = self.framed(png, page.viewport_size["width"], full)
        for attempt in range(5):        # a viewer or an indexer may hold the old file for a moment
            try:
                dst.write_bytes(png)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.5)
        print("  ", dst.name)

    def framed(self, png: bytes, width: int, full: bool) -> bytes:
        # the PNG's height: IHDR is right after the 8-byte signature; width at 16, height at 20
        height = int.from_bytes(png[20:24], "big")
        self.frame.set_viewport_size({"width": width + 160, "height": height + 160})
        self.frame.set_content(FRAME_HTML % (width, height, base64.b64encode(png).decode("ascii")))
        self.frame.wait_for_timeout(150)
        return self.frame.locator(".wrap").screenshot()


def goto(page: Page, base: str, session: str, step: str, settle: int = 2500) -> None:
    # not "networkidle": while a job runs the page polls every 600 ms and the network never idles
    page.goto(f"{base}/#/{session}/{step}", wait_until="load")
    page.wait_for_timeout(settle)


def capture_screens(page: Page, base: str, session: str, sh: Shooter) -> None:
    page.goto(f"{base}/#/", wait_until="load")
    page.wait_for_timeout(2500)
    sh.shot(page, "home")

    goto(page, base, session, "process")
    sh.shot(page, "process")
    page.click(".adv-toggle")
    sh.shot(page, "process-advanced")

    goto(page, base, session, "cameras")
    sh.shot(page, "cameras")

    goto(page, base, session, "sync")
    sh.shot(page, "sync")
    sh.shot(page, "sync-full", full=True)

    goto(page, base, session, "review", settle=4000)
    sh.shot(page, "review")
    takes = page.locator(".take-blk")
    if takes.count():
        takes.first.click()
        page.wait_for_timeout(1500)
        sh.shot(page, "review-take")
    page.keyboard.press("k")        # deselect
    page.wait_for_timeout(300)
    # the full-quality frame viewer: click a tile that has footage
    tile = page.locator(".tile.has-frame").first
    if tile.count():
        tile.click()
        page.wait_for_timeout(2500)
        sh.shot(page, "frameview")
        page.keyboard.press("Escape")
        page.wait_for_timeout(300)

    goto(page, base, session, "export")
    sh.shot(page, "export")

    goto(page, base, session, "calibrate", settle=3000)
    row = page.locator("tr.mc-prow").first
    if row.count() and row.get_attribute("aria-expanded") != "true":     # the active profile opens by itself
        row.click()
        page.wait_for_timeout(800)
    sh.shot(page, "calibrate")

    goto(page, base, session, "mocap", settle=3000)
    sh.shot(page, "mocap")
    for head in page.locator(".mc-winhead").all():
        if head.get_attribute("aria-expanded") != "true":
            head.click()
            page.wait_for_timeout(200)
    sh.shot(page, "mocap-settings", full=True)
    done = processed_take_row(page)
    if done is not None:
        open_take(page, done)
        sh.shot(page, "mocap-take")

    # the job drawer in the header
    page.goto(f"{base}/#/{session}/process", wait_until="load")
    page.wait_for_timeout(1500)
    pill = page.locator(".jobpill")
    if pill.count():
        pill.click()
        page.wait_for_timeout(800)
        sh.shot(page, "jobs")


def take_rows(page: Page):
    """The Mocap table's rows, "+ N more takes" unfolded."""
    more = page.locator(".mc-more")
    if more.count() and more.get_attribute("aria-expanded") != "true":
        more.click()
        page.wait_for_timeout(500)
    return page.locator("table.mc-table tbody tr")


def row_status(row) -> str:
    status = row.locator(".mc-st").first
    return status.inner_text().strip().split(" ")[0] if status.count() else ""


def processed_take_row(page: Page, name: str | None = None):
    """The row of the Mocap table for take ``name`` (or the first one) whose status is done
    or stale: both have a skeleton. None when there is no such row."""
    rows = take_rows(page)
    for i in range(rows.count()):
        row = rows.nth(i)
        label = row.locator(".mc-tname").first
        if name is not None and (not label.count() or label.inner_text().strip() != name):
            continue
        if row_status(row) in ("done", "stale"):
            return row
        if name is not None:
            print(f"   {name}: not processed yet ({row_status(row) or 'no status'})", file=sys.stderr)
            return None
    if name is not None:
        print(f"   {name}: no such take", file=sys.stderr)
    return None


def open_take(page: Page, row) -> None:
    """Opens the take's detail and waits for the skeleton viewer to have loaded."""
    row.locator(".mc-tname").click()
    page.wait_for_selector(".skel-play:not([disabled])", timeout=20000)
    page.wait_for_timeout(600)
    page.locator(".mc-tpanel").first.scroll_into_view_if_needed()
    page.wait_for_timeout(400)


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
        start = act(page, lambda: time.monotonic() - t0)
    finally:
        video = page.video
        ctx.close()
    src = Path(video.path()) if video else None
    if start is None or not src or not src.exists():
        if src and src.exists():
            src.unlink()
        return None
    dst = out / f"{name}.webm"
    shutil.move(str(src), dst)
    print("  ", dst.name, f"(from {start:.1f} s)")
    return dst, start


def scene_sync(base, session):
    """The sync replay: ~8 s of animation, shown at double speed (SYNC_SPEED)."""
    def act(page: Page, clock):
        goto(page, base, session, "sync", settle=2500)
        btn = page.locator(".vcounts button", has_text="Replay")
        if not btn.count():
            return None
        start = clock()
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
        page.wait_for_timeout(REVIEW_SECONDS * 1000 + 1500)
        page.keyboard.press(" ")
        return start
    return act


@dataclass
class SkeletonClip:
    take: str | None      # take name; None = the first processed take
    mode: str             # 'third' | 'first'
    first: int = 0        # first frame (the viewer's slider value)
    last: int | None = None
    rate: float = 30.0    # frames per second to step through them

    @property
    def seconds(self) -> float:
        return ((self.last or self.first + 120) - self.first) / self.rate

    @classmethod
    def parse(cls, mode: str, spec: str | None) -> SkeletonClip:
        """``take:first-last[:rate]``; '' or None = the first processed take, 4 s from its start."""
        if not spec:
            return cls(None, mode, 0, 120, 30.0)
        parts = spec.split(":")
        a, _, b = parts[1].partition("-") if len(parts) > 1 else ("0", "", "")
        rate = float(parts[2]) if len(parts) > 2 else 30.0
        return cls(parts[0] or None, mode, int(a or 0), int(b) if b else None, rate)


STEP_FRAMES_JS = """([first, last, rate]) => new Promise((done) => {
  const r = document.querySelector('.skel-ctl input[type=range]');
  let f = first;
  const show = () => { r.value = String(f); r.dispatchEvent(new Event('input', { bubbles: true })); };
  show();
  const id = setInterval(() => {
    f += 1;
    if (f > last) { clearInterval(id); done(); return; }
    show();
  }, 1000 / rate);
})"""


def scene_skeleton(base, session, clip: SkeletonClip):
    """A processed take in the skeleton viewer, in third or first person, stepping through
    the clip's frames at its rate (the viewer plays on its own, but a chosen range needs
    the slider)."""
    def act(page: Page, clock):
        goto(page, base, session, "mocap", settle=3000)
        row = processed_take_row(page, clip.take)
        if row is None:
            return None
        open_take(page, row)
        label = "First person" if clip.mode == "first" else "Third person"
        page.locator(".skel-mode button", has_text=label).click()
        page.wait_for_timeout(300)
        n = int(page.locator(".skel-ctl input[type=range]").get_attribute("max") or 0)
        last = min(clip.last if clip.last is not None else clip.first + 120, n)
        page.evaluate(STEP_FRAMES_JS, [clip.first, clip.first, clip.rate])      # on the first frame before the clip starts
        page.wait_for_timeout(400)
        start = clock()
        page.evaluate(STEP_FRAMES_JS, [clip.first, last, clip.rate])
        page.wait_for_timeout(700)
        return start
    return act


GIF_WIDTH, GIF_FPS = 880, 12
SYNC_SECONDS, SYNC_SPEED = 8.0, 2.0      # the replay runs ~8 s; shown at 2× it settles in 4
REVIEW_SECONDS = 5.0


def _ffmpeg() -> str | None:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        print("ffmpeg not found: keeping the .webm only", file=sys.stderr)
    return ffmpeg


def _clip_input(src: Path, start: float, seconds: float, speed: float) -> list[str]:
    return ["-ss", f"{start:.3f}", "-t", f"{seconds * speed:.3f}", "-i", str(src)]


def _clip_filter(speed: float, width: int, fps: int) -> str:
    return f"setpts=PTS/{speed},fps={fps},scale={width}:-2:flags=lanczos,setsar=1"


PALETTE = "split[a][b];[a]palettegen=max_colors=128[p];[b][p]paletteuse=dither=bayer:bayer_scale=3"


def to_gif(src: Path, dst: Path, start: float, seconds: float, speed: float = 1.0,
           width: int = GIF_WIDTH, fps: int = GIF_FPS) -> None:
    """``seconds`` of GIF from ``start`` in the clip, the source played ``speed`` times faster."""
    ffmpeg = _ffmpeg()
    if not ffmpeg:
        return
    vf = f"{_clip_filter(speed, width, fps)},{PALETTE}"
    subprocess.run([ffmpeg, "-y", "-v", "error", *_clip_input(src, start, seconds, speed), "-vf", vf, "-loop", "0", str(dst)], check=True)
    print("  ", dst.name, f"{dst.stat().st_size // 1024} KB")


def hero_gif(clips: list[tuple[Path, float, float, float]], dst: Path, width: int = GIF_WIDTH, fps: int = GIF_FPS) -> None:
    """``clips``: (webm, start, seconds, speed) each, one after the other."""
    ffmpeg = _ffmpeg()
    if not ffmpeg or not clips:
        return
    inputs, parts = [], []
    for i, (c, start, seconds, speed) in enumerate(clips):
        inputs += _clip_input(c, start, seconds, speed)
        parts.append(f"[{i}:v]{_clip_filter(speed, width, fps)}[v{i}]")
    chain = "".join(f"[v{i}]" for i in range(len(clips)))
    graph = ";".join(parts) + f";{chain}concat=n={len(clips)}:v=1:a=0,{PALETTE}"
    subprocess.run([ffmpeg, "-y", "-v", "error", *inputs, "-filter_complex", graph, "-loop", "0", str(dst)], check=True)
    print("  ", dst.name, f"{dst.stat().st_size // 1024} KB")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--session", required=True, help="the session to show")
    ap.add_argument("--out", default=str(ROOT / "docs" / "img"))
    ap.add_argument("--gif", action="store_true", help="also record the moving parts and make GIFs")
    ap.add_argument("--only-gif", action="store_true", help="skip the screenshots, just the clips and GIFs")
    ap.add_argument("--third", default=None, help="third-person skeleton clip: take:first-last[:rate]")
    ap.add_argument("--first", default=None, help="first-person skeleton clip: take:first-last[:rate]")
    ap.add_argument("--no-censor", action="store_true", help="skip the mosaic (never for publishing)")
    ap.add_argument("--plain", action="store_true", help="raw screenshots, no floating window")
    ap.add_argument("--width", type=int, default=1920)
    ap.add_argument("--height", type=int, default=1080)
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
            frame = None if args.plain else browser.new_context(device_scale_factor=1).new_page()
            print("screens:")
            capture_screens(page, args.base, args.session, Shooter(out, frame))
            ctx.close()
        if args.gif or args.only_gif:
            print("clips:")
            size = {"width": args.width, "height": args.height}
            third = SkeletonClip.parse("third", args.third)
            first = SkeletonClip.parse("first", args.first)
            scenes = (
                ("sync-replay", scene_sync(args.base, args.session), SYNC_SECONDS / SYNC_SPEED, SYNC_SPEED),
                ("review-playback", scene_review(args.base, args.session), REVIEW_SECONDS, 1.0),
                ("mocap-skeleton", scene_skeleton(args.base, args.session, third), third.seconds + 0.3, 1.0),
                ("mocap-first-person", scene_skeleton(args.base, args.session, first), first.seconds + 0.3, 1.0),
            )
            factory = context_factory if args.no_censor else _censored(context_factory)
            clips = []
            for name, scene, seconds, speed in scenes:
                rec = record_scene(factory, args.base, args.session, out, name, scene, size)
                if rec:
                    clips.append((rec[0], rec[1], seconds, speed))
                    to_gif(rec[0], out / f"{name}.gif", rec[1], seconds, speed)
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
