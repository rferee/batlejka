"""Full-quality stills for Cut Takes' frame viewer: one frame at full resolution, as PNG.

- a source file's frame, upright the way the export makes it (its metadata rotation, then
  the camera's), read straight from source/ (never written to, R4);
- a frame of what FreeMoCap drew on a take (previews.py), from its original 4K video.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from fastapi import APIRouter, HTTPException
from fastapi.responses import Response

from . import ffmpeg_tools as ff
from . import previews
from .app_state import require_session, store
from .exporter import ROTATE

router = APIRouter()


def _png(src: Path, t: float, vf: list[str]) -> Response:
    args: list = ["ffmpeg", "-v", "error", "-ss", f"{max(0.0, t):.6f}", "-i", src]
    if vf:
        args += ["-vf", ",".join(vf)]
    args += ["-frames:v", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"]
    try:
        r = ff.run(args, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        raise HTTPException(500, f"couldn't read the frame: {e}")
    if r.returncode != 0 or not r.stdout:
        err = (r.stderr or b"").decode("utf-8", "replace").strip()[-300:]
        raise HTTPException(500, f"couldn't read the frame: {err or 'no frame at that time'}")
    return Response(r.stdout, media_type="image/png", headers={"Cache-Control": "no-store"})


def _clamp(t: float, duration: float) -> float:
    """Inside the video: ffmpeg finds no frame at or past its very end."""
    return min(max(0.0, t), max(0.0, duration - 0.05)) if duration > 0 else max(0.0, t)


@router.get("/api/sessions/{s}/files/{fid}/frame")
def file_frame(s: str, fid: str, t: float = 0.0):
    """The frame of source file ``fid`` at ``t`` seconds into it."""
    name = require_session(s)
    data = store.load(name)
    f = next((x for x in data["files"] if x.get("id") == fid), None)
    if f is None:
        raise HTTPException(404, f"no such file: {fid}")
    cam = next((c for c in data.get("cameras") or [] if c.get("id") == f.get("camera")), None) or {}
    rot = int(cam.get("rotation") or 0) % 360
    return _png(store.paths(name).source_file(f["rel"]), _clamp(t, float(f["probe"].get("duration") or 0)),
                [ROTATE[rot]] if rot in ROTATE else [])


@router.get("/api/sessions/{s}/previews/{take_id}/{camera_id}/frame")
def preview_frame(s: str, take_id: str, camera_id: str, t: float = 0.0):
    """The frame of FreeMoCap's video of a take's camera at ``t`` seconds into the take;
    it comes from the exported take, so it's upright already."""
    name = require_session(s)
    paths = store.paths(name)
    take = next((x for x in previews.takes(paths, store.load(name)) if x["take_id"] == take_id), None)
    cam = (take or {}).get("cameras", {}).get(camera_id)
    if cam is None:
        raise HTTPException(404, "FreeMoCap made no video for that take's camera")
    return _png(cam["src"], _clamp(t, float((take["end"] or 0) - (take["start"] or 0))), [])
