"""Setup panel: what this machine has — ffmpeg/ffprobe, an NVIDIA GPU, GPU decode and
NVENC (both actually tested, not taken from ``ffmpeg -hwaccels``), git, uv, FreeMoCap.

The GPU tests take a few seconds, so their results are cached in
``.tools/gpu_probe.json``, keyed by the ffmpeg path + version and the NVIDIA driver
version; a different ffmpeg build or driver re-runs them. Other code reads that cache
only (``gpu_caps()``, ``preferred_encoder()``): Process › GPU decode and Export ›
h264_nvenc are disabled when their test failed, and new sessions start with NVENC when
it passed.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

from . import config

FFMPEG_INSTALL_HINT = ("Install it with `winget install Gyan.FFmpeg` or `scoop install ffmpeg` and restart, "
                       "or point MOCAP_FFMPEG / MOCAP_FFPROBE at the executables.")

_lock = threading.Lock()
console = logging.getLogger("mocap.setup")
_cache: dict | None = None            # the slow part of get_setup(): tools, GPU, versions
_caps_cache: tuple | None = None      # (mtime, caps) of gpu_probe.json


# ---------------------------------------------------------------------------
# subprocess helpers (list args, no console window, always a timeout — R6)

def _run(args: list, timeout: float = 30) -> subprocess.CompletedProcess | None:
    """Run and capture text; None if the program is missing or timed out."""
    try:
        return subprocess.run([str(a) for a in args], stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=timeout,
                              creationflags=config.CREATE_NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None


def _err_text(r: subprocess.CompletedProcess | None, limit: int = 400) -> str:
    if r is None:
        return "didn't run (missing or timed out)"
    text = (r.stderr or r.stdout or "").strip()
    lines = [ln for ln in text.splitlines() if ln.strip()]
    return (" ".join(lines[-4:]) or f"exit code {r.returncode}")[-limit:]


def _first_line(r: subprocess.CompletedProcess | None) -> str | None:
    if r is None or r.returncode != 0:
        return None
    lines = (r.stdout or r.stderr or "").strip().splitlines()
    return lines[0].strip() if lines else None


# ---------------------------------------------------------------------------
# individual checks

def ffmpeg_info() -> dict:
    ffmpeg, ffprobe = config.ffmpeg_path(), config.ffprobe_path()
    version = None
    if ffmpeg:
        line = _first_line(_run([ffmpeg, "-version"], 20)) or ""
        m = re.match(r"ffmpeg version (\S+)", line)
        version = m.group(1) if m else (line or None)
    return {"ffmpeg": ffmpeg, "ffprobe": ffprobe, "version": version}


def nvidia_gpu() -> dict | None:
    """First NVIDIA GPU from nvidia-smi, or None (no GPU / no driver)."""
    exe = shutil.which("nvidia-smi")
    if not exe:
        return None
    r = _run([exe, "--query-gpu=name,driver_version,memory.total", "--format=csv,noheader"], 20)
    line = _first_line(r)
    if not line:
        return None
    parts = [p.strip() for p in line.split(",")]
    return {"name": parts[0], "driver": parts[1] if len(parts) > 1 else None,
            "memory": parts[2] if len(parts) > 2 else None}


def tool_version(name: str, exe: str | None) -> dict:
    if not exe:
        return {"path": None, "version": None}
    line = _first_line(_run([exe, "--version"], 20))
    return {"path": exe, "version": line}


def uv_path() -> str | None:
    """uv from the app's own venv (requirements.txt pins it), else PATH."""
    try:
        from uv import find_uv_bin

        return str(find_uv_bin())
    except Exception:  # noqa: BLE001 - package missing or no binary
        return shutil.which("uv")


# ---------------------------------------------------------------------------
# GPU probe (cached per ffmpeg build + driver)

def probe_file() -> Path:
    return config.tools_dir() / "gpu_probe.json"


def probe_key(ffm: dict, gpu: dict | None) -> dict:
    return {"ffmpeg": ffm.get("ffmpeg"), "ffmpeg_version": ffm.get("version"),
            "driver": (gpu or {}).get("driver")}


def _test_decode(ffmpeg: str) -> dict:
    tmp = config.tools_dir() / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    clip = tmp / "gpu_probe_src.mp4"
    r = _run([ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=30", "-t", "1",
              "-c:v", "libx264", "-pix_fmt", "yuv420p", clip], 60)
    if r is None or r.returncode != 0 or not clip.is_file():
        return {"ok": False, "error": f"couldn't make the test clip: {_err_text(r)}"}
    try:
        # export uses scale_cuda on frames that stay on the GPU, so test exactly that
        r = _run([ffmpeg, "-v", "error", "-hwaccel", "cuda", "-hwaccel_output_format", "cuda", "-i", clip,
                  "-vf", "scale_cuda=320:-2", "-f", "null", "-"], 60)
    finally:
        clip.unlink(missing_ok=True)
    return {"ok": True, "error": None} if r is not None and r.returncode == 0 else {"ok": False, "error": _err_text(r)}


def _test_nvenc(ffmpeg: str) -> dict:
    r = _run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=30", "-t", "1",
              "-c:v", "h264_nvenc", "-f", "null", "-"], 60)
    return {"ok": True, "error": None} if r is not None and r.returncode == 0 else {"ok": False, "error": _err_text(r)}


def run_gpu_probe(ffm: dict, gpu: dict | None) -> dict:
    if not ffm.get("ffmpeg"):
        res = {"ok": False, "error": "ffmpeg not found"}
        decode = nvenc = res
    elif gpu is None:
        res = {"ok": False, "error": "no NVIDIA GPU found"}
        decode = nvenc = res
    else:
        decode = _test_decode(ffm["ffmpeg"])
        nvenc = _test_nvenc(ffm["ffmpeg"])
    return {"key": probe_key(ffm, gpu), "at": time.time(), "decode": decode, "nvenc": nvenc}


def _read_probe() -> dict | None:
    try:
        with open(probe_file(), "r", encoding="utf-8-sig") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def gpu_probe(ffm: dict, gpu: dict | None, refresh: bool = False) -> dict:
    """The cached probe when it matches this ffmpeg + driver, else a fresh one (written to the cache)."""
    key = probe_key(ffm, gpu)
    cached = None if refresh else _read_probe()
    if cached and cached.get("key") == key and "decode" in cached and "nvenc" in cached:
        return cached
    result = run_gpu_probe(ffm, gpu)
    path = probe_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(result, fh, indent=1)
    os.replace(tmp, path)
    return result


def gpu_caps() -> dict | None:
    """Cached GPU test results — never runs a test. None if never probed."""
    global _caps_cache
    path = probe_file()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return None
    if _caps_cache and _caps_cache[0] == (str(path), mtime):
        return _caps_cache[1]
    data = _read_probe()
    caps = None
    if data and isinstance(data.get("decode"), dict) and isinstance(data.get("nvenc"), dict):
        caps = {"decode": data["decode"], "nvenc": data["nvenc"], "at": data.get("at")}
    _caps_cache = ((str(path), mtime), caps)
    return caps


def preferred_encoder() -> str:
    """Encoder for new sessions: h264_nvenc when its cached test passed."""
    caps = gpu_caps()
    return "h264_nvenc" if caps and caps["nvenc"].get("ok") else "libx264"


# ---------------------------------------------------------------------------
# the panel

def _env():
    from .fmc import env

    return env


def _fmt_time(t) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(t)) if t else "?"


def _job_action(job: dict | None) -> str | None:
    """Which FreeMoCap action an active setup job is (from its label)."""
    if not job:
        return None
    try:
        for action, (_fn, label) in _env().ACTIONS.items():
            if label == job.get("label"):
                return action
    except Exception:  # noqa: BLE001
        pass
    label = (job.get("label") or "").lower()
    for action in ("install", "repair", "remove"):
        if action in label:
            return action
    return "selftest" if "test" in label else None


def _busy_row(base: dict, job: dict) -> dict:
    pct = round(100 * float(job.get("progress") or 0))
    return {**base, "status": "busy", "detail": f"{job.get('label')}: {job.get('message') or 'starting…'} ({pct}%)",
            "progress": job.get("progress") or 0, "job_id": job.get("id"), "actions": []}


def _freemocap_rows(fmc: dict, busy_job: dict | None) -> list[dict]:
    v = fmc.get("version") or config.FMC_VERSION
    state = fmc.get("state")
    action = _job_action(busy_job)
    base = {"id": "freemocap", "label": f"FreeMoCap {v.lstrip('v')}"}
    if action in ("install", "repair", "remove"):
        row = _busy_row(base, busy_job)
    elif state == "ready":
        chk = fmc.get("check") or {}
        bits = [f"FreeMoCap {chk.get('freemocap') or v}"]
        bits += [f"{name} {chk[key]}" for key, name in (("python", "Python"), ("opencv", "OpenCV"),
                                                        ("mediapipe", "MediaPipe")) if chk.get(key)]
        row = {**base, "status": "ok", "detail": " · ".join(bits),
               "hint": f"checked {_fmt_time(chk.get('at'))} · {fmc.get('venv')}",
               "actions": [{"id": "repair", "label": "Repair"}, {"id": "remove", "label": "Remove"}]}
    elif state == "broken":
        row = {**base, "status": "error", "detail": fmc.get("error") or fmc.get("detail") or "broken",
               "hint": "Repair recreates the environment from the lock file.",
               "actions": [{"id": "repair", "label": "Repair"}, {"id": "remove", "label": "Remove"}]}
    else:
        row = {**base, "status": "warn", "detail": f"not installed — {fmc.get('size_hint') or '~3 GB download'}",
               "hint": "Only needed for the Mocap step (calibration and 3D tracking). Installs into .tools/, "
                       "separate from the app and from any FreeMoCap you already have.",
               "actions": [{"id": "install", "label": "Install"}]}

    st = fmc.get("selftest")
    test = {"id": "freemocap_selftest", "label": "FreeMoCap self-test"}
    if action == "selftest":
        test = _busy_row(test, busy_job)
    elif state != "ready":
        test.update(status="neutral", detail="install FreeMoCap first", actions=[])
    elif not st:
        test.update(status="neutral", detail="not run yet — calibrates and tracks FreeMoCap's 3-camera sample "
                                             "(~38 MB download, a few minutes)",
                    actions=[{"id": "selftest", "label": "Run"}])
    else:
        nums = []
        if isinstance(st.get("reprojection_error_px"), (int, float)):
            nums.append(f"reprojection {st['reprojection_error_px']:.2f} px")
        if isinstance(st.get("upper_arm_mm"), (int, float)):
            nums.append(f"upper arm {st['upper_arm_mm']:.0f} mm")
        if isinstance(st.get("thigh_mm"), (int, float)):
            nums.append(f"thigh {st['thigh_mm']:.0f} mm")
        if isinstance(st.get("nan_fraction"), (int, float)):
            nums.append(f"NaN {100 * st['nan_fraction']:.1f}%")
        if isinstance(st.get("seconds"), (int, float)):
            nums.append(f"{st['seconds']:.0f} s")
        word = "passed" if st.get("passed") else "failed"
        detail = f"{word} at {_fmt_time(st.get('at'))}" + (f": {' · '.join(nums)}" if nums else "")
        if not st.get("passed") and st.get("error"):
            detail += f" — {st['error']}"
        test.update(status="ok" if st.get("passed") else "error", detail=detail,
                    actions=[{"id": "selftest", "label": "Run again" if st.get("passed") else "Run"}])
    return [row, test]


def _collect(refresh: bool) -> dict:
    ffm = ffmpeg_info()
    gpu = nvidia_gpu()
    probe = gpu_probe(ffm, gpu, refresh=refresh)
    git = tool_version("git", shutil.which("git"))
    uv = tool_version("uv", uv_path())
    return {"at": time.time(), "ffmpeg": ffm, "gpu": gpu, "probe": probe, "git": git, "uv": uv}


def _static_rows(c: dict, fmc_installed: bool) -> list[dict]:
    rows = []
    ffm = c["ffmpeg"]
    if ffm["ffmpeg"] and ffm["ffprobe"]:
        rows.append({"id": "ffmpeg", "label": "ffmpeg / ffprobe", "status": "ok",
                     "detail": f"ffmpeg {ffm['version'] or '?'}", "hint": f"{ffm['ffmpeg']} · {ffm['ffprobe']}"})
    else:
        missing = " and ".join(n for n in ("ffmpeg", "ffprobe") if not ffm[n])
        rows.append({"id": "ffmpeg", "label": "ffmpeg / ffprobe", "status": "error",
                     "detail": f"{missing} not found", "hint": FFMPEG_INSTALL_HINT})

    gpu = c["gpu"]
    rows.append({"id": "gpu", "label": "NVIDIA GPU", "status": "ok" if gpu else "neutral",
                 "detail": (f"{gpu['name']} · driver {gpu['driver']} · {gpu['memory']}" if gpu
                            else "none found — everything runs on the CPU")})

    probe = c["probe"]
    for rid, label, key, what in (("gpu_decode", "GPU decode", "decode", "-hwaccel cuda + scale_cuda"),
                                  ("nvenc", "NVENC", "nvenc", "h264_nvenc encoding")):
        res = probe.get(key) or {}
        if res.get("ok"):
            rows.append({"id": rid, "label": label, "status": "ok", "detail": f"works ({what})"})
        elif gpu is None or not ffm["ffmpeg"]:
            rows.append({"id": rid, "label": label, "status": "neutral",
                         "detail": f"unavailable — {res.get('error') or 'needs an NVIDIA GPU'}"})
        else:
            rows.append({"id": rid, "label": label, "status": "warn", "detail": f"test failed: {res.get('error')}",
                         "hint": "Usually an old NVIDIA driver or an ffmpeg build without CUDA. The CPU path still works."})

    for rid, label in (("git", "git"), ("uv", "uv")):
        t = c[rid]
        if t["version"]:
            rows.append({"id": rid, "label": label, "status": "ok", "detail": t["version"], "hint": t["path"]})
        else:
            hint = ("needed to install FreeMoCap (its lock file pulls it from GitHub): `winget install Git.Git`"
                    if rid == "git" else "needed to install FreeMoCap: `.venv\\Scripts\\pip install -r requirements.txt` "
                                         "(pins uv)")
            rows.append({"id": rid, "label": label, "status": "neutral" if fmc_installed else "warn",
                         "detail": "not found", "hint": hint})
    return rows


def get_setup(refresh: bool = False, busy_job: dict | None = None) -> dict:
    """The Setup panel. The slow checks are cached in memory; ``refresh`` re-runs all of
    them (GPU tests included) and FreeMoCap's own check when it is installed."""
    global _cache
    with _lock:
        if refresh or _cache is None:
            t0 = time.perf_counter()
            console.info("checking the setup: ffmpeg, GPU, git, uv%s...", " (GPU tests again)" if refresh else "")
            _cache = _collect(refresh)
            console.info("setup checked in %.1f s", time.perf_counter() - t0)
        c = _cache
    env = _env()
    if refresh and busy_job is None and hasattr(env, "check"):
        try:
            if env.status().get("state") != "not_installed":
                env.check()
        except Exception:  # noqa: BLE001 - the check's failure shows up in env.status()
            pass
    try:
        fmc = dict(env.status())
    except Exception as e:  # noqa: BLE001
        fmc = {"version": config.FMC_VERSION, "state": "broken", "error": f"status failed: {e!r}"}
    fmc["busy"] = busy_job
    rows = _static_rows(c, fmc.get("state") not in (None, "not_installed")) + _freemocap_rows(fmc, busy_job)
    probe = c["probe"]
    return {
        "checked_at": c["at"],
        "ok": not any(r["status"] in ("warn", "error") for r in rows),
        "rows": rows,
        "gpu": {"decode": probe.get("decode"), "nvenc": probe.get("nvenc"), "gpu": c["gpu"], "at": probe.get("at")},
        "freemocap": fmc,
    }


def reset_cache() -> None:
    """Forget the in-memory results (tests)."""
    global _cache, _caps_cache
    with _lock:
        _cache = None
        _caps_cache = None
