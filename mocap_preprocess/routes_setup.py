"""/api/setup: the Setup panel on the Session screen, and the FreeMoCap install /
repair / remove / self-test jobs (kind "setup", no session — one at a time)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from . import setup_check
from .app_state import jobs
from .fmc import env

router = APIRouter()

FREEMOCAP_ACTIONS = ("install", "repair", "remove", "selftest")


def _busy() -> dict | None:
    job = next(iter(jobs.active(None, "setup")), None)
    return job.info() if job else None


@router.get("/api/setup")
def get_setup(refresh: bool = False):
    return setup_check.get_setup(refresh=refresh, busy_job=_busy())


@router.post("/api/setup/freemocap/{action}")
def freemocap_action(action: str):
    if action not in FREEMOCAP_ACTIONS:
        raise HTTPException(400, f"unknown action: {action}")
    entry = env.ACTIONS.get(action)
    if entry is None:
        raise HTTPException(503, f"FreeMoCap {action} isn't available in this build")
    if action == "selftest" and not env.ready():
        raise HTTPException(409, "FreeMoCap isn't ready — install or repair it first")
    fn, label = entry
    job = jobs.start("setup", None, fn, label=label)   # JobConflict → 409 while another setup job runs
    return job.info(log_since=0)
