"""Paths, external tool discovery and shared constants."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_DIR = PACKAGE_DIR.parent
STATIC_DIR = PACKAGE_DIR / "static"
DEFAULT_SESSIONS_DIR = Path.cwd() / "sessions"

# FreeMoCap integration: a pinned release in its own venv under .tools/ (see fmc/).
FMC_VERSION = "v1.8.2"
FMC_COMMIT = "a1cd374ac7c186d78d0552b8f69bf59be2f4bdcc"
FMC_PACKAGE_DIR = PACKAGE_DIR / "fmc"          # lock files + fmc_headless.py (runs in the FreeMoCap venv)


def tools_dir() -> Path:
    """Per-machine tools (uv's Python, the FreeMoCap env, GPU probe cache); MOCAP_TOOLS_DIR overrides."""
    return Path(os.environ.get("MOCAP_TOOLS_DIR") or REPO_DIR / ".tools").resolve()


def fmc_dir() -> Path:
    return tools_dir() / f"freemocap-{FMC_VERSION}"

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
    "nvenc_preset": "p5",  # p5 quality … p3 about twice as fast at 4K
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


_tools_found: dict[tuple[str, str | None], str] = {}


def _find_tool(name: str, env_var: str) -> str | None:
    """Where ``name`` is (the env var's path or program wins). A hit is remembered as long as
    the file is still there, so the thousands of ffmpeg/ffprobe runs of a session don't each
    walk PATH; a miss is looked up again every time, so installing ffmpeg needs no restart."""
    override = os.environ.get(env_var)
    key = (name, override)
    found = _tools_found.get(key)
    if found and Path(found).is_file():
        return found
    if override:
        found = str(Path(override)) if Path(override).is_file() else shutil.which(override)
    else:
        found = shutil.which(name)
    if found:
        _tools_found[key] = found
    return found


def ffmpeg_path() -> str | None:
    return _find_tool("ffmpeg", "MOCAP_FFMPEG")


def ffprobe_path() -> str | None:
    return _find_tool("ffprobe", "MOCAP_FFPROBE")


def open_in_os(path: Path) -> None:
    """Show a folder, or open a file with the program the OS associates with it."""
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606 - local tool; callers check the path is theirs
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
