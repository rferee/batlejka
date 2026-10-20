"""/api/sessions/{s}/mocap...: FreeMoCap calibration and processing of exported takes."""
from __future__ import annotations

import math

from fastapi import APIRouter, Body, HTTPException

from .app_state import jobs, require_session, store
from .fmc import env, mocap

router = APIRouter()

CALIBRATE, PROCESS = "mocap_calibrate", "mocap_process"


def _freemocap() -> dict:
    st = env.status()
    return {"ready": bool(env.ready()), "state": st.get("state"), "detail": st.get("detail")}


def _require_ready() -> None:
    if not env.ready():
        raise HTTPException(409, "FreeMoCap isn't ready — install it in Setup on the Session screen")


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


@router.get("/api/sessions/{s}/mocap")
def get_mocap(s: str):
    name = require_session(s)
    return mocap.view(store, name, _freemocap(), jobs.active(name, PROCESS))


@router.put("/api/sessions/{s}/mocap/options")
def put_options(s: str, payload: dict = Body(...)):
    name = require_session(s)
    try:
        with store.edit(name) as data:
            data["mocap"]["options"] = mocap.normalize_options(payload or {}, data["mocap"]["options"])
    except (TypeError, ValueError) as e:
        raise HTTPException(400, f"invalid options: {e}")
    return {"options": data["mocap"]["options"]}


@router.post("/api/sessions/{s}/mocap/calibrate")
def calibrate(s: str, payload: dict = Body(...)):
    name = require_session(s)
    _require_ready()
    board = str(payload.get("board") or "7x5")
    if board not in mocap.BOARDS:
        raise HTTPException(400, f"board must be one of {', '.join(mocap.BOARDS)}")
    try:
        square_mm = float(payload.get("square_mm"))
    except (TypeError, ValueError):
        square_mm = float("nan")
    if not math.isfinite(square_mm) or square_mm <= 0:
        raise HTTPException(400, "square size (mm) is required and must be > 0")
    take = _described(name, payload.get("take_id"))
    if not take["layout_ok"]:
        raise HTTPException(400, f"{take['name']}: no synchronized_videos/ folder (export it with the freemocap layout)")
    if take["missing"]:
        raise HTTPException(400, f"{take['name']}: videos missing on disk: {', '.join(take['missing'])} — re-export it")
    if len(take["cameras"]) < 2:
        raise HTTPException(400, f"{take['name']}: needs at least 2 cameras, has {len(take['cameras'])}")
    if take["leftovers"]:
        if not payload.get("delete_leftovers"):
            _leftover_conflict({take["take_id"]: take["leftovers"]}, "the calibration")
    if jobs.active(name, PROCESS):
        raise HTTPException(409, "mocap processing is running — wait for it or cancel it first")
    delete = {take["take_id"]: take["leftovers"]} if take["leftovers"] else {}
    groundplane = bool(payload.get("groundplane"))

    def run(job):
        for fn in delete.get(take["take_id"], []):     # the user confirmed deleting these
            p = store.paths(name).output / take["dir"] / mocap.VIDEO_DIR / fn
            if p.is_file():
                p.unlink()
                job.log(f"{take['name']}: deleted leftover {mocap.VIDEO_DIR}/{fn}", "warn")
        return mocap.run_calibrate(store, name, job, take, board, square_mm, groundplane)

    return _job_response(jobs.start(CALIBRATE, name, run, label="Calibrate cameras"))


@router.post("/api/sessions/{s}/mocap/process")
def process(s: str, payload: dict = Body(...)):
    name = require_session(s)
    _require_ready()
    take_ids = [str(x) for x in (payload.get("take_ids") or [])]
    if not take_ids:
        raise HTTPException(400, "select at least one take")
    data = store.load(name)
    calib = data["mocap"].get("calibration")
    if not calib:
        raise HTTPException(400, "calibrate the cameras first")
    if not (store.paths(name).root / calib["toml"]).is_file():
        raise HTTPException(400, f"calibration file is gone: {calib['toml']} — calibrate again")
    if jobs.active(name, CALIBRATE):
        raise HTTPException(409, "calibration is running — wait for it first")

    takes, problems, leftovers = [], [], {}
    for tid in dict.fromkeys(take_ids):
        tk = _described(name, tid)
        if tk["is_calibration"]:
            problems.append(f"{tk['name']} is the calibration take")
        elif not tk["layout_ok"]:
            problems.append(f"{tk['name']}: no synchronized_videos/ folder (flat layout?)")
        elif tk["missing"]:
            problems.append(f"{tk['name']}: videos missing on disk ({', '.join(tk['missing'])})")
        elif tk["calib_match"] and tk["calib_match"]["unknown"]:
            problems.append(f"{tk['name']}: camera(s) not in the calibration: {', '.join(tk['calib_match']['unknown'])}")
        elif tk["calib_match"] and tk["calib_match"]["n"] < 2:
            problems.append(f"{tk['name']}: needs at least 2 calibrated cameras")
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
        return mocap.run_process(store, name, job, takes, calib, processes, blender=bool(opts.get("blender")),
                                 blender_exe=opts.get("blender_exe"), delete=leftovers if delete_leftovers else None)

    return _job_response(jobs.start(PROCESS, name, run, label="Process mocap"))
