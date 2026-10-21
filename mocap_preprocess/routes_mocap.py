"""/api/sessions/{s}/mocap...: FreeMoCap calibration profiles and processing of exported takes."""
from __future__ import annotations

import os
import subprocess
import sys

from fastapi import APIRouter, Body, HTTPException

from .app_state import jobs, require_session, store
from .fmc import env, mocap
from .routes_previews import after_mocap_job

router = APIRouter()

CALIBRATE, PROCESS = "mocap_calibrate", "mocap_process"


def _freemocap() -> dict:
    st = env.status()
    return {"ready": bool(env.ready()), "state": st.get("state"), "detail": st.get("detail")}


def _require_ready() -> None:
    if not env.ready():
        raise HTTPException(409, "FreeMoCap isn't ready — install it in Setup on the Home screen")


def _leftover_conflict(leftovers: dict, what: str) -> None:
    n = sum(len(v) for v in leftovers.values())
    raise HTTPException(409, {
        "message": f"{n} leftover video(s) from an earlier export would be fed to FreeMoCap with {what}. "
                   "Delete them (or re-export the take) first.",
        "leftovers": leftovers,
    })


def _described(name: str, take_id: str) -> dict:
    try:
        return mocap.find_take(store, name, str(take_id))[1]
    except mocap.MocapError as e:
        raise HTTPException(400, str(e))


def _job_response(job) -> dict:
    return job.info(log_since=0)


def _view(name: str) -> dict:
    return mocap.view(store, name, _freemocap(), jobs.active(name, PROCESS), jobs.active(name, CALIBRATE))


def _calibrating(name: str, pid: str | None = None) -> bool:
    return any(pid is None or (j.live or {}).get("profile") == pid for j in jobs.active(name, CALIBRATE))


def _edit(name: str, fn):
    """Run ``fn(paths, data)`` under the session lock; map its errors to HTTP ones."""
    try:
        with store.edit(name) as data:
            out = fn(store.paths(name), data)
    except mocap.MocapError as e:
        raise HTTPException(400, str(e))
    except LookupError as e:
        raise HTTPException(404, f"no such profile: {e}")
    return out


@router.get("/api/sessions/{s}/mocap")
def get_mocap(s: str):
    return _view(require_session(s))


@router.put("/api/sessions/{s}/mocap/options")
def put_options(s: str, payload: dict = Body(...)):
    name = require_session(s)
    try:
        with store.edit(name) as data:
            data["mocap"]["options"] = mocap.normalize_options(payload or {}, data["mocap"]["options"],
                                                               mocap.param_defaults(data))
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"invalid options: {e}")
    return {"options": data["mocap"]["options"], "params": mocap.effective_params(data)}


# ---------------------------------------------------------------------------
# profiles

@router.post("/api/sessions/{s}/mocap/profiles")
def create_profile(s: str, payload: dict = Body(...)):
    """{name, take_id?, board?, square_mm?, groundplane?} → a "draft" (not run) profile."""
    name = require_session(s)
    p = _edit(name, lambda paths, data: mocap.create_profile(paths, data, payload or {}))
    return {"profile": p["id"], **_view(name)}


@router.put("/api/sessions/{s}/mocap/profiles/{pid}")
def update_profile(s: str, pid: str, payload: dict = Body(...)):
    """{name?, take_id?, board?, square_mm?, groundplane?}. A calibrated profile keeps its
    result (``settings_changed`` tells the UI it no longer matches) until it's recalibrated."""
    name = require_session(s)
    if _calibrating(name, pid):
        raise HTTPException(409, "that profile is calibrating — wait for it or cancel it")
    _edit(name, lambda paths, data: mocap.update_profile(paths, data, pid, payload or {}))
    return _view(name)


@router.post("/api/sessions/{s}/mocap/profiles/{pid}/duplicate")
def duplicate_profile(s: str, pid: str):
    name = require_session(s)
    p = _edit(name, lambda paths, data: mocap.duplicate_profile(data, pid))
    return {"profile": p["id"], **_view(name)}


@router.delete("/api/sessions/{s}/mocap/profiles/{pid}")
def delete_profile(s: str, pid: str):
    name = require_session(s)
    if _calibrating(name, pid):
        raise HTTPException(409, "that profile is calibrating — cancel it first")
    _edit(name, lambda paths, data: mocap.delete_profile(paths, data, pid))
    return _view(name)


@router.post("/api/sessions/{s}/mocap/profiles/{pid}/activate")
def activate_profile(s: str, pid: str):
    name = require_session(s)
    _edit(name, lambda paths, data: mocap.set_active(data, pid))
    return _view(name)


@router.post("/api/sessions/{s}/mocap/profiles/{pid}/calibrate")
def calibrate_profile(s: str, pid: str, payload: dict = Body(default={})):
    """Run FreeMoCap's calibration for a profile → job (kind mocap_calibrate, job.live.profile)."""
    name = require_session(s)
    _require_ready()
    data = store.load(name)
    p = mocap.get_profile(data, pid)
    if p is None:
        raise HTTPException(404, f"no such profile: {pid}")
    if mocap.calibrated(p):
        raise HTTPException(409, f"{p['name']} is already calibrated — duplicate it to calibrate other settings")
    if not p.get("square_mm"):
        raise HTTPException(400, f"{p['name']}: enter the square size (mm) first")
    if p.get("board") not in mocap.BOARDS:
        raise HTTPException(400, f"board must be one of {', '.join(mocap.BOARDS)}")
    take = _described(name, p.get("take_id"))
    if not take["layout_ok"]:
        raise HTTPException(400, f"{take['name']}: no synchronized_videos/ folder (export it with the freemocap layout)")
    if take["missing"]:
        raise HTTPException(400, f"{take['name']}: videos missing on disk: {', '.join(take['missing'])} — re-export it")
    if len(take["cameras"]) < 2:
        raise HTTPException(400, f"{take['name']}: needs at least 2 cameras, has {len(take['cameras'])}")
    if take["leftovers"] and not (payload or {}).get("delete_leftovers"):
        _leftover_conflict({take["take_id"]: take["leftovers"]}, "the calibration")
    if jobs.active(name, PROCESS):
        raise HTTPException(409, "mocap processing is running — wait for it or cancel it first")
    delete = list(take["leftovers"])

    def run(job):
        for fn in delete:                    # the user confirmed deleting these
            f = store.paths(name).output / take["dir"] / mocap.VIDEO_DIR / fn
            if f.is_file():
                f.unlink()
                job.log(f"{take['name']}: deleted leftover {mocap.VIDEO_DIR}/{fn}", "warn")
        try:
            return mocap.run_calibrate(store, name, job, pid)
        finally:
            after_mocap_job(name, job)      # its board videos get previews for Cut Takes

    job = jobs.start(CALIBRATE, name, run, label=f"Calibrate {p['name']}")
    job.live = {"profile": pid}
    return _job_response(job)


@router.post("/api/sessions/{s}/mocap/calibrate")
def calibrate(s: str, payload: dict = Body(...)):
    """The New profile form: {name?, take_id, board, square_mm, groundplane} → a new profile,
    calibrated now (without a name it's called after the board). If the run can't start, the
    profile isn't kept."""
    name = require_session(s)
    _require_ready()
    try:
        sq = float(payload.get("square_mm"))
    except (TypeError, ValueError):
        raise HTTPException(400, "square size (mm) is required and must be > 0")
    board = str(payload.get("board") or "7x5")

    def make(paths, data):
        if str(payload.get("name") or "").strip():
            pname = mocap.clean_name(payload["name"])        # create_profile refuses a name in use
        else:
            pname = mocap.unique_name(data, mocap.clean_name(f"{board}-{sq:g}mm" if sq > 0 else board))
        return mocap.create_profile(paths, data, {**payload, "board": board, "square_mm": sq, "name": pname})

    p = _edit(name, make)
    try:
        return calibrate_profile(s, p["id"], {"delete_leftovers": payload.get("delete_leftovers")})
    except HTTPException:
        _edit(name, lambda paths, data: mocap.delete_profile(paths, data, p["id"]))
        raise


@router.put("/api/sessions/{s}/mocap/takes/{tid}/profile")
def set_take_profile(s: str, tid: str, payload: dict = Body(...)):
    """{profile: <id> | null} — the take's calibration (null: follow the active profile)."""
    name = require_session(s)
    if tid in mocap.running_takes(jobs.active(name, PROCESS)):
        raise HTTPException(409, "that take is being processed")
    _described(name, tid)
    _edit(name, lambda paths, data: mocap.set_take_profile(data, tid, (payload or {}).get("profile")))
    return _view(name)


@router.get("/api/sessions/{s}/mocap/takes/{tid}/detail")
def take_detail(s: str, tid: str):
    """What FreeMoCap produced for one take: mocap_info.json, quality numbers from its
    arrays, the output files, and the .blend if one was exported."""
    name = require_session(s)
    try:
        return mocap.take_detail(store, name, str(tid))
    except mocap.MocapError as e:
        raise HTTPException(400, str(e))


@router.post("/api/sessions/{s}/mocap/takes/{tid}/open_blend")
def open_blend(s: str, tid: str):
    """Open the take's .blend with the program the OS associates with it (Blender)."""
    name = require_session(s)
    try:
        path = mocap.blend_path(store, name, str(tid))
    except mocap.MocapError as e:
        raise HTTPException(404, str(e))
    if os.name == "nt":
        os.startfile(str(path))  # noqa: S606 - local tool, path checked to be inside the take folder
    else:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        subprocess.Popen([opener, str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return {"opened": str(path)}


# ---------------------------------------------------------------------------
# process

@router.post("/api/sessions/{s}/mocap/process")
def process(s: str, payload: dict = Body(...)):
    name = require_session(s)
    _require_ready()
    take_ids = [str(x) for x in (payload.get("take_ids") or [])]
    if not take_ids:
        raise HTTPException(400, "select at least one take")
    data = store.load(name)
    if jobs.active(name, CALIBRATE):
        raise HTTPException(409, "a calibration is running — wait for it first")

    params = mocap.effective_params(data)
    try:
        mocap.check_params(params)
    except mocap.MocapError as e:
        raise HTTPException(400, str(e))
    takes, problems, leftovers = [], [], {}
    for tid in dict.fromkeys(take_ids):
        tk = _described(name, tid)
        prof = mocap.get_profile(data, tk["profile"]) if tk["profile"] else None
        skipped = mocap.skipped_stage_missing(store.paths(name).output / tk["dir"], params)
        if tk["is_calibration"]:
            problems.append(f"{tk['name']} is a calibration take")
        elif not mocap.calibrated(prof):
            problems.append(f"{tk['name']}: no calibrated profile — calibrate one on the Calibrate step")
        elif not (store.paths(name).root / prof["toml"]).is_file():
            problems.append(f"{tk['name']}: {prof['name']}'s calibration file is gone ({prof['toml']}) — recalibrate it")
        elif not tk["layout_ok"]:
            problems.append(f"{tk['name']}: no synchronized_videos/ folder (flat layout?)")
        elif tk["missing"]:
            problems.append(f"{tk['name']}: videos missing on disk ({', '.join(tk['missing'])})")
        elif tk["calib_match"] and tk["calib_match"]["unknown"]:
            problems.append(f"{tk['name']}: camera(s) not in {prof['name']}: {', '.join(tk['calib_match']['unknown'])}")
        elif tk["calib_match"] and tk["calib_match"]["n"] < 2:
            problems.append(f"{tk['name']}: needs at least 2 calibrated cameras")
        elif skipped:
            problems.append(f"{tk['name']}: {skipped}")
        if tk["leftovers"]:
            leftovers[tid] = tk["leftovers"]
        takes.append(tk)
    if problems:
        raise HTTPException(400, "; ".join(problems))
    delete_leftovers = bool(payload.get("delete_leftovers"))
    if leftovers and not delete_leftovers:
        _leftover_conflict(leftovers, "these takes")

    opts = data["mocap"]["options"]
    processes = opts.get("processes") or mocap.default_processes(max(len(t["cameras"]) for t in takes))

    def run(job):
        try:
            return mocap.run_process(store, name, job, takes, processes, blender=bool(opts.get("blender")),
                                     blender_exe=opts.get("blender_exe"), delete=leftovers if delete_leftovers else None,
                                     params=params)
        finally:
            after_mocap_job(name, job)      # the skeleton videos get previews for Cut Takes

    return _job_response(jobs.start(PROCESS, name, run, label="Process mocap"))
