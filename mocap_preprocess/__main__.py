"""CLI entry point: ``python -m mocap_preprocess``.

The console says what the server is doing: each startup step, a "ready" line once it
actually listens (the browser opens then, not on a timer), then background jobs and the
changes made from the UI (server.py, jobs.py).
"""
from __future__ import annotations

import argparse
import logging
import platform
import threading
import time
import webbrowser

from . import config

log = logging.getLogger("mocap")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mocap_preprocess",
                                     description="Batlejka: sync and cut multi-camera footage, then run FreeMoCap on it.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--sessions-dir", default="./sessions", help="folder holding <session>/source/")
    parser.add_argument("--no-browser", action="store_true", help="don't open the UI in a browser")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.WARNING, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    log.setLevel(logging.INFO)
    log.info("Batlejka starting (Python %s)", platform.python_version())

    sessions = config.set_sessions_dir(args.sessions_dir)
    found = sorted(d.name for d in sessions.iterdir() if d.is_dir() and not d.name.startswith(".")) if sessions.is_dir() else []
    log.info("sessions in %s: %s", sessions, ", ".join(found) if found else "none yet")
    ffmpeg, ffprobe = config.ffmpeg_path(), config.ffprobe_path()
    if ffmpeg and ffprobe:
        log.info("ffmpeg: %s", ffmpeg)
    else:
        log.warning("WARNING: ffmpeg/ffprobe not found on PATH (set MOCAP_FFMPEG / MOCAP_FFPROBE)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        log.warning("WARNING: binding to a non-loopback address exposes your footage to the network")

    log.info("loading the server...")
    t0 = time.perf_counter()
    import uvicorn

    from .server import app
    log.info("server loaded in %.1f s, starting on port %d...", time.perf_counter() - t0, args.port)

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level="warning",
                                           access_log=False, log_config=None))

    def announce() -> None:
        while not server.started and not server.should_exit:
            time.sleep(0.05)
        if server.started:
            log.info("ready: %s  (Ctrl+C to stop)", url)
            if not args.no_browser:
                webbrowser.open(url)

    threading.Thread(target=announce, name="announce-ready", daemon=True).start()
    server.run()
    log.info("stopped")


if __name__ == "__main__":
    main()
