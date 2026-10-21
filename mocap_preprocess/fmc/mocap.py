"""Session-level FreeMoCap calibration profiles and processing.

A session keeps any number of calibration *profiles* (``mocap.profiles``): one ChArUco
board setup (calibration take, board, square size, ground plane) and, once run, its
FreeMoCap result. One profile is *active* (``mocap.active_profile``); every take uses it
unless the take picks another (``mocap.takes[<id>].profile``). A profile's TOML is kept
in ``output/calibrations/<name>.toml``, so calibrating another profile on the same take
(FreeMoCap always writes ``<take>/<take>_camera_calibration.toml``) can't overwrite it.

Lists the exported takes (``output/<dir>/take_info.json``), matches their cameras against
their profile, works out whether a take's mocap is up to date (``mocap_info.json``
snapshot of the synchronized videos and the profile's calibration), and runs the
calibrate / process jobs through :mod:`.runner`. The session lock is only held for short
writes, never while FreeMoCap runs.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import time
import tomllib
from pathlib import Path

from .. import config
from ..ffmpeg_tools import Cancelled
from ..session_store import sanitize_filename, write_json_atomic
from . import runner

VIDEO_DIR = "synchronized_videos"
MOCAP_INFO = "mocap_info.json"
CALIB_DIR = "calibrations"          # output/calibrations/<profile name>.toml
JOB_CALIBRATE, JOB_PROCESS = "mocap_calibrate", "mocap_process"     # the two job kinds this module runs
BOARDS = ("7x5", "5x3")
SETTINGS = ("take_id", "board", "square_mm", "groundplane")
# how far into a take each FreeMoCap stage is (2D tracking fills 0 → 0.75 from the tqdm bars)
STAGE_FRACTION = {"tracking_2d": 0.0, "triangulating": 0.8, "skeleton": 0.88, "saving": 0.95, "blender": 0.97}
TRACKING_SHARE = 0.75
_NAME_BAD = re.compile(r"[^A-Za-z0-9._-]+")


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


def _rebuilt_info(d: Path, data: dict | None) -> dict | None:
    """A take_info for an export folder that lost its take_info.json (deleted by hand or by
    another tool): the session take its folder is named after, and the videos on disk."""
    vids = d / VIDEO_DIR
    if data is None or not vids.is_dir():
        return None
    mp4s = sorted((p for p in vids.iterdir() if p.is_file() and p.suffix.lower() == ".mp4"), key=lambda p: p.name.lower())
    take = next((t for t in data.get("takes") or [] if sanitize_filename(t["name"], t["id"]).lower() == d.name.lower()), None)
    if not mp4s or take is None:
        return None
    by_file = {f"{sanitize_filename(c['name'], c['id'])}_synced".lower(): c for c in data.get("cameras") or []}
    cameras = []
    for p in mp4s:
        cam = by_file.get(p.stem.lower())
        cameras.append({"camera_id": cam["id"] if cam else None, "name": cam["name"] if cam else p.stem,
                        "output": f"{VIDEO_DIR}/{p.name}"})
    return {"take": take["name"], "id": take["id"], "start": take["start"], "end": take["end"],
            "duration": take["end"] - take["start"], "frames": None, "layout": "freemocap",
            "exported_at": max(p.stat().st_mtime for p in mp4s), "cameras": cameras, "rebuilt": True}


def exported_takes(paths, data: dict | None = None) -> list[dict]:
    """``{dir, path, info}`` for every folder in output/ with a take_info.json. When
    several folders carry the same take id (renamed and exported again), the newest wins.
    With the session ``data``, a folder that lost its take_info.json still counts (see
    _rebuilt_info), unless another folder has the real thing for that take."""
    best: dict[str, dict] = {}
    out = paths.output
    if not out.is_dir():
        return []
    for d in sorted(out.iterdir(), key=lambda x: x.name.lower()):
        if not d.is_dir():
            continue
        info = _read_json(d / "take_info.json") or _rebuilt_info(d, data)
        if not info or not info.get("id"):
            continue
        entry = {"dir": d.name, "path": d, "info": info}
        prev = best.get(info["id"])
        rank = (not info.get("rebuilt"), info.get("exported_at") or 0)
        if prev is None or rank > (not prev["info"].get("rebuilt"), prev["info"].get("exported_at") or 0):
            best[info["id"]] = entry
    return sorted(best.values(), key=lambda e: (e["info"].get("start") or 0, e["dir"].lower()))


def _rel(paths, p: Path) -> str:
    p = Path(p).resolve()
    try:
        return p.relative_to(paths.root.resolve()).as_posix()
    except ValueError:
        return str(p)


def _in_calib_dir(paths, rel: str | None) -> Path | None:
    """The profile TOML's path if it lives in output/calibrations/ (the files we own)."""
    if not rel:
        return None
    p = (paths.root / rel).resolve()
    return p if p.parent == (paths.output / CALIB_DIR).resolve() else None


# ---------------------------------------------------------------------------
# profiles

def profiles(data: dict) -> list[dict]:
    return data["mocap"].setdefault("profiles", [])


def get_profile(data: dict, pid) -> dict | None:
    return next((p for p in profiles(data) if p.get("id") == pid), None)


def calibrated(p: dict | None) -> bool:
    """Has a usable result (a failed *re*calibration keeps the previous one)."""
    return bool(p and p.get("toml") and p.get("created_at") and p.get("status") in ("calibrated",))


def active_profile(data: dict) -> dict | None:
    return get_profile(data, data["mocap"].get("active_profile"))


def effective_profile(data: dict, tid: str) -> dict | None:
    """The profile a take is processed with: its own pick, else the active one."""
    rec = (data["mocap"].get("takes") or {}).get(tid) or {}
    p = get_profile(data, rec.get("profile")) if rec.get("profile") else None
    return p or active_profile(data)


def clean_name(name) -> str:
    s = _NAME_BAD.sub("-", str(name or "").strip()).strip("-.")[:60]
    if not s:
        raise MocapError("the profile needs a name (letters, digits, . _ -)")
    return s


def unique_name(data: dict, base: str, exclude=None) -> str:
    names = {p["name"] for p in profiles(data) if p.get("id") != exclude}
    n, i = base, 2
    while n in names:
        n = f"{base}-{i}"
        i += 1
    return n


def _new_id(data: dict) -> str:
    used = {p.get("id") for p in profiles(data)}
    i = len(used) + 1
    while f"p{i}" in used:
        i += 1
    return f"p{i}"


def _settings(p: dict) -> dict:
    return {k: p.get(k) for k in SETTINGS}


def _check_settings(paths, data: dict, patch: dict) -> dict:
    board = str(patch.get("board") or "7x5")
    if board not in BOARDS:
        raise MocapError(f"board must be one of {', '.join(BOARDS)}")
    v = patch.get("square_mm")
    if v in (None, ""):
        square = None
    else:
        try:
            f = float(v)
        except (TypeError, ValueError):
            raise MocapError("square size must be a number of mm")
        if not (f > 0 and f < 10000):
            raise MocapError("square size (mm) must be > 0")
        square = round(f, 3)
    tid = patch.get("take_id")
    if tid not in {e["info"]["id"] for e in exported_takes(paths, data)}:
        raise MocapError(f"take {tid!r} hasn't been exported")
    return {"board": board, "square_mm": square, "groundplane": bool(patch.get("groundplane", False)), "take_id": tid}


def create_profile(paths, data: dict, payload: dict) -> dict:
    name = clean_name(payload.get("name"))
    if any(p["name"] == name for p in profiles(data)):
        raise MocapError(f"a profile named {name!r} already exists")
    patch = dict(payload)
    if not patch.get("take_id"):
        patch["take_id"] = suggested_take(exported_takes(paths, data)) or None
    s = _check_settings(paths, data, patch)
    now = time.time()
    p = {"id": _new_id(data), "name": name, **s, "status": "draft", "toml": None, "reprojection_error_px": None,
         "cameras": [], "created_at": None, "updated_at": now, "error": None, "groundplane_result": None,
         "freemocap": None, "calibrated_with": None}
    profiles(data).append(p)
    return p


def delete_profile(paths, data: dict, pid: str) -> None:
    p = get_profile(data, pid)
    if p is None:
        raise LookupError(pid)
    if data["mocap"].get("active_profile") == pid:
        raise MocapError("that's the active profile — make another one active first")
    data["mocap"]["profiles"] = [q for q in profiles(data) if q["id"] != pid]
    for rec in (data["mocap"].get("takes") or {}).values():
        if rec.get("profile") == pid:
            rec.pop("profile", None)          # back to the active profile
    f = _in_calib_dir(paths, p.get("toml"))
    if f is not None:
        f.unlink(missing_ok=True)


def set_active(data: dict, pid: str) -> None:
    p = get_profile(data, pid)
    if p is None:
        raise LookupError(pid)
    if not calibrated(p):
        raise MocapError(f"{p['name']} isn't calibrated yet — run it first")
    data["mocap"]["active_profile"] = pid


def set_take_profile(data: dict, tid: str, pid) -> None:
    takes = data["mocap"].setdefault("takes", {})
    rec = takes.setdefault(tid, {})
    if pid in (None, ""):
        rec.pop("profile", None)
        return
    p = get_profile(data, pid)
    if p is None:
        raise LookupError(pid)
    if not calibrated(p):
        raise MocapError(f"{p['name']} isn't calibrated yet")
    rec["profile"] = pid


def suggested_take(exported: list[dict]) -> str | None:
    """The first exported take named like a calibration."""
    return next((e["info"]["id"] for e in exported if "calib" in (e["info"].get("take") or e["dir"]).lower()), None)


# ---------------------------------------------------------------------------
# view

def _profile_cameras(paths, p: dict | None) -> list[str] | None:
    if not calibrated(p):
        return None
    f = paths.root / p["toml"]
    if not f.is_file():
        return None
    try:
        return read_toml_cameras(f)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _stale_reason(entry: dict, prof: dict | None, params: dict | None = None) -> str | None:
    take_dir = entry["path"]
    info = _read_json(take_dir / MOCAP_INFO)
    if info is None:
        return "mocap_info.json missing"
    if not any((take_dir / "output_data").glob("*_body_3d_xyz.npy")):
        return "output_data missing"
    if (info.get("videos") or {}) != snapshot_videos(take_dir):
        return "videos re-exported"
    used = info.get("calibration") or {}
    if not calibrated(prof):
        return "calibration changed"
    if used.get("profile") and used["profile"] != prof["id"]:
        return "other calibration selected"
    if used.get("created_at") != prof.get("created_at"):
        return "calibration changed"
    if params is not None and changed_params(info.get("params"), params):
        return "processing settings changed"
    return None


def changed_params(used: dict | None, params: dict) -> list[str]:
    """Settings that differ from a run's; runs from before settings existed used FreeMoCap's defaults.
    The framerate only counts beyond 0.5 % (a 30 fps filter on 29.97 fps footage is the same filter)."""
    before = {**FMC_DEFAULTS, **(used or {})}
    out = []
    for k, v in params.items():
        b = before.get(k)
        if k == "framerate" and isinstance(b, (int, float)) and abs(b - v) <= 0.005 * v:
            continue
        if b != v:
            out.append(k)
    return out


def skipped_stage_missing(take_dir: Path, params: dict) -> str | None:
    """FreeMoCap loads the earlier results when a stage is off; say so before it fails on a missing file."""
    raw = take_dir / "output_data" / "raw_data"
    if not params["run_image_tracking"] and not any(raw.glob("*_2dData_numCams_numFrames_numTrackedPoints_pixelXY.npy")):
        return "2D tracking is off, but the take has no 2D data from an earlier run"
    if not params["run_3d_triangulation"] and not any(raw.glob("*_3dData_numFrames_numTrackedPoints_spatialXYZ.npy")):
        return "3D triangulation is off, but the take has no 3D data from an earlier run"
    return None


def describe_take(entry: dict, data: dict, prof_cams: dict, running: set[str], calib_takes: set[str],
                  params: dict | None = None) -> dict:
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
    rec = (data["mocap"].get("takes") or {}).get(tid) or {}
    prof = effective_profile(data, tid)
    pcams = prof_cams.get(prof["id"]) if prof else None

    match = None
    if pcams is not None:
        known = [s for s in stems if s in pcams]
        match = {"n": len(known), "of": len(pcams),
                 "missing": [c for c in pcams if c not in stems],
                 "unknown": [s for s in stems if s not in pcams]}

    status = rec.get("status") or "not_processed"
    stale_reason = None
    if tid in running:
        status = "running"
    elif status == "done":
        stale_reason = _stale_reason(entry, prof, params)
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
        "is_calibration": tid in calib_takes,
        "profile": prof["id"] if prof else None,
        "profile_explicit": bool(rec.get("profile") and get_profile(data, rec.get("profile"))),
        "calib_match": match,
        "status": status,
        "stale_reason": stale_reason,
        "error": rec.get("error") if status == "failed" else None,
        "processed_at": rec.get("processed_at"),
        "processed_profile": rec.get("processed_profile"),
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


def describe_profile(p: dict, data: dict, exported: dict, running_pid, cal_job, used_by: int) -> dict:
    take = exported.get(p.get("take_id"))
    take_cams = len([c for c in (take["info"].get("cameras") or []) if c.get("output")]) if take else None
    cw = p.get("calibrated_with")
    out = {k: p.get(k) for k in ("id", "name", "take_id", "board", "square_mm", "groundplane", "toml",
                                 "reprojection_error_px", "cameras", "created_at", "updated_at", "error",
                                 "groundplane_result", "freemocap", "calibrated_with")}
    out.update({
        "status": "running" if p.get("id") == running_pid else p.get("status") or "draft",
        "calibrated": calibrated(p),
        "active": p.get("id") == data["mocap"].get("active_profile"),
        "take_name": ((data_take_name(data, p.get("take_id")) or (take["info"].get("take") if take else None))
                      or p.get("take_id")),
        "take_exported": take is not None,
        "take_cameras": take_cams,
        "settings_changed": bool(cw) and cw != _settings(p),
        "take_reexported": bool(take and p.get("created_at") and (take["info"].get("exported_at") or 0) > p["created_at"] + 1),
        "used_by": used_by,
        "progress": ({"fraction": round(cal_job.progress, 4), "message": cal_job.message}
                     if cal_job is not None and p.get("id") == running_pid else None),
    })
    return out


def data_take_name(data: dict, tid) -> str | None:
    return next((t.get("name") for t in data.get("takes") or [] if t.get("id") == tid), None)


def view(store, name: str, freemocap: dict, process_jobs=(), calibrate_jobs=()) -> dict:
    data = store.load(name)
    paths = store.paths(name)
    exported = exported_takes(paths, data)
    by_id = {e["info"]["id"]: e for e in exported}
    cal_job = next(iter(calibrate_jobs or []), None)
    running_pid = ((getattr(cal_job, "live", None) or {}).get("profile")) if cal_job is not None else None
    prof_cams = {p["id"]: _profile_cameras(paths, p) for p in profiles(data)}
    calib_takes = {p.get("take_id") for p in profiles(data)}
    running = running_takes(process_jobs)
    params = effective_params(data)
    takes = [describe_take(e, data, prof_cams, running, calib_takes, params) for e in exported]
    used = {}
    for t in takes:
        if t["profile"] and not t["is_calibration"]:
            used[t["profile"]] = used.get(t["profile"], 0) + 1
    profs = [describe_profile(p, data, by_id, running_pid, cal_job, used.get(p["id"], 0)) for p in profiles(data)]
    act = active_profile(data)
    # what a run uses without a setting: one process per camera of the takes processed
    n_cams = max((len(t["cameras"]) for t in takes if not t["is_calibration"]), default=0)
    if not n_cams:
        n_cams = len(prof_cams.get(act["id"]) or []) if act else 0
    if not n_cams:
        n_cams = len([c for c in data.get("cameras") or [] if c.get("include", True)])
    return {
        "freemocap": freemocap,
        "cpu_count": os.cpu_count() or 1,
        "default_processes": default_processes(n_cams),
        "options": data["mocap"]["options"],
        "params": params,
        "param_defaults": param_defaults(data),
        "profiles": profs,
        "active_profile": data["mocap"].get("active_profile"),
        "suggested_calibration_take": suggested_take(exported),
        "takes": takes,
    }


def find_take(store, name: str, take_id: str) -> tuple[dict, dict]:
    """(exported entry, described take) for ``take_id``, or MocapError."""
    data = store.load(name)
    paths = store.paths(name)
    prof_cams = {p["id"]: _profile_cameras(paths, p) for p in profiles(data)}
    calib_takes = {p.get("take_id") for p in profiles(data)}
    params = effective_params(data)
    for e in exported_takes(paths, data):
        if e["info"]["id"] == take_id:
            return e, describe_take(e, data, prof_cams, set(), calib_takes, params)
    raise MocapError(f"take {take_id!r} hasn't been exported")


# ---------------------------------------------------------------------------
# FreeMoCap processing settings (mocap.options.params: only what differs from the defaults)

YOLO_SIZES = ("nano", "small", "medium", "large", "extra_large", "high_res")
BUFFER_METHODS = ("buffer_by_box_size", "buffer_by_image_size")
# FreeMoCap 1.8.2's own defaults (what a run without settings used, before settings existed)
FMC_DEFAULTS = {
    "run_image_tracking": True, "mediapipe_model_complexity": 2, "min_detection_confidence": 0.5,
    "min_tracking_confidence": 0.5, "static_image_mode": False, "use_yolo_crop_method": False,
    "yolo_model_size": "nano", "bounding_box_buffer_percentage": 10.0, "buffer_size_method": "buffer_by_box_size",
    "run_3d_triangulation": True, "minimum_cameras_for_triangulation": 3, "flatten_single_camera_data": True,
    "use_triangulate_outlier_rejection": False, "maximum_cameras_to_drop": 1, "target_reprojection_error": 0.01,
    "run_butterworth_filter": True, "framerate": 30.0, "cutoff_frequency": 7.0, "order": 4,
}
# kind, then (lo, hi) for numbers or the allowed values
PARAM_SPEC = {
    "run_image_tracking": ("bool",), "static_image_mode": ("bool",), "use_yolo_crop_method": ("bool",),
    "run_3d_triangulation": ("bool",), "flatten_single_camera_data": ("bool",),
    "use_triangulate_outlier_rejection": ("bool",), "run_butterworth_filter": ("bool",),
    "mediapipe_model_complexity": ("choice", (0, 1, 2)),
    "yolo_model_size": ("choice", YOLO_SIZES),
    "buffer_size_method": ("choice", BUFFER_METHODS),
    "min_detection_confidence": ("float", 0.0, 1.0),
    "min_tracking_confidence": ("float", 0.0, 1.0),
    "bounding_box_buffer_percentage": ("float", 0.0, 100.0),
    "minimum_cameras_for_triangulation": ("int", 2, 32),
    "maximum_cameras_to_drop": ("int", 0, 31),
    "target_reprojection_error": ("float", 0.0001, 10.0),
    "framerate": ("float", 1.0, 1000.0),
    "cutoff_frequency": ("float", 0.1, 500.0),
    "order": ("int", 1, 10),
}


def session_fps(data: dict) -> float:
    from ..ffmpeg_tools import fps_float

    return round(fps_float(data["settings"].get("target_fps")) or 30.0, 3)


def param_defaults(data: dict) -> dict:
    """FreeMoCap's defaults, except the filter framerate: the session's fps (FreeMoCap assumes 30,
    which on 59.94 fps footage would double the real cutoff)."""
    return {**FMC_DEFAULTS, "framerate": session_fps(data)}


def _clean_param(key: str, v):
    spec = PARAM_SPEC[key]
    kind = spec[0]
    if kind == "bool":
        return bool(v)
    if kind == "choice":
        allowed = spec[1]
        if key == "mediapipe_model_complexity":
            try:
                v = int(v)
            except (TypeError, ValueError):
                raise MocapError(f"{key} must be one of {allowed}")
        if v not in allowed:
            raise MocapError(f"{key} must be one of {', '.join(map(str, allowed))}")
        return v
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise MocapError(f"{key} must be a number")
    if f != f:
        raise MocapError(f"{key} must be a number")
    f = max(spec[1], min(spec[2], f))
    return int(round(f)) if kind == "int" else round(f, 4)


def normalize_params(payload, defaults: dict) -> dict:
    """Validated, clamped overrides; values equal to the default are dropped (null = default)."""
    if payload is None:
        return {}
    if not isinstance(payload, dict):
        raise MocapError("params must be an object")
    out = {}
    for k, v in payload.items():
        if k not in PARAM_SPEC:
            raise MocapError(f"unknown FreeMoCap setting: {k}")
        if v is None:
            continue
        c = _clean_param(k, v)
        if c != defaults[k]:
            out[k] = c
    return out


def effective_params(data: dict) -> dict:
    d = param_defaults(data)
    return {**d, **{k: v for k, v in (data["mocap"]["options"].get("params") or {}).items() if k in d}}


def check_params(p: dict) -> None:
    """What FreeMoCap would only reject halfway through a run."""
    if p["run_butterworth_filter"] and p["cutoff_frequency"] >= p["framerate"] / 2:
        raise MocapError(f"the Butterworth cutoff ({p['cutoff_frequency']:g} Hz) must be below half the framerate "
                         f"({p['framerate'] / 2:g} Hz)")


# ---------------------------------------------------------------------------
# options

def normalize_options(payload: dict, current: dict, defaults: dict | None = None) -> dict:
    opts = dict(current)
    if "params" in payload:
        opts["params"] = normalize_params(payload["params"], defaults or FMC_DEFAULTS)
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

def _protect_take_file(store, name: str, take_dir: Path) -> None:
    """FreeMoCap is about to overwrite ``<take>/<take>_camera_calibration.toml``: profiles
    that still point at it (migrated from before profiles) get their own copy first."""
    paths = store.paths(name)
    rel = _rel(paths, take_dir / f"{take_dir.name}_camera_calibration.toml")
    with store.edit(name) as data:
        for q in profiles(data):
            if q.get("toml") == rel and (paths.root / rel).is_file():
                dst = paths.output / CALIB_DIR / f"{q['name']}.toml"
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(paths.root / rel, dst)
                q["toml"] = _rel(paths, dst)


def run_calibrate(store, name: str, job, pid: str) -> dict:
    """Run FreeMoCap's calibration for profile ``pid`` and store its result."""
    data = store.load(name)
    p = get_profile(data, pid)
    if p is None:
        raise MocapError(f"no profile {pid!r}")
    _, take = find_take(store, name, p["take_id"])
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

    square, board, ground = float(p["square_mm"]), p["board"], bool(p["groundplane"])
    args = [take_dir, "--square-mm", f"{square:g}", "--board", board]
    if ground:
        args.append("--groundplane")
    job.log(f"calibrating profile {p['name']} from {take['name']} ({n_cams} cameras, board {board}, {square:g} mm squares"
            f"{', board on the floor' if ground else ''})")
    _protect_take_file(store, name, take_dir)

    def failed(msg: str):
        with store.edit(name) as d:
            q = get_profile(d, pid)
            if q is not None:
                q["error"] = msg
                q["updated_at"] = time.time()
                if not calibrated(q):
                    q["status"] = "failed"
        raise RuntimeError(msg)

    try:
        res = runner.run_headless("calibrate", args, job, on_event=on_event, on_tqdm=on_tqdm)
    except Cancelled:
        raise
    except Exception as e:  # noqa: BLE001 - FreeMoCap missing etc.: the profile shows it
        failed(str(e) or e.__class__.__name__)
    if not res.get("ok"):
        failed(res.get("error") or "calibration failed")

    dst = paths.output / CALIB_DIR / f"{p['name']}.toml"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(Path(res["toml"]), dst)
    now = time.time()
    with store.edit(name) as d:
        q = get_profile(d, pid)
        if q is None:                       # deleted meanwhile: nothing to store
            dst.unlink(missing_ok=True)
            raise RuntimeError("the profile was deleted while it calibrated")
        q.update({
            "status": "calibrated", "toml": _rel(paths, dst), "error": None,
            "reprojection_error_px": res.get("reprojection_error_px"), "cameras": res.get("cameras") or [],
            "created_at": now, "updated_at": now, "groundplane_result": res.get("groundplane"),
            "freemocap": config.FMC_VERSION,
            "calibrated_with": {"take_id": p["take_id"], "board": board, "square_mm": p["square_mm"], "groundplane": ground},
        })
        if not calibrated(active_profile(d)):
            d["mocap"]["active_profile"] = pid
        result = dict(q)
    err = result.get("reprojection_error_px")
    job.set_progress(1.0, f"{result['name']}: reprojection error {err:.2f} px" if isinstance(err, (int, float)) else "calibrated")
    return result


# ---------------------------------------------------------------------------
# process

def run_process(store, name: str, job, takes: list[dict], processes: int,
                blender: bool = False, blender_exe: str | None = None,
                delete: dict | None = None, params: dict | None = None) -> dict:
    """Process ``takes`` (described takes; each carries its ``profile``) in one job: one
    FreeMoCap run per profile. ``delete`` maps take id → leftover video names to remove
    first (the user confirmed it)."""
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

    live = {"current": None, "takes": {t["take_id"]: {"stage": "queued", "fraction": 0.0, "status": "queued", "error": None}
                                         for t in takes}}
    job.live = live
    bars = runner.Bars()
    groups: dict[str, list[dict]] = {}
    for t in takes:
        groups.setdefault(t["profile"], []).append(t)

    def progress():
        total = sum(st["fraction"] for st in live["takes"].values()) / max(1, len(takes))
        cur = live["current"]
        tk = next((t for t in takes if t["take_id"] == cur), None)
        msg = f"{tk['name']}: {live['takes'][cur]['stage']}" if tk else "starting FreeMoCap"
        job.set_progress(total, msg)

    def record(tid: str, entry: dict) -> None:
        with store.edit(name) as data:
            rec = data["mocap"].setdefault("takes", {}).setdefault(tid, {})
            rec.update(entry)               # keeps the take's own profile pick

    res = {}
    for pid, group in groups.items():
        prof = get_profile(store.load(name), pid)
        if not calibrated(prof):
            raise RuntimeError(f"profile {pid} isn't calibrated")
        toml_abs = (paths.root / prof["toml"]).resolve()
        snapshot_calib = {"profile": pid, "profile_name": prof["name"], "toml": prof["toml"],
                          "reprojection_error_px": prof.get("reprojection_error_px"), "created_at": prof.get("created_at")}
        by_dir = {t["dir"]: t for t in group}

        def on_event(ev, by_dir=by_dir, snapshot_calib=snapshot_calib, pid=pid):
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
                    "params": ev.get("params") or params,
                    "seconds": ev.get("seconds"),
                    "processed_at": now,
                    "videos": snapshot_videos(take_dir),
                }
                write_json_atomic(take_dir / MOCAP_INFO, info)
                record(tid, {"status": "done", "processed_at": now, "error": None, "subset": info["subset"],
                             "cameras": info["cameras"], "seconds": info["seconds"], "processed_profile": pid})
                st.update(status="done", stage="done", fraction=1.0, error=None)
            elif t == "take_failed":
                err = ev.get("error") or "failed"
                record(tid, {"status": "failed", "processed_at": time.time(), "error": err, "subset": None,
                             "cameras": tk["cameras"], "seconds": None, "processed_profile": pid})
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

        args = [paths.output / t["dir"] for t in group] + ["--toml", toml_abs, "--processes", int(processes)]
        if params:
            pfile = paths.pre / "fmc_params.json"          # one process job per session at a time
            pfile.parent.mkdir(parents=True, exist_ok=True)
            pfile.write_text(json.dumps(params, indent=1), encoding="utf-8")
            args += ["--params", pfile]
        if blender:
            args.append("--blender")
            if blender_exe:
                args += ["--blender-exe", blender_exe]
        job.log(f"processing {len(group)} take(s) with profile {prof['name']} ({prof['toml']}, "
                f"{processes} tracking process(es))")
        progress()
        try:
            res = runner.run_headless("process", args, job, on_event=on_event, on_tqdm=on_tqdm)
        except BaseException:
            for st in live["takes"].values():
                if st["status"] in ("queued", "running"):
                    st.update(status="cancelled", stage="cancelled")
            raise
        for tk in group:
            st = live["takes"][tk["take_id"]]
            if st["status"] in ("queued", "running"):   # the run ended before reporting this take
                err = res.get("error") or "FreeMoCap stopped before this take finished"
                record(tk["take_id"], {"status": "failed", "processed_at": time.time(), "error": err, "subset": None,
                                       "cameras": tk["cameras"], "seconds": None, "processed_profile": pid})
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


# ---------------------------------------------------------------------------
# take detail (Mocap step: what FreeMoCap produced for one take)

BODY_POINTS = 33          # MediaPipe pose landmarks come first in FreeMoCap's tracked points
_detail_cache: dict = {}  # (take dir, files' mtimes) -> computed quality


def _bone(body, a: int, b: int) -> list | None:
    import numpy as np

    d = np.linalg.norm(body[:, a] - body[:, b], axis=1)
    d = d[np.isfinite(d)]
    if not d.size:
        return None
    q1, med, q3 = np.percentile(d, [25, 50, 75])
    return [round(float(med), 1), round(float(q3 - q1) / 2, 1)]


def _quality(take_dir: Path) -> dict | None:
    """Numbers from FreeMoCap's own arrays: mean reprojection error of the body points,
    frames with the whole body, gaps its post-processing filled, two bone lengths, and
    how often each camera saw the body. Cached by the files' mtimes."""
    import numpy as np

    out = take_dir / "output_data"
    body_f = next(iter(sorted(out.glob("*_body_3d_xyz.npy"))), None)
    if body_f is None:
        return None
    raw = out / "raw_data"
    reproj_f = next(iter(sorted(raw.glob("*_3dData_numFrames_numTrackedPoints_reprojectionError.npy"))), None)
    xyz_f = next(iter(sorted(raw.glob("*_3dData_numFrames_numTrackedPoints_spatialXYZ.npy"))), None)
    px_f = next(iter(sorted(raw.glob("*_2dData_numCams_numFrames_numTrackedPoints_pixelXY.npy"))), None)
    files = [f for f in (body_f, reproj_f, xyz_f, px_f) if f is not None]
    key = (str(take_dir), tuple((f.name, f.stat().st_mtime) for f in files))
    if key in _detail_cache:
        return _detail_cache[key]
    q: dict = {}
    try:
        body = np.asarray(np.load(body_f, mmap_mode="r")[:, :BODY_POINTS, :], dtype=float)
        frames = int(body.shape[0])
        whole = np.isfinite(body).all(axis=(1, 2))
        q["frames"] = frames
        q["body_tracked_pct"] = round(float(whole.mean()) * 100, 1) if frames else None
        q["upper_arm_mm"] = _bone(body, 11, 13)       # left shoulder - left elbow
        q["thigh_mm"] = _bone(body, 23, 25)           # left hip - left knee
        if reproj_f is not None:
            r = np.asarray(np.load(reproj_f, mmap_mode="r")[:, :BODY_POINTS], dtype=float)
            # the median: a few wild points (FreeMoCap leaves values up to 1e11 px) would swamp a mean
            q["reprojection_median_px"] = round(float(np.nanmedian(r)), 1) if np.isfinite(r).any() else None
        if xyz_f is not None:
            rawb = np.asarray(np.load(xyz_f, mmap_mode="r")[:, :BODY_POINTS, :], dtype=float)
            if rawb.shape[0] == frames:
                filled = (~np.isfinite(rawb).all(axis=(1, 2))) & whole
                longest = cur = 0
                for f in filled:
                    cur = cur + 1 if f else 0
                    longest = max(longest, cur)
                q["gaps_filled_frames"] = int(filled.sum())
                q["longest_gap"] = int(longest)
        if px_f is not None:
            px = np.load(px_f, mmap_mode="r")
            seen = []
            for c in range(px.shape[0]):
                xy = np.asarray(px[c, :, :BODY_POINTS, 0], dtype=float)
                seen.append(round(float(np.isfinite(xy).any(axis=1).mean()) * 100, 1) if xy.size else None)
            q["camera_detected_pct"] = seen
    except (OSError, ValueError, IndexError) as e:
        q["error"] = f"couldn't read FreeMoCap's output: {e}"
    _detail_cache[key] = q
    return q


def _size(p: Path) -> int:
    if p.is_file():
        return p.stat().st_size
    return sum(f.stat().st_size for f in p.rglob("*") if f.is_file())


def _outputs(take_dir: Path) -> list[dict]:
    """FreeMoCap's files in the take folder, the many small ones grouped."""
    out = take_dir / "output_data"
    rows = []

    def add(name, paths):
        paths = [p for p in paths if p.exists()]
        if paths:
            rows.append({"name": name, "size": sum(_size(p) for p in paths)})

    for p in sorted(out.glob("*_body_3d_xyz.*")):
        add(f"output_data/{p.name}", [p])
    extra = [p for p in out.glob("*_3d_xyz.*") if "_body_" not in p.name]
    if extra:
        add(f"output_data/hands + face 3D ({len(extra)} files)", extra)
    for p in sorted(out.glob("*skeleton*")) + sorted(out.glob("*rigid_bones*")):
        add(f"output_data/{p.name}", [p])
    add("output_data/center_of_mass/", [out / "center_of_mass"])
    add("output_data/raw_data/", [out / "raw_data"])
    vids = sorted((take_dir / "annotated_videos").glob("*.mp4"))
    if vids:
        add(f"annotated_videos/ ({len(vids)} video{'s' if len(vids) != 1 else ''})", vids)
    for pat in ("*_by_frame.csv", "*_by_frame.json", "*_by_trajectory.csv", "*_frame_name_xyz.npy", "*.blend"):
        for p in sorted(take_dir.glob(pat)):
            add(p.name, [p])
    return rows


def take_detail(store, name: str, take_id: str) -> dict:
    entry, take = find_take(store, name, take_id)
    take_dir: Path = entry["path"]
    done = take["status"] in ("done", "stale")
    info = _read_json(take_dir / MOCAP_INFO) if done else None
    q = _quality(take_dir) if done else None
    if q and q.get("camera_detected_pct") is not None and info:
        q = {**q, "camera_detected": [{"camera": c, "pct": pct}
                                      for c, pct in zip(info.get("cameras") or [], q["camera_detected_pct"])]}
    blend = next(iter(sorted(take_dir.glob("*.blend"))), None)
    files = _outputs(take_dir) if take["status"] in ("done", "stale", "failed") else []
    return {
        "take_id": take_id,
        "dir": entry["dir"],
        "out_dir": f"output/{entry['dir']}/",
        "info": info,
        "quality": q,
        "files": files,
        "out_size": sum(f["size"] for f in files),
        "blend": blend.name if blend else None,
    }


def blend_path(store, name: str, take_id: str) -> Path:
    """The take's exported .blend, checked to be inside its folder."""
    entry, _ = find_take(store, name, take_id)
    take_dir = entry["path"].resolve()
    blend = next(iter(sorted(take_dir.glob("*.blend"))), None)
    if blend is None or blend.resolve().parent != take_dir:
        raise MocapError("this take has no .blend: tick Export .blend and process it again")
    return blend.resolve()
