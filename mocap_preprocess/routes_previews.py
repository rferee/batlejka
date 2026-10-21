"""Cut Takes' Preview: FreeMoCap's annotated videos as browser-playable previews (previews.py).
They're built in the background whenever a calibration or mocap run finishes, and on
demand when Preview is turned on with some missing."""
from __future__ import annotations

import threading

from fastapi import APIRouter

from . import previews
from .app_state import jobs, require_session, store
from .fmc import mocap
from .jobs import JobConflict

router = APIRouter()
PREVIEWS = "previews"
_again: set[str] = set()          # sessions that asked for a build while one was running
_lock = threading.Lock()


def _busy_takes(name: str) -> set[str]:
    """Takes FreeMoCap may still be writing: being processed, or being calibrated."""
    busy = set(mocap.running_takes(jobs.active(name, mocap.JOB_PROCESS)))
    data = None
    for j in jobs.active(name, mocap.JOB_CALIBRATE):
        pid = (getattr(j, "live", None) or {}).get("profile")
        data = data or store.load(name)
        p = mocap.get_profile(data, pid) if pid else None
        if p and p.get("take_id"):
            busy.add(p["take_id"])
    return busy


def start_build(name: str):
    """Start a preview build; if one is running, another starts when it ends (once, however
    often this is called meanwhile). Returns the job, or None when it was queued."""
    with _lock:
        try:
            return jobs.start(PREVIEWS, name, lambda job: previews.build(store, name, job, _busy_takes(name)),
                              label="Make previews")
        except JobConflict:
            running = jobs.active(name, PREVIEWS)
            if name in _again or not running:
                return None
            _again.add(name)

    def later(j=running[0]):
        j.done_event.wait()
        with _lock:
            _again.discard(name)
        start_build(name)
    threading.Thread(target=later, name=f"previews-again-{name}", daemon=True).start()
    return None


def after_mocap_job(name: str, job) -> None:
    """Called as a calibration or processing job ends: once it's off the books (its takes
    aren't busy any more), its new videos get previews."""
    def later():
        job.done_event.wait(120)
        try:
            start_build(name)
        except Exception:  # noqa: BLE001 - a preview build must never break anything else
            pass
    threading.Thread(target=later, name=f"previews-after-{job.id}", daemon=True).start()


@router.get("/api/sessions/{s}/previews")
def list_previews(s: str):
    name = require_session(s)
    out = previews.view(store.paths(name), store.load(name), name)
    out["building"] = bool(jobs.active(name, PREVIEWS))
    return out


@router.post("/api/sessions/{s}/previews/build")
def build_previews(s: str):
    name = require_session(s)
    job = start_build(name)
    return {"job": job.info(log_since=0) if job else None, "queued": job is None}
