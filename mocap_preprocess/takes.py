"""Take handling: auto-detection from clip clusters (§6.7) and validation."""
from __future__ import annotations

import math
import re

from .session_store import placed_video_files

CONTINUOUS_FRACTION = 0.6   # files this long relative to the session are ignored for clustering
MERGE_GAP = 2.0             # clips closer than this belong to the same take
MIN_TAKE = 1.0


def detect(data: dict, take_seq: int = 0) -> list[dict]:
    """Takes numbered on from ``take_seq`` (see take_seq())."""
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
    ids = _fresh_ids(take_seq)
    return [{"id": next(ids), "name": f"take_{i:02d}", "start": t["start"], "end": t["end"], "export": True}
            for i, t in enumerate(takes, 1)]


_TID = re.compile(r"^t\d{1,5}$")
MAX_TAKE_NO = 99999


def take_seq(data: dict, exported_ids=()) -> int:
    """The highest take number this session has issued. Export folders, mocap results and
    previews are keyed by take id, so a new take numbered above it can't inherit a deleted
    take's. Counts the stored ``take_seq``, the takes, the mocap records and ``exported_ids``
    (the take ids in output/)."""
    ids = [t.get("id") for t in data.get("takes") or [] if isinstance(t, dict)]
    ids += list((data.get("mocap") or {}).get("takes") or {}) + list(exported_ids)
    seq = data.get("take_seq")
    seq = seq if isinstance(seq, int) and not isinstance(seq, bool) and seq > 0 else 0
    return max([seq] + [int(i[1:]) for i in ids if isinstance(i, str) and _TID.match(i)])


def _fresh_ids(take_seq: int):
    """t<take_seq + 1>, t<take_seq + 2>, …; once the numbers run out, the low ones."""
    for k in range(take_seq + 1, MAX_TAKE_NO + 1):
        yield f"t{k}"
    for k in range(1, min(take_seq, MAX_TAKE_NO) + 1):
        yield f"t{k}"


def validate(takes_in, take_seq: int = 0) -> list[dict]:
    """New and invalid ids get fresh numbers above ``take_seq``; ids the client sends are
    kept even when they're at or below it, since undo brings deleted takes back as they were."""
    if not isinstance(takes_in, list) or not all(isinstance(t, dict) for t in takes_in):
        raise ValueError("takes must be a list of objects")
    out, ids = [], set()
    sent = {str(x.get("id")) for x in takes_in}
    for i, t in enumerate(takes_in):
        tid = str(t.get("id") or "")
        if not _TID.match(tid) or tid in ids:
            tid = next(k for k in _fresh_ids(take_seq) if k not in ids and k not in sent)
        ids.add(tid)
        try:
            start, end = float(t["start"]), float(t["end"])
        except (KeyError, TypeError, ValueError, OverflowError):
            raise ValueError(f"take {t.get('name')!r}: start and end must be numbers")
        if not (math.isfinite(start) and math.isfinite(end)):   # JSON can't carry them back out
            raise ValueError(f"take {t.get('name')!r}: start and end must be finite")
        if not end > start:
            raise ValueError(f"take {t.get('name')!r}: end must be after start")
        name = str(t.get("name") or "").strip()[:80] or f"take_{i + 1:02d}"
        out.append({"id": tid, "name": name, "start": round(start, 6), "end": round(end, 6),
                    "export": bool(t.get("export", True))})
    out.sort(key=lambda t: t["start"])
    return out
