"""FastAPI app: JSON API, background jobs, static UI and media serving."""
from __future__ import annotations

import inspect
import os
import re
import subprocess
import sys
from pathlib import Path

from fastapi import Body, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import config
from . import ffmpeg_tools as ff
from . import exporter, grouping, pipeline, syncing
from . import takes as takes_mod
from .jobs import JobConflict, JobManager
from .session_store import (InvalidName, SessionNotFound, SessionStore, validate_fid,
                            validate_session_name)

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


def _apply_settings(data: dict, payload: dict) -> None:
    st = data["settings"]
    if "target_fps" in payload:
        fps = ff.parse_fraction(str(payload["target_fps"]).strip())
        if fps is None:
            raise HTTPException(400, f"invalid fps: {payload['target_fps']}")
        fps_s = ff.frac_str(fps)
        if fps_s != st["target_fps"]:
            st["target_fps_user_set"] = True
        st["target_fps"] = fps_s
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
    for key, lo, hi in (("sync_min_score", 0.0, 1.0), ("sync_min_ratio", 1.0, 100.0), ("sync_window", 0.2, 30.0)):
        if key in payload:
            st[key] = max(lo, min(hi, float(payload[key])))


@app.put("/api/sessions/{s}/settings")
def put_settings(s: str, payload: dict = Body(...)):
    name = _session(s)
    with store.edit(name) as data:
        _apply_settings(data, payload)
    return session_view(name)


def _start_proxies(name: str):
    try:
        return jobs.start("proxies", name, lambda j: pipeline.run_proxies(store, name, j), label="Build proxies")
    except JobConflict:
        return None


@app.post("/api/sessions/{s}/process")
def process_session(s: str, payload: dict = Body(default={})):
    name = _session(s)
    _require_tools()
    if (payload or {}).get("settings"):
        with store.edit(name) as data:
            _apply_settings(data, payload["settings"])
    job = jobs.start("process", name, lambda j: pipeline.process(store, name, j, lambda: _start_proxies(name)),
                     label="Process source files")
    return _job_response(job)


@app.post("/api/sessions/{s}/proxies")
def build_proxies(s: str):
    name = _session(s)
    _require_tools()
    job = _start_proxies(name) or next(iter(jobs.active(name, "proxies")), None)
    return _job_response(job)


@app.post("/api/sessions/{s}/regroup")
def regroup(s: str):
    name = _session(s)

    def run(job):
        paths = store.paths(name)
        with store.edit(name) as data:
            grouping.regroup(data, pipeline.video_files(data), pipeline.load_feature_fn(paths))
            n = len(data["cameras"])
        job.log(f"grouped into {n} camera(s)")
        return {"cameras": n}

    return _job_response(jobs.start("regroup", name, run, label="Re-run grouping"))


_CAM_ID = re.compile(r"^c\d{1,4}$")


@app.put("/api/sessions/{s}/cameras")
def put_cameras(s: str, payload: dict = Body(...)):
    name = _session(s)
    cams_in = payload.get("cameras")
    assignments = payload.get("assignments") or {}
    if not isinstance(cams_in, list) or not isinstance(assignments, dict):
        raise HTTPException(400, "expected {cameras: [...], assignments: {file_id: camera_id}}")
    with store.edit(name) as data:
        old = {c["id"]: c for c in data["cameras"]}
        cams, ids = [], set()
        for c in cams_in:
            cid = str(c.get("id", ""))
            if not _CAM_ID.match(cid) or cid in ids:
                raise HTTPException(400, f"bad camera id: {cid!r}")
            rot = int(c.get("rotation", 0))
            if rot not in (0, 90, 180, 270):
                raise HTTPException(400, f"rotation must be 0/90/180/270, got {rot}")
            ids.add(cid)
            cams.append({"id": cid, "name": str(c.get("name") or "").strip()[:60] or cid, "rotation": rot,
                         "include": bool(c.get("include", True)),
                         "grouped_by": (old.get(cid) or {}).get("grouped_by", "manual")})
        files = {f["id"]: f for f in data["files"]}
        for fid, cid in assignments.items():
            validate_fid(fid)
            if fid not in files or cid not in ids:
                raise HTTPException(400, f"bad assignment {fid} -> {cid}")
            files[fid]["camera"] = cid
        used = {f.get("camera") for f in data["files"]}
        data["cameras"] = [c for c in cams if c["id"] in used]
    return session_view(name)


@app.post("/api/sessions/{s}/sync")
def sync(s: str, payload: dict = Body(default={})):
    name = _session(s)
    _require_tools()
    resync_manual = bool((payload or {}).get("resync_manual", False))
    job = jobs.start("sync", name, lambda j: syncing.sync_session(store, name, j, resync_manual),
                     label="Sync by audio")
    return _job_response(job)


@app.put("/api/sessions/{s}/files/{fid}/offset")
def put_offset(s: str, fid: str, payload: dict = Body(...)):
    name = _session(s)
    validate_fid(fid)
    try:
        offset = float(payload["offset"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "expected {offset: seconds}")
    with store.edit(name) as data:
        f = next((x for x in data["files"] if x["id"] == fid), None)
        if f is None:
            raise HTTPException(404, "file not found")
        prev = f.get("sync") or {}
        f["offset"] = round(offset, 6)
        f["sync"] = {"method": "manual", "was": prev.get("was") if prev.get("method") == "manual" else prev.get("method")}
    return {"id": fid, "offset": f["offset"], "sync": f["sync"]}


@app.post("/api/sessions/{s}/files/{fid}/resync")
def resync(s: str, fid: str, payload: dict = Body(...)):
    name = _session(s)
    validate_fid(fid)
    ref = validate_fid(str((payload or {}).get("ref_fid", "")))
    if jobs.active(name, "sync"):
        raise HTTPException(409, "a sync job is running")

    def run(job):
        job.set_progress(0.1, "correlating")
        res = syncing.resync_file(store, name, fid, ref)
        job.log(f"re-sync → {res['offset']:+.3f} s (score {res['score']:.3f}, ratio {res['ratio']:.2f})"
                + ("" if res["accepted"] else " — weak match, check it"), "info" if res["accepted"] else "warn")
        return res

    return _job_response(jobs.start("resync", name, run, label="Re-sync file"))


@app.put("/api/sessions/{s}/takes")
def put_takes(s: str, payload: list = Body(...)):
    name = _session(s)
    try:
        clean = takes_mod.validate(payload)
    except (ValueError, KeyError, TypeError) as e:
        raise HTTPException(400, f"invalid takes: {e}")
    with store.edit(name) as data:
        data["takes"] = clean
    return {"takes": clean, "state": data["state"]}


@app.post("/api/sessions/{s}/takes/auto")
def auto_takes(s: str, payload: dict = Body(default={})):
    name = _session(s)
    with store.edit(name) as data:
        if data["takes"] and not (payload or {}).get("replace"):
            raise HTTPException(409, "takes already exist; pass {replace: true}")
        data["takes"] = takes_mod.detect(data)
    return session_view(name)


@app.put("/api/sessions/{s}/export_options")
def put_export_options(s: str, payload: dict = Body(...)):
    name = _session(s)
    with store.edit(name) as data:
        data["export_options"] = exporter.normalize_options(payload, config.DEFAULT_EXPORT_OPTIONS)
    return {"export_options": data["export_options"]}


@app.post("/api/sessions/{s}/export")
def export(s: str, payload: dict = Body(...)):
    name = _session(s)
    _require_tools()
    take_ids = [str(x) for x in (payload.get("take_ids") or [])]
    camera_ids = [str(x) for x in (payload.get("camera_ids") or [])]
    with store.edit(name) as data:
        opts = exporter.normalize_options(payload.get("options") or data["export_options"], config.DEFAULT_EXPORT_OPTIONS)
        opts["camera_ids"] = camera_ids
        data["export_options"] = opts
    if not take_ids or not camera_ids:
        raise HTTPException(400, "select at least one take and one camera")
    job = jobs.start("export", name, lambda j: exporter.export(store, name, j, take_ids, camera_ids, opts),
                     label="Export takes")
    return _job_response(job)


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
def get_job(job_id: str, log_since: int = 0, live: bool = False):
    job = jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.info(log_since=log_since, live=live)


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = jobs.cancel(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return job.info()


# ---------------------------------------------------------------------------
# media (Range support is required for <video> seeking in Chrome)

def _file_response_handles_range() -> bool:
    try:
        src = inspect.getsource(FileResponse).lower()
    except (OSError, TypeError):
        return False
    return "content-range" in src and "206" in src


FILE_RESPONSE_RANGE = _file_response_handles_range()
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


def _range_response(path: Path, media_type: str, request: Request) -> Response:
    """Starlette's FileResponse handles Range itself in recent versions; this
    is the fallback for older installs."""
    headers = {"Accept-Ranges": "bytes", "Cache-Control": "no-cache"}
    rng = request.headers.get("range")
    if FILE_RESPONSE_RANGE or not rng:
        return FileResponse(path, media_type=media_type, headers=headers)
    size = path.stat().st_size
    m = _RANGE.match(rng.strip())
    if not m or (m.group(1) == "" and m.group(2) == ""):
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})
    if m.group(1) == "":
        start, end = max(0, size - int(m.group(2))), size - 1
    else:
        start = int(m.group(1))
        end = min(int(m.group(2)) if m.group(2) else size - 1, size - 1)
    if start >= size or start > end:
        return Response(status_code=416, headers={"Content-Range": f"bytes */{size}"})

    def body():
        with open(path, "rb") as fh:
            fh.seek(start)
            remaining = end - start + 1
            while remaining > 0:
                chunk = fh.read(min(1 << 16, remaining))
                if not chunk:
                    break
                remaining -= len(chunk)
                yield chunk

    headers.update({"Content-Range": f"bytes {start}-{end}/{size}", "Content-Length": str(end - start + 1)})
    return StreamingResponse(body(), status_code=206, media_type=media_type, headers=headers)


_MEDIA = {"thumb": ("jpg", "image/jpeg"), "proxy": ("mp4", "video/mp4"), "wave": ("json", "application/json")}


@app.get("/media/{s}/{kind}/{filename}")
def media(s: str, kind: str, filename: str, request: Request):
    name = _session(s)
    if kind not in _MEDIA:
        raise HTTPException(404, "unknown media kind")
    ext, mime = _MEDIA[kind]
    stem, dot, got_ext = filename.rpartition(".")
    if not dot or got_ext != ext:
        raise HTTPException(404, "not found")
    fid = validate_fid(stem)
    paths = store.paths(name)
    path = {"thumb": paths.thumb, "proxy": paths.proxy, "wave": paths.wave_json}[kind](fid)
    if not path.exists():
        raise HTTPException(404, "not built yet")
    return _range_response(path, mime, request)


# ---------------------------------------------------------------------------
# static UI

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(config.STATIC_DIR / "index.html", media_type="text/html")


app.mount("/static", StaticFiles(directory=str(config.STATIC_DIR)), name="static")
