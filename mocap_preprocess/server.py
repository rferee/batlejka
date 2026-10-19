"""FastAPI app: JSON API, background jobs, static UI and media serving."""
from __future__ import annotations

import inspect
import os
import subprocess
import sys
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import config
from . import ffmpeg_tools as ff
from . import pipeline
from .jobs import JobConflict, JobManager
from .session_store import InvalidName, SessionNotFound, SessionStore, validate_session_name

app = FastAPI(title="mocap-preprocess", docs_url="/api/docs", openapi_url="/api/openapi.json")
store = SessionStore()
jobs = JobManager()


# ---------------------------------------------------------------------------
# errors

@app.exception_handler(InvalidName)
async def _invalid(_: Request, exc: InvalidName):
    return JSONResponse({"detail": str(exc)}, status_code=400)


@app.exception_handler(SessionNotFound)
async def _not_found(_: Request, exc: SessionNotFound):
    return JSONResponse({"detail": f"session not found: {exc}"}, status_code=404)


@app.exception_handler(JobConflict)
async def _conflict(_: Request, exc: JobConflict):
    return JSONResponse({"detail": str(exc)}, status_code=409)


@app.exception_handler(ff.ToolMissing)
async def _tool_missing(_: Request, exc: ff.ToolMissing):
    return JSONResponse({"detail": str(exc)}, status_code=503)


@app.middleware("http")
async def _no_cache_ui(request: Request, call_next):
    response = await call_next(request)
    path = request.url.path
    if path == "/" or path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def _session(s: str) -> str:
    name = validate_session_name(s)
    if not store.exists(name):
        raise SessionNotFound(name)
    return name


def _require_tools() -> None:
    if not config.ffmpeg_path() or not config.ffprobe_path():
        raise HTTPException(503, "ffmpeg/ffprobe not found on PATH (or set MOCAP_FFMPEG / MOCAP_FFPROBE)")


def _job_response(job) -> dict:
    return job.info(log_since=0)


# ---------------------------------------------------------------------------
# health and sessions

@app.get("/api/health")
def health():
    return {
        "ffmpeg": config.ffmpeg_path(),
        "ffprobe": config.ffprobe_path(),
        "version": ff.tool_version("ffmpeg"),
        "sessions_dir": str(config.sessions_dir()),
        "platform": sys.platform,
    }


@app.get("/api/sessions")
def list_sessions():
    return store.list_sessions()


@app.post("/api/sessions")
def create_session(payload: dict = Body(...)):
    name = validate_session_name(payload.get("name"))
    if store.exists(name):
        raise HTTPException(409, f"session already exists: {name}")
    store.create(name)
    return {"name": name}


def session_view(name: str) -> dict:
    data = store.load(name)
    data["fps_check"] = pipeline.fps_check(data)
    data["standard_rates"] = pipeline.standard_rates()
    data["jobs"] = [j.info() for j in jobs.active(name)]
    return data


@app.get("/api/sessions/{s}")
def get_session(s: str):
    return session_view(_session(s))


@app.post("/api/sessions/{s}/scan")
def scan_session(s: str):
    name = _session(s)
    _require_tools()
    job = jobs.start("scan", name, lambda j: pipeline.scan(store, name, j), label="Scan source")
    return _job_response(job)


SAMPLE_RATES = (4000, 8000, 11025, 16000, 22050, 24000, 32000, 44100, 48000)


@app.put("/api/sessions/{s}/settings")
def put_settings(s: str, payload: dict = Body(...)):
    name = _session(s)
    with store.edit(name) as data:
        st = data["settings"]
        if "target_fps" in payload:
            fps = str(payload["target_fps"]).strip()
            if ff.parse_fraction(fps) is None:
                raise HTTPException(400, f"invalid fps: {fps}")
            fps = ff.frac_str(ff.parse_fraction(fps))
            if fps != st["target_fps"]:
                st["target_fps_user_set"] = True
            st["target_fps"] = fps
        if "target_fps_user_set" in payload:
            st["target_fps_user_set"] = bool(payload["target_fps_user_set"])
            if not st["target_fps_user_set"]:
                pipeline.prefill_target_fps(data)
        if "sync_sample_rate" in payload:
            sr = int(payload["sync_sample_rate"])
            if sr not in SAMPLE_RATES:
                raise HTTPException(400, f"unsupported sample rate: {sr}")
            st["sync_sample_rate"] = sr
        if "proxy_long_side" in payload:
            st["proxy_long_side"] = max(160, min(1920, int(payload["proxy_long_side"])))
        if "hwaccel" in payload:
            st["hwaccel"] = bool(payload["hwaccel"])
        for key, lo, hi in (("sync_min_score", 0.0, 1.0), ("sync_min_ratio", 1.0, 100.0)):
            if key in payload:
                st[key] = max(lo, min(hi, float(payload[key])))
    return session_view(name)


@app.post("/api/sessions/{s}/reveal")
def reveal(s: str, payload: dict = Body(default={})):
    name = _session(s)
    paths = store.paths(name)
    what = (payload or {}).get("what", "session")
    target = {"session": paths.root, "output": paths.output, "source": paths.source}.get(what)
    if target is None:
        raise HTTPException(400, f"unknown target: {what}")
    target.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        os.startfile(str(target))  # noqa: S606 - local tool, path is ours
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(target)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"opened": str(target)}


# ---------------------------------------------------------------------------
# jobs

@app.get("/api/jobs")
def list_jobs(session: str | None = None):
    if session is not None:
        session = validate_session_name(session)
    return [j.info() for j in jobs.list(session)]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str, log_since: int = 0):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.info(log_since=log_since)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.info()


# ---------------------------------------------------------------------------
# static UI

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(config.STATIC_DIR / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
