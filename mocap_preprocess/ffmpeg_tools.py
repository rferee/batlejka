"""ffprobe/ffmpeg wrappers: probing, frame grabs, audio extraction, proxy
command building and ``-progress`` parsing.

Every subprocess is started with a list of arguments (never ``shell=True``)
and with ``CREATE_NO_WINDOW`` on Windows (R6).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
from collections import deque
from datetime import datetime, timezone
from fractions import Fraction
from pathlib import Path
from typing import Callable

import numpy as np

from . import config


class ToolMissing(RuntimeError):
    pass


class FFmpegError(RuntimeError):
    pass


class Cancelled(Exception):
    """Raised when a job was cancelled by the user."""


# ---------------------------------------------------------------------------
# process helpers

def _tool(name: str) -> str:
    path = config.ffmpeg_path() if name == "ffmpeg" else config.ffprobe_path()
    if not path:
        env = "MOCAP_FFMPEG" if name == "ffmpeg" else "MOCAP_FFPROBE"
        raise ToolMissing(f"{name} not found on PATH (or set {env})")
    return path


def _cmd(args: list) -> list[str]:
    return [_tool(str(args[0]))] + [str(a) for a in args[1:]]


def run(args: list, timeout: float | None = None) -> subprocess.CompletedProcess:
    """Run ffmpeg/ffprobe (``args[0]`` is the tool name) and capture bytes."""
    return subprocess.run(
        _cmd(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        creationflags=config.CREATE_NO_WINDOW,
    )


def tool_version(name: str) -> str | None:
    try:
        r = run([name, "-version"], timeout=20)
    except (ToolMissing, OSError, subprocess.SubprocessError):
        return None
    first = r.stdout.decode("utf-8", "replace").splitlines()[:1]
    return first[0].strip() if first else None


def _stderr_text(r: subprocess.CompletedProcess, limit: int = 600) -> str:
    text = (r.stderr or b"").decode("utf-8", "replace").strip()
    return text[-limit:]


def run_progress(
    args: list,
    duration: float,
    on_progress: Callable[[float], None] | None = None,
    cancel: threading.Event | None = None,
) -> tuple[int, str]:
    """Run an ffmpeg command that has ``-progress pipe:1 -nostats``.

    Calls ``on_progress(fraction)`` from ``out_time_us`` vs. ``duration``.
    Kills ffmpeg and raises :class:`Cancelled` when ``cancel`` is set.
    Returns ``(returncode, stderr_tail)``.
    """
    proc = subprocess.Popen(
        _cmd(args),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        creationflags=config.CREATE_NO_WINDOW,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    tail: deque[str] = deque(maxlen=40)
    cancelled = threading.Event()

    def read_stderr():
        for line in proc.stderr:
            tail.append(line.rstrip())

    def watch_cancel():
        while proc.poll() is None:
            if cancel.wait(0.2):
                cancelled.set()
                try:
                    proc.kill()
                except OSError:
                    pass
                return

    t_err = threading.Thread(target=read_stderr, daemon=True)
    t_err.start()
    if cancel is not None:
        threading.Thread(target=watch_cancel, daemon=True).start()

    duration = max(float(duration or 0), 1e-6)
    for line in proc.stdout:
        key, _, value = line.strip().partition("=")
        if key in ("out_time_us", "out_time_ms"):  # both are microseconds
            try:
                t = int(value) / 1e6
            except ValueError:
                continue
            if on_progress:
                on_progress(min(1.0, max(0.0, t / duration)))
        elif key == "progress" and value == "end" and on_progress:
            on_progress(1.0)
    rc = proc.wait()
    t_err.join(timeout=2)
    if cancelled.is_set():
        raise Cancelled()
    return rc, "\n".join(tail)


# ---------------------------------------------------------------------------
# numbers

def parse_fraction(value) -> Fraction | None:
    if value is None:
        return None
    try:
        if isinstance(value, (int, float)):
            f = Fraction(value).limit_denominator(100000)
        else:
            s = str(value).strip()
            if "/" in s:
                num, den = s.split("/", 1)
                if int(den) == 0:
                    return None
                f = Fraction(int(num), int(den))
            else:
                f = Fraction(s)
    except (ValueError, ZeroDivisionError):
        return None
    return f if f > 0 else None


def frac_str(f: Fraction) -> str:
    return str(f.numerator) if f.denominator == 1 else f"{f.numerator}/{f.denominator}"


def fps_float(value) -> float | None:
    f = parse_fraction(value)
    return float(f) if f else None


def snap_fps(value) -> str | None:
    """Snap a measured rate to the nearest standard rate within 0.3 %."""
    f = parse_fraction(value)
    if not f:
        return None
    v = float(f)
    best = min(config.STANDARD_FPS, key=lambda s: abs(float(Fraction(s)) - v))
    if abs(v / float(Fraction(best)) - 1) <= 0.003:
        return best
    return frac_str(f)


def fps_label(value) -> str:
    v = fps_float(value)
    if v is None:
        return "?"
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.3f}".rstrip("0").rstrip(".")


_TZ_RE = re.compile(r"([+-])(\d{2}):?(\d{2})$")


def parse_time(value) -> float | None:
    """Parse an ISO 8601 timestamp (any common camera variant) to epoch seconds."""
    if not value:
        return None
    s = str(value).strip()
    if re.match(r"^\d{4}-\d{2}-\d{2} \d", s):
        s = s.replace(" ", "T", 1)
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    tpos = s.find("T")
    m = _TZ_RE.search(s)
    if m and tpos >= 0 and m.start() > tpos:
        s = f"{s[:m.start()]}{m.group(1)}{m.group(2)}:{m.group(3)}"
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    ts = dt.timestamp()
    if ts < 1e8:  # 1904/1970 "zero" dates written by some cameras
        return None
    return ts


def _rotation_cw(stream: dict) -> int:
    """Clockwise display rotation in {0, 90, 180, 270}.

    ffprobe's display-matrix ``rotation`` is counter-clockwise; the legacy
    ``rotate`` tag is clockwise.
    """
    deg = None
    for sd in stream.get("side_data_list") or []:
        if "rotation" in sd:
            try:
                deg = -float(sd["rotation"])
            except (TypeError, ValueError):
                pass
            break
    if deg is None:
        tags = {k.lower(): v for k, v in (stream.get("tags") or {}).items()}
        if "rotate" in tags:
            try:
                deg = float(tags["rotate"])
            except (TypeError, ValueError):
                deg = None
    if deg is None:
        return 0
    return int(round(deg / 90.0)) * 90 % 360


def _float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=None):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# probe

def probe(path: str | os.PathLike) -> dict:
    r = run(["ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", path], timeout=120)
    if r.returncode != 0:
        raise FFmpegError(_stderr_text(r) or f"ffprobe failed ({r.returncode})")
    data = json.loads(r.stdout.decode("utf-8", "replace") or "{}")
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    ftags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}

    vstream = next(
        (s for s in streams
         if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")),
        None,
    )
    astream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    format_start = _float(fmt.get("start_time"), 0.0) or 0.0
    duration = _float(fmt.get("duration"))
    if duration is None and vstream is not None:
        duration = _float(vstream.get("duration"))
    duration = duration or 0.0

    video = None
    vtags: dict = {}
    if vstream is not None:
        vtags = {k.lower(): v for k, v in (vstream.get("tags") or {}).items()}
        r_fps = vstream.get("r_frame_rate") or "0/0"
        avg_fps = vstream.get("avg_frame_rate") or "0/0"
        rf, af = parse_fraction(r_fps), parse_fraction(avg_fps)
        if af is None:
            af = rf
            avg_fps = r_fps
        rot = _rotation_cw(vstream)
        w, h = _int(vstream.get("width"), 0), _int(vstream.get("height"), 0)
        dw, dh = (h, w) if rot in (90, 270) else (w, h)
        video = {
            "codec": vstream.get("codec_name"),
            "width": w,
            "height": h,
            "display_width": dw,
            "display_height": dh,
            "rotation_meta": rot,
            "r_fps": r_fps,
            "avg_fps": avg_fps,
            "nominal_fps": snap_fps(af) if af else None,
            "vfr_suspect": bool(rf and af and abs(float(rf) / float(af) - 1) > 0.002),
            "nb_frames": _int(vstream.get("nb_frames")),
            "duration": _float(vstream.get("duration")),
            "start": _float(vstream.get("start_time"), format_start),
            "pix_fmt": vstream.get("pix_fmt"),
        }

    audio = None
    if astream is not None:
        audio = {
            "codec": astream.get("codec_name"),
            "sample_rate": _int(astream.get("sample_rate")),
            "channels": _int(astream.get("channels")),
            "start": _float(astream.get("start_time"), format_start),
        }

    creation_time = None
    for key in ("com.apple.quicktime.creationdate", "creation_time"):
        creation_time = parse_time(ftags.get(key))
        if creation_time is not None:
            break
    if creation_time is None:
        creation_time = parse_time(vtags.get("creation_time"))

    def tag(*keys):
        for k in keys:
            v = ftags.get(k)
            if v:
                return str(v).strip()
        return None

    return {
        "duration": duration,
        "format_start": format_start,
        "video": video,
        "audio": audio,
        "creation_time": creation_time,
        "make": tag("com.apple.quicktime.make", "make", "com.android.manufacturer"),
        "model": tag("com.apple.quicktime.model", "model", "com.android.model"),
        "encoder": tag("encoder") or vtags.get("encoder"),
        "handler": vtags.get("handler_name"),
    }


# ---------------------------------------------------------------------------
# frame grabs and audio

def replace_with_retry(src: Path, dst: Path, attempts: int = 12, delay: float = 0.05) -> None:
    """``os.replace`` with retries: Windows raises PermissionError while
    another process (antivirus, indexer, a browser streaming the file) holds it."""
    import time

    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(delay * (i + 1))


def grab_jpeg(src: Path, t: float, dst: Path, box: int = config.THUMB_BOX) -> bool:
    """One frame at ``t`` seconds, scaled to fit ``box``×``box``."""
    tmp = dst.with_name(dst.stem + ".tmp.jpg")
    vf = f"scale={box}:{box}:force_original_aspect_ratio=decrease:force_divisible_by=2"
    for when in (max(0.0, t), 0.0):
        r = run(["ffmpeg", "-v", "error", "-y", "-ss", f"{when:.3f}", "-i", src,
                 "-frames:v", "1", "-vf", vf, "-q:v", "3", "-update", "1", tmp], timeout=120)
        if r.returncode == 0 and tmp.exists() and tmp.stat().st_size > 0:
            replace_with_retry(tmp, dst)
            return True
    tmp.unlink(missing_ok=True)
    return False


def grab_gray(src: Path, t: float, size: int = config.FEATURE_SIZE) -> np.ndarray | None:
    """One frame at ``t`` scaled to ``size``×``size`` grayscale (raw on stdout)."""
    r = run(["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.3f}", "-i", src, "-frames:v", "1",
             "-vf", f"scale={size}:{size}:flags=area,format=gray", "-f", "rawvideo", "-"], timeout=120)
    n = size * size
    if r.returncode != 0 or len(r.stdout) < n:
        return None
    return np.frombuffer(r.stdout[:n], dtype=np.uint8).reshape(size, size).astype(np.float32)


def extract_audio(src: Path, sample_rate: int) -> np.ndarray:
    """First audio stream as mono float32 at ``sample_rate``."""
    r = run(["ffmpeg", "-v", "error", "-i", src, "-vn", "-map", "0:a:0", "-ac", "1",
             "-ar", str(int(sample_rate)), "-f", "f32le", "-"])
    if r.returncode != 0:
        raise FFmpegError(_stderr_text(r) or "audio extraction failed")
    return np.frombuffer(r.stdout, dtype="<f4").astype(np.float32)


def scale_expr(long_side: int) -> str:
    """Fit the long side to ``long_side`` (downscale only), keep orientation."""
    L = int(long_side)
    return f"scale=w='if(gte(iw,ih),min({L},iw),-2)':h='if(gte(iw,ih),-2,min({L},ih))'"


def proxy_args(src: Path, dst_tmp: Path, long_side: int, hwaccel: bool) -> list:
    args: list = ["ffmpeg", "-y", "-v", "error"]
    if hwaccel:
        args += ["-hwaccel", "cuda"]
    args += [
        "-i", src, "-map", "0:v:0", "-map", "0:a:0?",
        "-vf", scale_expr(long_side),
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-g", "15", "-keyint_min", "15", "-sc_threshold", "0",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-ac", "1", "-b:a", "96k",
        "-movflags", "+faststart", "-progress", "pipe:1", "-nostats", dst_tmp,
    ]
    return args


def count_video_frames(path: Path) -> int | None:
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-count_packets",
             "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", path], timeout=300)
    if r.returncode != 0:
        return None
    text = r.stdout.decode("utf-8", "replace").strip().split(",")[0].strip()
    return _int(text)


def video_stream_info(path: Path) -> dict | None:
    r = run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
             "stream=width,height,r_frame_rate,avg_frame_rate", "-of", "json", path], timeout=60)
    if r.returncode != 0:
        return None
    streams = json.loads(r.stdout.decode("utf-8", "replace") or "{}").get("streams") or []
    return streams[0] if streams else None
