"""File → camera assignment (§6.3).

1. A file inside ``source/<sub>/`` belongs to camera ``<sub>``.
2. Other files are partitioned by ``(make, model, codec, display WxH, nominal fps)``.
3. Inside a partition identical devices are split with two cues: a camera can't
   record two clips at once (creation-time intervals overlapping > 1 s), and
   the static background of a view (per-pixel median of 5 tiny gray frames)
   correlates strongly between clips of the same camera.
"""
from __future__ import annotations

from pathlib import PurePosixPath
from typing import Callable

import numpy as np

OVERLAP_LIMIT = 1.0
JOIN_CORR = 0.6
MERGE_CORR = 0.85

FeatureLoader = Callable[[dict], "np.ndarray | None"]


def subfolder(rel: str) -> str | None:
    parts = PurePosixPath(rel.replace("\\", "/")).parts
    return parts[0] if len(parts) > 1 else None


def signature(f: dict) -> tuple:
    p = f["probe"]
    v = p["video"]
    return (p.get("make"), p.get("model"), v.get("codec"), v.get("display_width"),
            v.get("display_height"), v.get("nominal_fps"))


def interval(f: dict, stamp: str = "start") -> tuple[float, float] | None:
    ct = f["probe"].get("creation_time")
    if ct is None:
        return None
    d = float(f["probe"].get("duration") or 0)
    return (ct, ct + d) if stamp == "start" else (ct - d, ct)


def overlaps(a: dict, b: dict) -> bool:
    """Recorded at the same time whether the device stamps creation_time at the
    start or at the end of a recording (consecutive clips of an end-stamping
    camera overlap if read as start stamps)."""
    for stamp in ("start", "end"):
        ia, ib = interval(a, stamp), interval(b, stamp)
        if ia is None or ib is None or min(ia[1], ib[1]) - max(ia[0], ib[0]) <= OVERLAP_LIMIT:
            return False
    return True


def pearson(x: np.ndarray | None, y: np.ndarray | None) -> float:
    if x is None or y is None or x.size != y.size:
        return 0.0
    x = x.ravel().astype(np.float64)
    y = y.ravel().astype(np.float64)
    x = x - x.mean()
    y = y - y.mean()
    d = np.sqrt((x * x).sum() * (y * y).sum())
    return float((x * y).sum() / d) if d > 1e-12 else 0.0


def _sort_key(f: dict):
    ct = f["probe"].get("creation_time")
    return (ct is None, ct or 0.0, f["rel"])


class Cluster:
    def __init__(self, f: dict, feat):
        self.members = [f]
        self.feats = [feat] if feat is not None else []

    def add(self, f: dict, feat) -> None:
        self.members.append(f)
        if feat is not None:
            self.feats.append(feat)

    def mean(self) -> np.ndarray | None:
        return np.mean(np.stack(self.feats), axis=0) if self.feats else None

    def conflicts(self, f: dict) -> bool:
        return any(overlaps(f, m) for m in self.members)

    def earliest(self):
        return min(_sort_key(m) for m in self.members)


def split_partition(files: list[dict], feats: dict[str, np.ndarray | None]) -> list[Cluster]:
    clusters: list[Cluster] = []
    for f in sorted(files, key=_sort_key):
        feat = feats.get(f["id"])
        best, best_r = None, -np.inf
        for c in clusters:
            if c.conflicts(f):
                continue
            r = pearson(feat, c.mean())
            if r > best_r:
                best, best_r = c, r
        if best is not None and best_r >= JOIN_CORR:
            best.add(f, feat)
        else:
            clusters.append(Cluster(f, feat))
    # merge clusters whose mean backgrounds agree and which never overlap
    while True:
        best_pair, best_r = None, MERGE_CORR
        for i in range(len(clusters)):
            for j in range(i + 1, len(clusters)):
                a, b = clusters[i], clusters[j]
                if any(a.conflicts(m) for m in b.members):
                    continue
                r = pearson(a.mean(), b.mean())
                if r >= best_r:
                    best_pair, best_r = (i, j), r
        if best_pair is None:
            break
        i, j = best_pair
        for m in clusters[j].members:
            clusters[i].add(m, feats.get(m["id"]))
        del clusters[j]
    return clusters


def _next_name(used: set[str]) -> str:
    k = 1
    while f"cam{k}" in used:
        k += 1
    return f"cam{k}"


def _next_id(used: set[str]) -> str:
    k = 1
    while f"c{k}" in used:
        k += 1
    return f"c{k}"


def group_all(files: list[dict], load_feature: FeatureLoader) -> tuple[list[dict], dict[str, str]]:
    """Full grouping. Returns ``(cameras, {file_id: camera_id})``."""
    by_sub: dict[str, list[dict]] = {}
    rest: list[dict] = []
    for f in files:
        sub = subfolder(f["rel"])
        if sub:
            by_sub.setdefault(sub, []).append(f)
        else:
            rest.append(f)

    groups: list[tuple[str | None, list[dict]]] = [(sub, fs) for sub, fs in by_sub.items()]
    if rest:
        feats = {f["id"]: load_feature(f) for f in rest}
        partitions: dict[tuple, list[dict]] = {}
        for f in rest:
            partitions.setdefault(signature(f), []).append(f)
        for part in partitions.values():
            groups += [(None, c.members) for c in split_partition(part, feats)]

    groups.sort(key=lambda g: min(_sort_key(m) for m in g[1]))
    cameras, assignment = [], {}
    used_names = {sub for sub, _ in groups if sub}
    used_ids: set[str] = set()
    for sub, members in groups:
        cid = _next_id(used_ids)
        used_ids.add(cid)
        name = sub or _next_name(used_names)
        used_names.add(name)
        cameras.append({"id": cid, "name": name, "rotation": 0, "include": True,
                        "grouped_by": "subfolder" if sub else "metadata+background"})
        for m in members:
            assignment[m["id"]] = cid
    return cameras, assignment


def assign_new(data: dict, files: list[dict], load_feature: FeatureLoader) -> list[str]:
    """Incremental: put unassigned files on existing cameras (best match) or new
    ones, leaving the user's assignments alone. Returns the ids assigned."""
    cameras = data["cameras"]
    cam_ids = {c["id"] for c in cameras}
    assigned = [f for f in files if f.get("camera") in cam_ids]
    todo = sorted((f for f in files if f.get("camera") not in cam_ids), key=_sort_key)
    feats = {f["id"]: load_feature(f) for f in assigned + todo}
    done = []
    for f in todo:
        target = None
        sub = subfolder(f["rel"])
        if sub:
            target = next((c for c in cameras if c["name"] == sub), None)
        else:
            best_r = JOIN_CORR
            for c in cameras:
                members = [m for m in assigned if m["camera"] == c["id"]]
                if not members or any(signature(m) != signature(f) for m in members):
                    continue
                if any(overlaps(f, m) for m in members):
                    continue
                mfeats = [feats[m["id"]] for m in members if feats.get(m["id"]) is not None]
                r = pearson(feats.get(f["id"]), np.mean(np.stack(mfeats), axis=0) if mfeats else None)
                if r >= best_r:
                    target, best_r = c, r
        if target is None:
            target = {"id": _next_id({c["id"] for c in cameras}),
                      "name": sub or _next_name({c["name"] for c in cameras}),
                      "rotation": 0, "include": True,
                      "grouped_by": "subfolder" if sub else "metadata+background"}
            cameras.append(target)
        f["camera"] = target["id"]
        assigned.append(f)
        done.append(f["id"])
    return done


def regroup(data: dict, files: list[dict], load_feature: FeatureLoader) -> None:
    cameras, assignment = group_all(files, load_feature)
    data["cameras"] = cameras
    for f in data["files"]:
        f["camera"] = assignment.get(f["id"])
