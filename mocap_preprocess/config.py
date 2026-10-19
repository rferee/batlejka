"""Paths, external tool discovery and shared constants."""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
STATIC_DIR = PACKAGE_DIR / "static"
DEFAULT_SESSIONS_DIR = Path.cwd() / "sessions"

VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".m4v", ".mts", ".m2ts", ".mxf"}

# Standard rates that "nominal fps" snaps to (within 0.3 %).
STANDARD_FPS = [
    "24000/1001", "24", "25", "30000/1001", "30", "48", "50",
    "60000/1001", "60", "100", "120000/1001", "120", "240",
]

DEFAULT_SETTINGS = {
    "target_fps": "30000/1001",
    "target_fps_user_set": False,
    "sync_sample_rate": 8000,
    "proxy_long_side": 640,
    "hwaccel": False,
    "sync_min_score": 0.10,
    "sync_min_ratio": 1.3,
    "sync_window": 1.5,
}

DEFAULT_EXPORT_OPTIONS = {
    "layout": "freemocap",
    "long_side": 1920,
    "encoder": "libx264",
    "crf": 17,
    "audio": True,
    "overwrite": True,
    "parallel": 1,        # files encoded at once (2-3 help when decoding is the bottleneck, e.g. NVENC)
}

PROCESS_WORKERS = 3
WAVE_BIN_SECONDS = 0.02
THUMB_BOX = 480
FEATURE_SIZE = 32

# Hide console windows of ffmpeg/ffprobe on Windows (R6).
CREATE_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0

_sessions_dir: Path = DEFAULT_SESSIONS_DIR


def set_sessions_dir(path: str | os.PathLike) -> Path:
    global _sessions_dir
    _sessions_dir = Path(path).expanduser().resolve()
    _sessions_dir.mkdir(parents=True, exist_ok=True)
    return _sessions_dir


def sessions_dir() -> Path:
    return _sessions_dir


def _find_tool(name: str, env_var: str) -> str | None:
    override = os.environ.get(env_var)
    if override:
        if Path(override).is_file():
            return str(Path(override))
        found = shutil.which(override)
        return found
    return shutil.which(name)


def ffmpeg_path() -> str | None:
    return _find_tool("ffmpeg", "MOCAP_FFMPEG")


def ffprobe_path() -> str | None:
    return _find_tool("ffprobe", "MOCAP_FFPROBE")
