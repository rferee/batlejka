"""CLI entry point: ``python -m mocap_preprocess``."""
from __future__ import annotations

import argparse
import threading
import webbrowser

from . import config


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="mocap_preprocess",
                                     description="Sync and cut multi-camera mocap footage for FreeMoCap.")
    parser.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--sessions-dir", default="./sessions", help="folder holding <session>/source/")
    parser.add_argument("--no-browser", action="store_true", help="don't open the UI in a browser")
    args = parser.parse_args(argv)

    sessions = config.set_sessions_dir(args.sessions_dir)

    import uvicorn

    from .server import app

    url = f"http://{'127.0.0.1' if args.host in ('0.0.0.0', '::') else args.host}:{args.port}/"
    print(f"mocap-preprocess: sessions in {sessions}")
    print(f"mocap-preprocess: open {url}")
    if not config.ffmpeg_path() or not config.ffprobe_path():
        print("WARNING: ffmpeg/ffprobe not found on PATH (set MOCAP_FFMPEG / MOCAP_FFPROBE)")
    if args.host not in ("127.0.0.1", "localhost", "::1"):
        print("WARNING: binding to a non-loopback address exposes your footage to the network")
    if not args.no_browser:
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
