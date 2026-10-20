"""FreeMoCap environment: install / repair / remove / check / self-test.

FreeMoCap v1.8.2 lives in its own venv under ``.tools/freemocap-v1.8.2/``, built by uv
(the pinned ``uv`` in the app's .venv) on a uv-managed Python 3.12 in ``.tools/python/``
from the committed lock ``fmc/requirements-freemocap.lock``. Nothing here imports
FreeMoCap; it runs through ``runner`` (``fmc_headless.py`` in the FreeMoCap venv).

Per-machine state (install time, last check, last self-test) is in
``.tools/freemocap-v1.8.2/state.json``, never in a session.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import threading
import time
from collections import deque
from pathlib import Path

from .. import config
from ..ffmpeg_tools import Cancelled, kill_tree
from ..session_store import write_json_atomic
from . import runner

LOCK_FILE = config.FMC_PACKAGE_DIR / "requirements-freemocap.lock"
PYTHON_VERSION = "3.12"
MIN_FREE_BYTES = 6 * 1024 ** 3
SIZE_HINT = "~3 GB download"

_state_lock = threading.Lock()


# ---------------------------------------------------------------------------
# paths and state

def python_dir() -> Path:
    return config.tools_dir() / "python"


def home_dir() -> Path:
    return config.fmc_dir() / "home"


def logs_dir() -> Path:
    return config.fmc_dir() / "logs"


def selftest_dir() -> Path:
    return config.fmc_dir() / "selftest"


def state_path() -> Path:
    return config.fmc_dir() / "state.json"


def load_state() -> dict:
    try:
        with open(state_path(), "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def update_state(**fields) -> dict:
    with _state_lock:
        data = load_state()
        data.update(fields)
        data["version"] = config.FMC_VERSION
        data["commit"] = config.FMC_COMMIT
        write_json_atomic(state_path(), data)
        return data


def uv_bin() -> str | None:
    """The uv pinned in requirements.txt (in the app's .venv), else one on PATH."""
    try:
        from uv import find_uv_bin

        return find_uv_bin()
    except Exception:  # noqa: BLE001 - package missing or no binary
        return shutil.which("uv")


def uv_cache_dir() -> Path:
    return config.tools_dir() / "uv-cache"


def uv_env() -> dict:
    env = dict(os.environ)
    env["UV_PYTHON_INSTALL_DIR"] = str(python_dir())
    # uv's default cache is on C: (%LOCALAPPDATA%); the env is several GB of wheels, so keep
    # it next to the venv: same drive, so uv can hardlink instead of copying.
    env["UV_CACHE_DIR"] = str(uv_cache_dir())
    env["UV_PYTHON_PREFERENCE"] = "only-managed"   # never pick up a system Python
    env["NO_COLOR"] = "1"
    env.pop("VIRTUAL_ENV", None)                    # the app's own venv must not leak into uv
    return env


# ---------------------------------------------------------------------------
# status

def status() -> dict:
    st = load_state()
    python = runner.venv_python()
    check = st.get("check")
    out = {
        "version": config.FMC_VERSION, "state": "not_installed", "detail": "not installed", "error": None,
        "installed_at": st.get("installed_at"), "check": check, "selftest": st.get("selftest"),
        "venv": str(runner.venv_dir()), "size_hint": SIZE_HINT,
    }
    if not python.is_file():
        if st.get("installed_at") or runner.venv_dir().exists():
            out.update(state="broken", detail="the FreeMoCap env is incomplete — Repair it",
                       error=f"{python} is missing")
        return out
    if not check:
        out.update(state="broken", detail="installed but never checked — Repair or Re-check",
                   error="no check result")
    elif not check.get("ok"):
        out.update(state="broken", detail="the FreeMoCap check failed — Repair it", error=check.get("error"))
    else:
        out.update(state="ready", detail=f"FreeMoCap {check.get('freemocap')} · Python {check.get('python')} · "
                                          f"OpenCV {check.get('opencv')} · MediaPipe {check.get('mediapipe')}")
    return out


def ready() -> bool:
    return status()["state"] == "ready"


def _setup_busy() -> bool:
    try:
        from ..app_state import jobs

        return bool(jobs.active(None, "setup"))
    except Exception:  # noqa: BLE001
        return False


def check(job=None) -> dict:
    """Run ``fmc_headless.py check`` and remember the result in state.json.

    A check from outside a setup job (``job`` None, e.g. the Setup panel's Re-check) is
    skipped while a setup job runs: mid-install it would find a half-built venv and
    record a failure. The stored result is returned instead."""
    at = time.time()
    if job is None and _setup_busy():
        return load_state().get("check") or {"ok": False, "at": at, "error": "a FreeMoCap setup job is running"}
    if not runner.venv_python().is_file():
        return {"ok": False, "at": at, "error": "FreeMoCap isn't installed"}
    try:
        res = runner.run_headless("check", [], job)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001
        res = {"ok": False, "error": str(e)}
    result = {
        "ok": bool(res.get("ok")), "at": at,
        "freemocap": res.get("freemocap"), "python": res.get("python"),
        "opencv": res.get("opencv"), "mediapipe": res.get("mediapipe"),
        "error": None if res.get("ok") else (res.get("error") or "check failed"),
    }
    update_state(check=result)
    return result


# ---------------------------------------------------------------------------
# subprocess steps

def _run_step(job, argv: list, label: str, log, env: dict | None = None, on_line=None) -> None:
    """Run one install step, streaming its output to the job log (debug) and the install log;
    cancellable; raises RuntimeError with the output tail when it fails."""
    job.log(f"{label}: {subprocess.list2cmdline([str(a) for a in argv])}", "debug")
    log.write(f"\n$ {subprocess.list2cmdline([str(a) for a in argv])}\n")
    log.flush()
    proc = subprocess.Popen(
        [str(a) for a in argv], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace", bufsize=1, env=env or uv_env(),
        cwd=str(config.REPO_DIR), creationflags=config.CREATE_NO_WINDOW,
    )
    tail: deque[str] = deque(maxlen=30)
    cancelled = threading.Event()

    def watch():
        while proc.poll() is None:
            if job.cancel_event.wait(0.25):
                cancelled.set()
                kill_tree(proc)
                return

    threading.Thread(target=watch, daemon=True).start()
    for raw in proc.stdout:
        line = runner.strip_ansi(raw).rstrip()
        log.write(line + "\n")
        if not line.strip():
            continue
        tail.append(line)
        job.log(line, "debug")
        if on_line:
            on_line(line)
    rc = proc.wait()
    log.flush()
    if cancelled.is_set():
        raise Cancelled()
    if rc != 0:
        raise RuntimeError(f"{label} failed (exit code {rc}): " + " | ".join(list(tail)[-8:]))


def _lock_packages() -> int:
    n = 0
    for line in LOCK_FILE.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and ("==" in s or " @ " in s):
            n += 1
    return n


def _preflight(job) -> str:
    job.set_progress(0.0, "checking git, uv and disk space")
    uv = uv_bin()
    if not uv:
        raise RuntimeError("uv not found — run run.bat once (it installs requirements.txt, which pins uv)")
    git = shutil.which("git")
    if not git:
        raise RuntimeError("git not found on PATH — FreeMoCap is installed from GitHub (git+https), "
                           "install Git for Windows and restart the app")
    if not LOCK_FILE.is_file():
        raise RuntimeError(f"lock file missing: {LOCK_FILE}")
    base = config.tools_dir()
    probe = base
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    free = shutil.disk_usage(probe).free
    if free < MIN_FREE_BYTES:
        raise RuntimeError(f"only {free / 1024 ** 3:.1f} GB free on {probe.anchor or probe} — "
                           f"the FreeMoCap env needs about 3 GB, keep at least 6 GB free")
    job.log(f"uv: {uv} · git: {git} · {free / 1024 ** 3:.0f} GB free", "info")
    return uv


def _install_python(job, uv: str, log) -> None:
    job.check_cancel()
    job.set_progress(0.02, f"installing Python {PYTHON_VERSION} (uv-managed)")
    job.log(f"Python {PYTHON_VERSION} → {python_dir()}")
    python_dir().mkdir(parents=True, exist_ok=True)
    _run_step(job, [uv, "python", "install", PYTHON_VERSION], "uv python install", log)


def _create_venv(job, uv: str, log) -> None:
    job.check_cancel()
    job.set_progress(0.08, "creating the FreeMoCap venv")
    venv = runner.venv_dir()
    if runner.venv_python().is_file():
        job.log(f"venv exists: {venv} (synced below)")
        return
    job.log(f"venv → {venv}")
    venv.parent.mkdir(parents=True, exist_ok=True)
    _run_step(job, [uv, "venv", "--python", PYTHON_VERSION, str(venv)], "uv venv", log)


def _sync(job, uv: str, log) -> None:
    job.check_cancel()
    total = max(1, _lock_packages())
    job.set_progress(0.10, f"installing {total} packages from the lock (this is the big download)")
    job.log(f"installing {total} pinned packages (FreeMoCap {config.FMC_VERSION}, opencv-contrib 4.8.1.78, no opencv-python)")
    seen = {"prep": 0, "inst": 0}

    def on_line(line: str) -> None:
        s = line.strip()
        if s.startswith("+ "):
            seen["inst"] += 1
            job.set_progress(0.80 + 0.10 * min(1.0, seen["inst"] / total), f"installed {seen['inst']}/{total}")
        elif s.startswith(("Downloaded", "Built")):   # one per package that isn't in uv's cache yet
            seen["prep"] += 1
            job.set_progress(min(0.78, 0.12 + 0.66 * seen["prep"] / total), s[:120])
        elif s.startswith(("Downloading", "Building", "Updating", "Updated")):
            job.set_progress(job.progress, s[:120])
        elif s.startswith("Resolved"):
            job.set_progress(max(job.progress, 0.12), s)
        elif s.startswith("Prepared"):
            job.set_progress(0.80, s)

    _run_step(job, [uv, "pip", "sync", "--python", str(runner.venv_python()), str(LOCK_FILE)],
              "uv pip sync", log, on_line=on_line)


def _final_check(job) -> dict:
    job.check_cancel()
    job.set_progress(0.92, "checking FreeMoCap (first import is slow)")
    res = check(job)
    if not res["ok"]:
        raise RuntimeError(f"FreeMoCap check failed: {res.get('error')}")
    job.log(f"FreeMoCap {res['freemocap']} · Python {res['python']} · OpenCV {res['opencv']} · "
            f"MediaPipe {res['mediapipe']}", "done")
    return res


def _open_log(name: str):
    logs_dir().mkdir(parents=True, exist_ok=True)
    path = logs_dir() / f"{time.strftime('%Y%m%d-%H%M%S')}_{name}.log"
    return open(path, "w", encoding="utf-8", errors="replace"), path


# ---------------------------------------------------------------------------
# jobs

def install(job) -> dict:
    t0 = time.time()
    uv = _preflight(job)
    log, path = _open_log("install")
    job.log(f"install log: {path}", "debug")
    with log:
        _install_python(job, uv, log)
        _create_venv(job, uv, log)
        _sync(job, uv, log)
    res = _final_check(job)
    update_state(installed_at=time.time())
    secs = round(time.time() - t0, 1)
    job.set_progress(1.0, f"FreeMoCap {res['freemocap']} ready")
    job.log(f"installed in {secs} s", "done")
    return {"check": res, "seconds": secs}


def _safe_rmtree(path: Path, must_be_under: Path) -> None:
    path = path.resolve()
    root = must_be_under.resolve()
    if path != root and root not in path.parents:
        raise RuntimeError(f"refusing to delete {path}: not under {root}")
    if path.is_symlink() or (os.name == "nt" and _is_junction(path)):
        raise RuntimeError(f"refusing to delete {path}: it's a link")

    def onexc(func, p, _exc):   # read-only files (e.g. from git) on Windows
        try:
            os.chmod(p, stat.S_IWRITE)
            func(p)
        except OSError:
            raise

    if path.exists():
        try:
            shutil.rmtree(path, onexc=onexc)
        except TypeError:   # Python < 3.12
            shutil.rmtree(path, onerror=onexc)


def _is_junction(path: Path) -> bool:
    try:
        return bool(getattr(os.path, "isjunction", lambda _p: False)(path))
    except OSError:
        return False


def repair(job) -> dict:
    t0 = time.time()
    uv = _preflight(job)
    venv = runner.venv_dir()
    job.set_progress(0.05, "deleting the old venv")
    job.log(f"deleting {venv}")
    _safe_rmtree(venv, config.fmc_dir())
    log, path = _open_log("repair")
    job.log(f"repair log: {path}", "debug")
    with log:
        _install_python(job, uv, log)   # cheap no-op when present; restores a deleted .tools/python
        _create_venv(job, uv, log)
        _sync(job, uv, log)
    res = _final_check(job)
    st = load_state()
    update_state(installed_at=st.get("installed_at") or time.time(), repaired_at=time.time())
    secs = round(time.time() - t0, 1)
    job.set_progress(1.0, f"FreeMoCap {res['freemocap']} ready")
    job.log(f"repaired in {secs} s", "done")
    return {"check": res, "seconds": secs}


def remove(job) -> dict:
    d = config.fmc_dir().resolve()
    tools = config.tools_dir().resolve()
    if d.parent != tools or d.name != f"freemocap-{config.FMC_VERSION}":
        raise RuntimeError(f"refusing to delete unexpected path {d}")
    job.set_progress(0.1, f"deleting {d}")
    job.log(f"deleting {d}")
    _safe_rmtree(d, tools)
    job.set_progress(1.0, "FreeMoCap removed")
    job.log("FreeMoCap removed", "done")
    return {"removed": str(d)}


def selftest(job) -> dict:
    """Download FreeMoCap's 3-camera sample, calibrate one copy, process another, check the skeleton."""
    if not runner.venv_python().is_file():
        raise RuntimeError("FreeMoCap isn't installed")
    processes = max(1, min(3, (os.cpu_count() or 2) - 1))
    bars = runner.Bars()
    phase = {"name": "start"}
    spans = {"downloading": (0.0, 0.08), "calibration": (0.08, 0.40), "tracking_2d": (0.42, 0.85)}
    after = {"triangulating": 0.87, "skeleton": 0.90, "saving": 0.94, "blender": 0.97}

    def on_event(ev: dict) -> None:
        t, stage = ev.get("type"), ev.get("stage")
        if t == "download":
            total = ev.get("total") or 0
            frac = (ev.get("done") or 0) / total if total else 0.0
            a, b = spans["downloading"]
            job.set_progress(a + (b - a) * min(1.0, frac), f"downloading the sample: {(ev.get('done') or 0) / 1e6:.0f} MB")
        elif t == "stage" and stage == "downloading":
            phase["name"] = "downloading"
            job.set_progress(0.0, "downloading the sample (~38 MB)")
        elif t == "stage" and stage == "detecting_board":
            phase["name"] = "calibration"
            bars.reset()
            job.set_progress(spans["calibration"][0], "calibration: detecting the board")
        elif t == "calibration_done":
            phase["name"] = "calibrated"
            job.set_progress(0.41, "calibrated")
        elif t == "stage" and stage == "tracking_2d":
            phase["name"] = "tracking_2d"
            bars.reset()
            job.set_progress(spans["tracking_2d"][0], "processing: 2D tracking")
        elif t == "stage" and stage in after:
            phase["name"] = stage
            job.set_progress(after[stage], f"processing: {stage.replace('_', ' ')}")

    def on_tqdm(bar: dict) -> None:
        span = spans.get(phase["name"])
        if not span:
            return
        bars.update(bar)
        a, b = span
        label = "detecting the board" if phase["name"] == "calibration" else "2D tracking"
        job.set_progress(a + (b - a) * bars.fraction(3), f"{label}: {int(bars.fraction(3) * 100)}%")

    t0 = time.time()
    res = runner.run_headless("selftest", ["--workdir", str(selftest_dir()), "--processes", str(processes)],
                              job, on_event=on_event, on_tqdm=on_tqdm)
    keys = ("reprojection_error_px", "nan_fraction", "upper_arm_mm", "thigh_mm", "frames", "seconds", "checks")
    result = {"passed": bool(res.get("passed")) and bool(res.get("ok")), "at": t0,
              **{k: res.get(k) for k in keys},
              "error": None if res.get("ok") else (res.get("error") or "self-test failed"),
              "log": res.get("log")}
    update_state(selftest=result)
    if not result["passed"]:
        failed = [k for k, v in (result.get("checks") or {}).items() if not v]
        raise RuntimeError(f"self-test failed: {result['error'] or ''} {('checks failed: ' + ', '.join(failed)) if failed else ''}".strip())
    err = result["reprojection_error_px"]
    job.log(f"self-test passed: reprojection {err:.2f} px, NaN {result['nan_fraction']:.3f}, upper arm "
            f"{result['upper_arm_mm']} mm, thigh {result['thigh_mm']} mm, {result['seconds']} s", "done")
    job.set_progress(1.0, "self-test passed")
    return result


ACTIONS = {
    "install": (install, "Install FreeMoCap"),
    "repair": (repair, "Repair FreeMoCap"),
    "remove": (remove, "Remove FreeMoCap"),
    "selftest": (selftest, "FreeMoCap self-test"),
}
