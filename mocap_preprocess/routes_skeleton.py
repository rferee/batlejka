"""/api/sessions/{s}/mocap/takes/{take_id}/skeleton: a processed take's 3D skeleton for the
viewer in the Mocap step (FreeMoCap's ``output_data/*_body_3d_xyz.npy``, MediaPipe's 33 body
points in mm), downsampled for the browser. Read-only."""
from __future__ import annotations

import math
from pathlib import Path

import numpy as np
from fastapi import APIRouter, HTTPException

from . import ffmpeg_tools as ff
from .app_state import require_session, store
from .fmc import mocap

router = APIRouter()

LANDMARKS = 33
# MediaPipe Pose bones (POSE_CONNECTIONS)
CONNECTIONS = [
    [0, 1], [1, 2], [2, 3], [3, 7], [0, 4], [4, 5], [5, 6], [6, 8], [9, 10],
    [11, 12], [11, 13], [13, 15], [15, 17], [15, 19], [15, 21], [17, 19],
    [12, 14], [14, 16], [16, 18], [16, 20], [16, 22], [18, 20],
    [11, 23], [12, 24], [23, 24], [23, 25], [24, 26], [25, 27], [26, 28],
    [27, 29], [28, 30], [29, 31], [30, 32], [27, 31], [28, 32],
]


def _take(name: str, take_id: str) -> tuple[Path, dict]:
    for e in mocap.exported_takes(store.paths(name), store.load(name)):
        if str(e["info"].get("id")) == take_id:
            return Path(e["path"]), e["info"]
    raise HTTPException(404, f"take {take_id!r} hasn't been exported")


def _body_file(take_dir: Path, label: str) -> Path:
    out = take_dir / "output_data"
    found = []
    if out.is_dir():
        root = out.resolve()
        for p in out.glob("*_body_3d_xyz.npy"):
            rp = p.resolve()
            if rp.is_file() and rp.parent == root:     # nothing outside the take's output_data/
                found.append(rp)
    if not found:
        raise HTTPException(404, f"{label} hasn't been processed yet (no output_data/*_body_3d_xyz.npy)")
    return next((p for p in found if p.name.startswith("mediapipe_")), sorted(found)[0])


@router.get("/api/sessions/{s}/mocap/takes/{take_id}/skeleton")
def skeleton(s: str, take_id: str, max_fps: float = 30.0):
    name = require_session(s)
    take_dir, info = _take(name, take_id)
    label = info.get("take") or take_dir.name
    path = _body_file(take_dir, label)
    try:
        arr = np.load(path, allow_pickle=False, mmap_mode="r")
    except (OSError, ValueError) as e:
        raise HTTPException(500, f"can't read {path.name}: {e}")
    if arr.ndim != 3 or arr.shape[1] != LANDMARKS or arr.shape[2] != 3:
        raise HTTPException(500, f"{path.name}: expected frames × {LANDMARKS} × 3, got {tuple(arr.shape)}")

    fps = ff.fps_float(info.get("fps")) or 30.0
    max_fps = min(240.0, max(1.0, float(max_fps)))
    step = max(1, math.ceil(fps / max_fps - 1e-9))      # every step-th frame: ≤ max_fps
    sub = np.round(np.asarray(arr[::step], dtype=np.float64).reshape(-1, LANDMARKS * 3), 1)
    finite = np.isfinite(sub)
    rows = [[v if ok else None for v, ok in zip(row, oks)] for row, oks in zip(sub.tolist(), finite.tolist())]
    return {
        "take_id": take_id,
        "take": label,
        "file": path.name,
        "fps": fps / step,
        "source_fps": fps,
        "step": step,
        "frames": len(rows),
        "landmarks": LANDMARKS,
        "units": "mm",
        "connections": CONNECTIONS,
        "data": rows,            # one row per frame: x0, y0, z0, x1, … (null where FreeMoCap had no point)
    }
