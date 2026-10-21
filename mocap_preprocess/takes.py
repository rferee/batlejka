"""Take handling: auto-detection from clip clusters (§6.7) and validation."""
from __future__ import annotations

import re

from .session_store import placed_video_files

CONTINUOUS_FRACTION = 0.6   # files this long relative to the session are ignored for clustering
MERGE_GAP = 2.0             # clips closer than this belong to the same take
MIN_TAKE = 1.0


def detect(data: dict) -> list[dict]:
    files = placed_video_files(data)
    if not files:
        return []
    spans = [(f, f["offset"], f["offset"] + float(f["probe"].get("duration") or 0)) for f in files]
    session = max(e for _, _, e in spans) - min(s for _, s, _ in spans)
    short = [x for x in spans if (x[2] - x[1]) < CONTINUOUS_FRACTION * session]
    if not short:
        return []
    short.sort(key=lambda x: x[1])
    clusters: list[list[tuple]] = []
    end = None
    for item in short:
        if clusters and item[1] <= end + MERGE_GAP:
            clusters[-1].append(item)
            end = max(end, item[2])
        else:
            clusters.append([item])
            end = item[2]

    takes = []
    for cluster in clusters:
        per_cam: dict[str, tuple[float, float]] = {}
        for f, s, e in cluster:
            key = f.get("camera") or f["id"]
            a, b = per_cam.get(key, (s, e))
            per_cam[key] = (min(a, s), max(b, e))
        start = max(a for a, _ in per_cam.values())
        end_ = min(b for _, b in per_cam.values())
        if end_ - start < MIN_TAKE:
            start = min(s for _, s, _ in cluster)
            end_ = max(e for _, _, e in cluster)
        takes.append({"start": round(start, 6), "end": round(end_, 6)})
    return [{"id": f"t{i}", "name": f"take_{i:02d}", "start": t["start"], "end": t["end"], "export": True}
            for i, t in enumerate(takes, 1)]


_TID = re.compile(r"^t\d{1,5}$")


def validate(takes_in) -> list[dict]:
    if not isinstance(takes_in, list):
        raise ValueError("takes must be a list")
    out, ids = [], set()
    for i, t in enumerate(takes_in):
        tid = str(t.get("id") or "")
        if not _TID.match(tid) or tid in ids:
            tid = next(f"t{k}" for k in range(1, 100000) if f"t{k}" not in ids
                       and not any(str(x.get("id")) == f"t{k}" for x in takes_in))
        ids.add(tid)
        start, end = float(t["start"]), float(t["end"])
        if not end > start:
            raise ValueError(f"take {t.get('name')!r}: end must be after start")
        name = str(t.get("name") or "").strip()[:80] or f"take_{i + 1:02d}"
        out.append({"id": tid, "name": name, "start": round(start, 6), "end": round(end, 6),
                    "export": bool(t.get("export", True))})
    out.sort(key=lambda t: t["start"])
    return out
