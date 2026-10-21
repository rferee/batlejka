"""Preview videos for Cut Takes: what FreeMoCap drew on each exported take — the board
detections of a calibration take, the skeleton of a processed one — swapped in for the
proxies when Preview is on.

FreeMoCap writes those videos as 4K MPEG-4 Part 2, which browsers don't decode, so they are
re-encoded like proxies (H.264, the proxy size, short GOP for scrubbing) with the take's
audio from its exported video, into ``.preprocess/previews/<take id>/<camera id>.mp4``. A
sidecar JSON remembers the source's size and mtime, so a re-run calibration or mocap
makes the preview stale and it's built again.
"""
from __future__ import annotations

import glob
import json
from pathlib import Path
from urllib.parse import quote

from . import ffmpeg_tools as ff
from .fmc import mocap
from .session_store import write_json_atomic

CHARUCO_DIR = "charuco_annotated_videos"     # <stem>_charuco.mp4, from FreeMoCap's calibration
ANNOTATED_DIR = "annotated_videos"           # <stem>_mediapipe.mp4 (or another tracker), from processing
DEFAULT_LONG_SIDE = 480


def _annotated(take_dir: Path, kind: str, stem: str) -> Path | None:
    if kind == "calibration":
        p = take_dir / CHARUCO_DIR / f"{stem}_charuco.mp4"
        return p if p.is_file() else None
    found = sorted(Path(p) for p in glob.glob(str(take_dir / ANNOTATED_DIR / f"{glob.escape(stem)}_*.mp4")))
    found = [p for p in found if p.is_file()]
    return next((p for p in found if p.stem.endswith("_mediapipe")), found[0] if found else None)


def _stat(p: Path) -> dict:
    st = p.stat()
    return {"size": st.st_size, "mtime": round(st.st_mtime, 3)}


def long_side(data: dict) -> int:
    return int((data.get("settings") or {}).get("proxy_long_side") or DEFAULT_LONG_SIDE)


def preview_path(paths, tid: str, cid: str) -> Path:
    return paths.pre / "previews" / tid / f"{cid}.mp4"


def takes(paths, data: dict) -> list[dict]:
    """Every exported take FreeMoCap worked on: ``{take_id, kind, start, end, cameras}``, kind
    ``calibration`` (a calibrated profile's take, or one with board videos) or ``mocap``;
    cameras ``{camera_id: {name, src, synced, out, ready}}`` for the videos found on disk."""
    calibrated = {p.get("take_id") for p in mocap.profiles(data) if mocap.calibrated(p)}
    processed = {tid for tid, r in (data["mocap"].get("takes") or {}).items() if r.get("status") == "done"}
    L = long_side(data)
    out = []
    for e in mocap.exported_takes(paths, data):
        info, take_dir = e["info"], e["path"]
        tid = info["id"]
        cams = [c for c in info.get("cameras") or [] if c.get("output") and c.get("camera_id")]
        found = {}
        for kind in ("calibration", "mocap"):
            found[kind] = {}
            for c in cams:
                src = _annotated(take_dir, kind, Path(c["output"]).stem)
                if src is not None:
                    found[kind][c["camera_id"]] = (c, src)
        is_cal = tid in calibrated or bool(found["calibration"])
        is_mocap = tid in processed or bool(found["mocap"])
        if not (is_cal or is_mocap):
            continue
        kind = "calibration" if is_cal else "mocap"
        vids = found[kind] or found["mocap" if kind == "calibration" else "calibration"]
        cameras = {}
        for cid, (c, src) in vids.items():
            dst = preview_path(paths, tid, cid)
            side = cur = None
            try:
                side = json.loads(dst.with_suffix(".json").read_text(encoding="utf-8"))
                cur = _stat(src)
            except (OSError, ValueError):
                pass
            same = (isinstance(side, dict) and side.get("src") == str(src) and side.get("stat") == cur
                    and side.get("long_side") == L)
            # a calibration or run that was cancelled leaves a video without its index: unreadable
            broken = same and bool(side.get("failed"))
            ready = same and not broken and dst.is_file()
            cameras[cid] = {"name": c.get("name"), "src": src, "synced": take_dir / c["output"], "out": dst,
                            "ready": ready, "broken": broken, "version": int(dst.stat().st_mtime) if ready else None}
        out.append({"take_id": tid, "kind": kind, "start": info.get("start"), "end": info.get("end"),
                    "cameras": cameras})
    return out


def view(paths, data: dict, session: str) -> dict:
    listed = takes(paths, data)
    return {
        "takes": [{
            "take_id": t["take_id"], "kind": t["kind"], "start": t["start"], "end": t["end"],
            "cameras": {cid: {"name": c["name"], "ready": c["ready"], "broken": c["broken"],
                              "url": (f"/media/{quote(session)}/preview/{quote(t['take_id'])}/{quote(cid)}.mp4?v={c['version']}"
                                      if c["ready"] else None)}
                        for cid, c in t["cameras"].items()},
        } for t in listed],
        "missing": sum(1 for t in listed for c in t["cameras"].values() if not c["ready"] and not c["broken"]),
    }


def build(store, name: str, job, busy_takes=frozenset()) -> dict:
    """Encode the previews that are missing or stale. Takes FreeMoCap is still writing
    (``busy_takes``) wait for the next build."""
    paths = store.paths(name)
    data = store.load(name)
    L = long_side(data)
    todo = [(t, cid, c) for t in takes(paths, data) if t["take_id"] not in busy_takes
            for cid, c in t["cameras"].items() if not c["ready"] and not c["broken"]]
    built = failed = 0
    for i, (t, cid, c) in enumerate(todo):
        label = f"{i + 1}/{len(todo)} {c['name'] or cid} · {t['take_id']}"
        job.set_progress(i / max(1, len(todo)), label)
        dur = max(0.1, float((t["end"] or 0) - (t["start"] or 0)))
        dst: Path = c["out"]
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.with_name(dst.stem + ".tmp.mp4")
        args: list = ["ffmpeg", "-y", "-v", "error", "-i", c["src"]]
        if c["synced"].is_file():
            args += ["-i", c["synced"], "-map", "0:v:0", "-map", "1:a:0?"]
        else:
            args += ["-map", "0:v:0"]
        args += ["-vf", ff.scale_expr(L)] + ff.PROXY_ENCODE + ["-shortest"] + ff.PROXY_TAIL + [tmp]
        before = _stat(c["src"])
        try:
            rc, err = ff.run_progress(args, dur, lambda x, i=i: job.set_progress((i + x) / len(todo), label),
                                      job.cancel_event)
        except ff.Cancelled:
            tmp.unlink(missing_ok=True)
            raise
        if rc != 0 or not tmp.exists() or _stat(c["src"]) != before:
            tmp.unlink(missing_ok=True)
            failed += 1
            reason = err[-200:].strip() or "source changed while reading"
            job.log(f"{t['take_id']}/{c['src'].name}: no preview ({reason})", "warn")
            if _stat(c["src"]) == before:      # unreadable as it stands: don't retry until it changes
                write_json_atomic(dst.with_suffix(".json"), {"src": str(c["src"]), "stat": before, "long_side": L,
                                                             "failed": reason})
            continue
        try:
            ff.replace_with_retry(tmp, dst)
        except OSError as e:     # the old preview is still being streamed
            tmp.unlink(missing_ok=True)
            failed += 1
            job.log(f"{t['take_id']}/{c['src'].name}: can't replace the old preview ({e})", "warn")
            continue
        write_json_atomic(dst.with_suffix(".json"), {"src": str(c["src"]), "stat": before, "long_side": L})
        built += 1
    job.set_progress(1.0, f"{built} preview(s) made" + (f", {failed} failed" if failed else ""))
    job.log(f"done: {built} preview(s) made, {failed} failed, {len(busy_takes)} take(s) busy")
    return {"built": built, "failed": failed}
