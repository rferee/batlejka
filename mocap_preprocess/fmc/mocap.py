"""Session-level FreeMoCap calibration and processing.

Lists the exported takes (``output/<dir>/take_info.json``), matches their cameras
against the calibration TOML, works out whether a take's mocap is up to date
(``mocap_info.json`` snapshot of the synchronized videos and the calibration), and
runs the calibrate / process jobs through :mod:`.runner`. ``session.json`` holds
``mocap.calibration``, ``mocap.options`` and ``mocap.takes``; the session lock is
only held for short writes, never while FreeMoCap runs.
"""
from __future__ import annotations

import json
import os
import time
import tomllib
from pathlib import Path

from .. import config
from ..session_store import write_json_atomic
from . import runner

VIDEO_DIR = "synchronized_videos"
MOCAP_INFO = "mocap_info.json"
BOARDS = ("7x5", "5x3")
# how far into a take each FreeMoCap stage is (2D tracking fills 0 → 0.75 from the tqdm bars)
STAGE_FRACTION = {"tracking_2d": 0.0, "triangulating": 0.8, "skeleton": 0.88, "saving": 0.95, "blender": 0.97}
TRACKING_SHARE = 0.75


class MocapError(ValueError):
    """A request the current state can't satisfy (→ HTTP 400)."""


# ---------------------------------------------------------------------------
# files

def read_toml_cameras(path: Path) -> list[str]:
    """Camera names in the order aniposelib loads them (``cam_*`` sections sorted by key)."""
    with open(path, "rb") as fh:
        calib = tomllib.load(fh)
    return [calib[k]["name"] for k in sorted(calib) if k != "metadata" and isinstance(calib[k], dict)]


def _read_json(path: Path) -> dict | None:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def snapshot_videos(take_dir: Path) -> dict:
    """Size and mtime of every ``.mp4`` FreeMoCap would read from the take."""
    vids = take_dir / VIDEO_DIR
    out = {}
    if vids.is_dir():
        for p in sorted(vids.iterdir(), key=lambda x: x.name.lower()):
            if p.is_file() and p.suffix.lower() == ".mp4":
                st = p.stat()
                out[p.name] = {"size": st.st_size, "mtime": st.st_mtime}
    return out


def exported_takes(paths) -> list[dict]:
    """``{dir, path, info}`` for every folder in output/ with a take_info.json. When
    several folders carry the same take id (renamed and exported again), the newest wins."""
    best: dict[str, dict] = {}
    out = paths.output
    if not out.is_dir():
        return []
    for d in sorted(out.iterdir(), key=lambda x: x.name.lower()):
        if not d.is_dir():
            continue
        info = _read_json(d / "take_info.json")
        if not info or not info.get("id"):
            continue
        entry = {"dir": d.name, "path": d, "info": info}
        prev = best.get(info["id"])
        if prev is None or (info.get("exported_at") or 0) > (prev["info"].get("exported_at") or 0):
            best[info["id"]] = entry
    return sorted(best.values(), key=lambda e: (e["info"].get("start") or 0, e["dir"].lower()))


def _calibration_cameras(paths, calib: dict | None) -> list[str] | None:
    if not calib or not calib.get("toml"):
        return None
    p = paths.root / calib["toml"]
    if not p.is_file():
        return None
    try:
        return read_toml_cameras(p)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _stale_reason(entry: dict, calib: dict | None) -> str | None:
    take_dir = entry["path"]
    info = _read_json(take_dir / MOCAP_INFO)
    if info is None:
        return "mocap_info.json missing"
    if not any((take_dir / "output_data").glob("*_body_3d_xyz.npy")):
        return "output_data missing"
    if (info.get("videos") or {}) != snapshot_videos(take_dir):
        return "videos re-exported"
    used = info.get("calibration") or {}
    if not calib or used.get("toml") != calib.get("toml") or used.get("created_at") != calib.get("created_at"):
        return "calibration changed"
    return None


def describe_take(entry: dict, data: dict, calib_cams: list[str] | None, running: set[str]) -> dict:
    info = entry["info"]
    take_dir: Path = entry["path"]
    tid = info["id"]
    vids = take_dir / VIDEO_DIR
    layout_ok = info.get("layout", "freemocap") == "freemocap" and vids.is_dir()
    cams = [c for c in info.get("cameras") or [] if c.get("output")]
    expected = [Path(c["output"]).name for c in cams]
    stems = [Path(n).stem for n in expected]
    on_disk = {p.name for p in vids.glob("*")} if vids.is_dir() else set()
    leftovers = sorted((n for n in on_disk if n.lower().endswith(".mp4") and n not in expected), key=str.lower)
    missing = [n for n in expected if n not in on_disk] if layout_ok else expected
    session_take = next((t for t in data.get("takes") or [] if t.get("id") == tid), None)
    calib = data["mocap"].get("calibration")
    is_calibration = bool(calib and calib.get("take_id") == tid)

    match = None
    if calib_cams is not None:
        known = [s for s in stems if s in calib_cams]
        match = {"n": len(known), "of": len(calib_cams),
                 "missing": [c for c in calib_cams if c not in stems],
                 "unknown": [s for s in stems if s not in calib_cams]}

    rec = (data["mocap"].get("takes") or {}).get(tid) or {}
    status = rec.get("status") or "not_processed"
    stale_reason = None
    if tid in running:
        status = "running"
    elif status == "done":
        stale_reason = _stale_reason(entry, calib)
        if stale_reason:
            status = "stale"
    elif status not in ("failed",):
        status = "not_processed"
    return {
        "take_id": tid,
        "name": (session_take or {}).get("name") or info.get("take") or entry["dir"],
        "dir": entry["dir"],
        "cameras": stems,
        "camera_names": [c.get("name") or Path(c["output"]).stem for c in cams],
        "leftovers": leftovers,
        "missing": missing,
        "layout_ok": layout_ok,
        "frames": info.get("frames"),
        "duration": info.get("duration"),
        "exported_at": info.get("exported_at"),
        "is_calibration": is_calibration,
        "calib_match": match,
        "status": status,
        "stale_reason": stale_reason,
        "error": rec.get("error") if status == "failed" else None,
        "processed_at": rec.get("processed_at"),
        "subset": rec.get("subset"),
        "seconds": rec.get("seconds"),
    }


def running_takes(active_jobs) -> set[str]:
    out = set()
    for j in active_jobs or []:
        live = getattr(j, "live", None) or {}
        for tid, st in (live.get("takes") or {}).items():
            if st.get("status") in ("queued", "running"):
                out.add(tid)
    return out


def default_processes(n_cams: int) -> int:
    return max(1, min(n_cams or 1, (os.cpu_count() or 2) - 1))


def view(store, name: str, freemocap: dict, active_jobs=()) -> dict:
    data = store.load(name)
    paths = store.paths(name)
    calib = data["mocap"].get("calibration")
    calib_cams = _calibration_cameras(paths, calib)
    running = running_takes(active_jobs)
    takes = [describe_take(e, data, calib_cams, running) for e in exported_takes(paths)]
    suggested = next((t["take_id"] for t in takes if "calib" in (t["name"] or "").lower()), None)
    n_cams = len(calib_cams) if calib_cams else max((len(t["cameras"]) for t in takes), default=0)
    if not n_cams:
        n_cams = len([c for c in data.get("cameras") or [] if c.get("include", True)])
    return {
        "freemocap": freemocap,
        "cpu_count": os.cpu_count() or 1,
        "default_processes": default_processes(n_cams),
        "options": data["mocap"]["options"],
        "calibration": calib,
        "suggested_calibration_take": suggested,
        "takes": takes,
    }


def find_take(store, name: str, take_id: str) -> tuple[dict, dict]:
    """(exported entry, described take) for ``take_id``, or MocapError."""
    data = store.load(name)
    paths = store.paths(name)
    calib_cams = _calibration_cameras(paths, data["mocap"].get("calibration"))
    for e in exported_takes(paths):
        if e["info"]["id"] == take_id:
            return e, describe_take(e, data, calib_cams, set())
    raise MocapError(f"take {take_id!r} hasn't been exported")


# ---------------------------------------------------------------------------
# options

def normalize_options(payload: dict, current: dict) -> dict:
    opts = dict(current)
    if "processes" in payload:
        v = payload["processes"]
        opts["processes"] = None if v in (None, "", 0) else max(1, min(64, int(v)))
    if "blender" in payload:
        opts["blender"] = bool(payload["blender"])
    if "blender_exe" in payload:
        v = payload["blender_exe"]
        opts["blender_exe"] = (str(v).strip() or None) if v is not None else None
    return opts


# ---------------------------------------------------------------------------
# calibrate

def run_calibrate(store, name: str, job, take: dict, board: str, square_mm: float, groundplane: bool) -> dict:
    """``take`` is the described take (see :func:`describe_take`)."""
    paths = store.paths(name)
    take_dir = paths.output / take["dir"]
    n_cams = len(take["cameras"])
    bars = runner.Bars()
    job.set_progress(0.02, "starting FreeMoCap")

    def on_tqdm(bar):
        bars.update(bar)
        f = bars.fraction(n_cams)
        job.set_progress(0.05 + 0.8 * f, f"detecting the board: {round(f * 100)}%")

    def on_event(ev):
        if ev.get("type") == "stage" and ev.get("stage") == "detecting_board":
            job.set_progress(0.05, "detecting the board")
        elif ev.get("type") == "calibration_done":
            job.set_progress(0.95, "calibrated")

    args = [take_dir, "--square-mm", f"{float(square_mm):g}", "--board", board]
    if groundplane:
        args.append("--groundplane")
    job.log(f"calibrating from {take['name']} ({n_cams} cameras, board {board}, {float(square_mm):g} mm squares"
            f"{', board on the floor' if groundplane else ''})")
    res = runner.run_headless("calibrate", args, job, on_event=on_event, on_tqdm=on_tqdm)
    if not res.get("ok"):
        raise RuntimeError(res.get("error") or "calibration failed")

    toml_abs = Path(res["toml"]).resolve()
    try:
        toml_rel = toml_abs.relative_to(paths.root.resolve()).as_posix()
    except ValueError:
        toml_rel = str(toml_abs)
    calib = {
        "take_id": take["take_id"],
        "toml": toml_rel,
        "board": board,
        "square_mm": float(square_mm),
        "groundplane": bool(groundplane),
        "groundplane_result": res.get("groundplane"),
        "reprojection_error_px": res.get("reprojection_error_px"),
        "cameras": res.get("cameras") or [],
        "created_at": time.time(),
        "freemocap": config.FMC_VERSION,
    }
    with store.edit(name) as data:
        data["mocap"]["calibration"] = calib
    err = calib["reprojection_error_px"]
    job.set_progress(1.0, f"reprojection error {err:.2f} px" if isinstance(err, (int, float)) else "calibrated")
    return calib


# ---------------------------------------------------------------------------
# process

def run_process(store, name: str, job, takes: list[dict], calib: dict, processes: int,
                blender: bool = False, blender_exe: str | None = None,
                delete: dict | None = None) -> dict:
    """Process ``takes`` (described takes) in one FreeMoCap run. ``delete`` maps take id →
    leftover video names to remove first (the user confirmed it)."""
    paths = store.paths(name)
    for tid, files in (delete or {}).items():
        tk = next((t for t in takes if t["take_id"] == tid), None)
        if tk is None:
            continue
        for fn in files:
            p = paths.output / tk["dir"] / VIDEO_DIR / fn
            if p.parent.resolve() != (paths.output / tk["dir"] / VIDEO_DIR).resolve() or not p.is_file():
                continue
            p.unlink()
            job.log(f"{tk['name']}: deleted leftover {VIDEO_DIR}/{fn}", "warn")

    by_dir = {t["dir"]: t for t in takes}
    live = {"current": None, "takes": {t["take_id"]: {"stage": "queued", "fraction": 0.0, "status": "queued", "error": None}
                                         for t in takes}}
    job.live = live
    bars = runner.Bars()
    toml_abs = (paths.root / calib["toml"]).resolve()
    snapshot_calib = {"toml": calib["toml"], "reprojection_error_px": calib.get("reprojection_error_px"),
                      "created_at": calib.get("created_at")}

    def progress():
        total = sum(st["fraction"] for st in live["takes"].values()) / max(1, len(takes))
        cur = live["current"]
        tk = next((t for t in takes if t["take_id"] == cur), None)
        msg = f"{tk['name']}: {live['takes'][cur]['stage']}" if tk else "starting FreeMoCap"
        job.set_progress(total, msg)

    def record(tid: str, entry: dict) -> None:
        with store.edit(name) as data:
            data["mocap"]["takes"][tid] = entry

    def on_event(ev):
        t = ev.get("type")
        tk = by_dir.get(ev.get("take"))
        if tk is None:
            return
        tid = tk["take_id"]
        st = live["takes"][tid]
        if t == "take_start":
            live["current"] = tid
            bars.reset()
            st.update(status="running", stage="starting", fraction=0.0)
        elif t == "stage" and ev.get("scope", "take") == "take":
            stage = ev.get("stage")
            st["stage"] = stage
            st["fraction"] = max(st["fraction"], STAGE_FRACTION.get(stage, st["fraction"]))
        elif t == "take_done":
            take_dir = paths.output / tk["dir"]
            now = time.time()
            info = {
                "freemocap": config.FMC_VERSION,
                "calibration": snapshot_calib,
                "cameras": ev.get("cameras") or tk["cameras"],
                "subset": bool(ev.get("subset")),
                "toml": ev.get("toml"),
                "processes": processes,
                "seconds": ev.get("seconds"),
                "processed_at": now,
                "videos": snapshot_videos(take_dir),
            }
            write_json_atomic(take_dir / MOCAP_INFO, info)
            record(tid, {"status": "done", "processed_at": now, "error": None, "subset": info["subset"],
                         "cameras": info["cameras"], "seconds": info["seconds"]})
            st.update(status="done", stage="done", fraction=1.0, error=None)
        elif t == "take_failed":
            err = ev.get("error") or "failed"
            record(tid, {"status": "failed", "processed_at": time.time(), "error": err, "subset": None,
                         "cameras": tk["cameras"], "seconds": None})
            st.update(status="failed", stage="failed", fraction=1.0, error=err)
        progress()

    def on_tqdm(bar):
        cur = live["current"]
        if cur is None:
            return
        st = live["takes"][cur]
        if st["stage"] not in ("starting", "tracking_2d"):
            return
        bars.update(bar)
        n = len(next(t for t in takes if t["take_id"] == cur)["cameras"])
        st["fraction"] = max(st["fraction"], TRACKING_SHARE * bars.fraction(n))
        progress()

    args = [paths.output / t["dir"] for t in takes] + ["--toml", toml_abs, "--processes", int(processes)]
    if blender:
        args.append("--blender")
        if blender_exe:
            args += ["--blender-exe", blender_exe]
    job.log(f"processing {len(takes)} take(s) with {calib['toml']} ({processes} tracking process(es))")
    progress()
    try:
        res = runner.run_headless("process", args, job, on_event=on_event, on_tqdm=on_tqdm)
    except BaseException:
        for st in live["takes"].values():
            if st["status"] in ("queued", "running"):
                st.update(status="cancelled", stage="cancelled")
        raise

    for tk in takes:
        st = live["takes"][tk["take_id"]]
        if st["status"] in ("queued", "running"):   # the run ended before reporting this take
            err = res.get("error") or "FreeMoCap stopped before this take finished"
            record(tk["take_id"], {"status": "failed", "processed_at": time.time(), "error": err, "subset": None,
                                   "cameras": tk["cameras"], "seconds": None})
            st.update(status="failed", stage="failed", fraction=1.0, error=err)
    live["current"] = None
    done = [t["name"] for t in takes if live["takes"][t["take_id"]]["status"] == "done"]
    failed = {t["name"]: live["takes"][t["take_id"]]["error"] for t in takes
              if live["takes"][t["take_id"]]["status"] == "failed"}
    if failed:
        raise RuntimeError(f"{len(failed)} of {len(takes)} take(s) failed: "
                           + "; ".join(f"{k}: {v}" for k, v in failed.items()))
    job.set_progress(1.0, f"{len(done)} take(s) processed")
    return {"processed": done, "failed": failed, "log": res.get("log")}
