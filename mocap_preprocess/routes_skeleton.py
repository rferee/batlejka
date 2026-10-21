"""/api/sessions/{s}/mocap/takes/{take_id}/skeleton: a processed take's 3D skeleton for the
viewer in the Mocap step: FreeMoCap's ``output_data/*_body_3d_xyz.npy`` (MediaPipe's 33 body
points in mm) plus the two 21-point hands when FreeMoCap wrote them, downsampled for the
browser. Read-only."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException

from . import ffmpeg_tools as ff
from .app_state import require_session, store
from .fmc import mocap

router = APIRouter()

BODY = 33
HAND = 21
# MediaPipe Pose bones (POSE_CONNECTIONS), each with the body part it belongs to (the viewer
# colours by part). The body's three stub hand points (17-22) are only drawn without hands.
BODY_BONES = [
    ([0, 1], "face"), ([1, 2], "face"), ([2, 3], "face"), ([3, 7], "face"),
    ([0, 4], "face"), ([4, 5], "face"), ([5, 6], "face"), ([6, 8], "face"), ([9, 10], "face"),
    ([11, 12], "torso"), ([11, 23], "torso"), ([12, 24], "torso"), ([23, 24], "torso"),
    ([11, 13], "arm_l"), ([13, 15], "arm_l"), ([12, 14], "arm_r"), ([14, 16], "arm_r"),
    ([23, 25], "leg_l"), ([25, 27], "leg_l"), ([27, 29], "leg_l"), ([27, 31], "leg_l"), ([29, 31], "leg_l"),
    ([24, 26], "leg_r"), ([26, 28], "leg_r"), ([28, 30], "leg_r"), ([28, 32], "leg_r"), ([30, 32], "leg_r"),
]
STUB_HAND_BONES = {"hand_l": [[15, 17], [15, 19], [15, 21], [17, 19]], "hand_r": [[16, 18], [16, 20], [16, 22], [18, 20]]}
# MediaPipe Hands (HAND_CONNECTIONS): wrist 0, thumb 1-4, index 5-8, middle 9-12, ring 13-16, pinky 17-20
HAND_BONES = [
    [0, 1], [1, 2], [2, 3], [3, 4], [0, 5], [5, 6], [6, 7], [7, 8], [5, 9], [9, 10], [10, 11], [11, 12],
    [9, 13], [13, 14], [14, 15], [15, 16], [13, 17], [0, 17], [17, 18], [18, 19], [19, 20],
]
BODY_WRIST = {"hand_l": 15, "hand_r": 16}


def _take(name: str, take_id: str) -> tuple[Path, dict]:
    for e in mocap.exported_takes(store.paths(name), store.load(name)):
        if str(e["info"].get("id")) == take_id:
            return Path(e["path"]), e["info"]
    raise HTTPException(404, f"take {take_id!r} hasn't been exported")


def _output_files(take_dir: Path, suffix: str) -> list[Path]:
    out = take_dir / "output_data"
    found = []
    if out.is_dir():
        root = out.resolve()
        for p in out.glob(f"*{suffix}"):
            rp = p.resolve()
            if rp.is_file() and rp.parent == root:     # nothing outside the take's output_data/
                found.append(rp)
    return sorted(found)


def _pick(files: list[Path]) -> Path | None:
    return next((p for p in files if p.name.startswith("mediapipe_")), files[0] if files else None)


def _load(path: Path, points: int) -> np.ndarray:
    try:
        arr = np.load(path, allow_pickle=False, mmap_mode="r")
    except (OSError, ValueError) as e:
        raise HTTPException(500, f"can't read {path.name}: {e}")
    if arr.ndim != 3 or arr.shape[1] != points or arr.shape[2] != 3:
        raise HTTPException(500, f"{path.name}: expected frames × {points} × 3, got {tuple(arr.shape)}")
    return arr


@router.get("/api/sessions/{s}/mocap/takes/{take_id}/skeleton")
def skeleton(s: str, take_id: str, max_fps: float = 30.0):
    name = require_session(s)
    take_dir, info = _take(name, take_id)
    label = info.get("take") or take_dir.name
    body_file = _pick(_output_files(take_dir, "_body_3d_xyz.npy"))
    if body_file is None:
        raise HTTPException(404, f"{label} hasn't been processed yet (no output_data/*_body_3d_xyz.npy)")
    body = _load(body_file, BODY)

    # the hands, when FreeMoCap tracked them and they cover the same frames
    parts = {"body": [0, BODY]}
    arrays = [body]
    bones = [(c, part) for c, part in BODY_BONES]
    for side, part in (("left", "hand_l"), ("right", "hand_r")):
        hand_file = _pick(_output_files(take_dir, f"_{side}_hand_3d_xyz.npy"))
        hand = _load(hand_file, HAND) if hand_file is not None else None
        if hand is None or hand.shape[0] != body.shape[0]:
            bones += [(c, part) for c in STUB_HAND_BONES[part]]
            continue
        base = sum(a.shape[1] for a in arrays)
        parts[f"{side}_hand"] = [base, base + HAND]
        arrays.append(hand)
        bones.append(([BODY_WRIST[part], base], part))
        bones += [([base + a, base + b], part) for a, b in HAND_BONES]
    points = sum(a.shape[1] for a in arrays)

    fps = ff.fps_float(info.get("fps")) or 30.0
    max_fps = min(240.0, max(1.0, float(max_fps)))
    step = max(1, math.ceil(fps / max_fps - 1e-9))      # every step-th frame: ≤ max_fps
    sub = np.concatenate([np.asarray(a[::step], dtype=np.float64) for a in arrays], axis=1)
    sub = np.round(sub.reshape(-1, points * 3), 1)
    finite = np.isfinite(sub)
    rows = [[v if ok else None for v, ok in zip(row, oks)] for row, oks in zip(sub.tolist(), finite.tolist())]
    return {
        "take_id": take_id,
        "take": label,
        "file": body_file.name,
        "fps": fps / step,
        "source_fps": fps,
        "step": step,
        "frames": len(rows),
        "landmarks": points,
        "parts": parts,                         # name -> [first, end) index into each row's points
        "units": "mm",
        "connections": [[a, b, part] for (a, b), part in bones],
        "data": rows,            # one row per frame: x0, y0, z0, x1, … (null where FreeMoCap had no point)
    }
