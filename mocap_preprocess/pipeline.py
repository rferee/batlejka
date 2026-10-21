"""Session pipeline steps that are not their own module: scan (§6.1),
the fps check, process (§6.2) and proxies (§6.4)."""
from __future__ import annotations

import json
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from scipy import ndimage

from . import config
from . import ffmpeg_tools as ff
from . import grouping, syncing
from .jobs import Job
from .session_store import SessionStore, file_id, now, video_files


# ---------------------------------------------------------------------------
# scan

def list_source_files(source: Path) -> list[dict]:
    out = []
    if not source.is_dir():
        return out
    for root, dirs, files in os.walk(source):
        dirs[:] = sorted(d for d in dirs if not d.startswith("."))
        for fn in sorted(files):
            if fn.startswith(".") or os.path.splitext(fn)[1].lower() not in config.VIDEO_EXTS:
                continue
            full = Path(root) / fn
            try:
                st = full.stat()
            except OSError:
                continue
            rel = full.relative_to(source).as_posix()
            out.append({"rel": rel, "path": full, "size": st.st_size, "mtime": round(st.st_mtime, 3)})
    return out


def new_file_record(rel: str, size: int, mtime: float) -> dict:
    return {
        "id": file_id(rel),
        "rel": rel,
        "size": size,
        "mtime": mtime,
        "probe": None,
        "probe_error": None,
        "assets": {"thumb": False, "features": False, "audio": False, "wave": False, "proxy": False},
        "camera": None,
        "offset": None,
        "sync": {"method": "none"},
    }


def delete_cache(paths, fid: str) -> None:
    for p in paths.cache_files(fid):
        try:
            p.unlink(missing_ok=True)
        except OSError:
            pass


def scan(store: SessionStore, name: str, job: Job | None = None) -> dict:
    paths = store.paths(name)
    entries = list_source_files(paths.source)
    data = store.load(name)
    known = {f["rel"]: f for f in data["files"]}

    def changed(e):
        f = known.get(e["rel"])
        return (f is None or f.get("probe") is None or f.get("size") != e["size"]
                or abs((f.get("mtime") or 0) - e["mtime"]) > 1e-3)

    todo = [e for e in entries if changed(e)]
    results: dict[str, tuple[dict | None, str | None]] = {}
    if job:
        job.log(f"{len(entries)} video files in source/, {len(todo)} to probe")
    if todo:
        with ThreadPoolExecutor(max_workers=4) as ex:
            futs = {ex.submit(ff.probe, e["path"]): e for e in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                e = futs[fut]
                try:
                    results[e["rel"]] = (fut.result(), None)
                except Exception as exc:  # noqa: BLE001
                    results[e["rel"]] = (None, str(exc))
                    if job:
                        job.log(f"{e['rel']}: probe failed: {exc}", "error")
                if job:
                    job.set_progress(i / len(todo), f"probed {i}/{len(todo)}")
                    if job.cancelled:
                        for f in futs:
                            f.cancel()
                        job.check_cancel()

    added = removed = reprobed = 0
    with store.edit(name) as data:
        old = {f["rel"]: f for f in data["files"]}
        files = []
        for e in entries:
            f = old.get(e["rel"])
            if e["rel"] in results:
                probe, err = results[e["rel"]]
                if f is None:
                    f = new_file_record(e["rel"], e["size"], e["mtime"])
                    added += 1
                else:
                    if f.get("size") != e["size"] or abs((f.get("mtime") or 0) - e["mtime"]) > 1e-3:
                        delete_cache(paths, f["id"])
                        f["assets"] = {k: False for k in f["assets"]}
                    reprobed += 1
                f.update(size=e["size"], mtime=e["mtime"], probe=probe, probe_error=err)
            if f is not None:
                files.append(f)
        seen = {e["rel"] for e in entries}
        for rel, f in old.items():
            if rel not in seen:
                removed += 1
                delete_cache(paths, f["id"])
        data["files"] = files
        used = {f.get("camera") for f in files}
        if removed:
            data["cameras"] = [c for c in data["cameras"] if c["id"] in used]
        prefill_target_fps(data)
        data["state"]["scanned_at"] = now()
    summary = {"files": len(entries), "added": added, "removed": removed, "reprobed": reprobed}
    if job:
        job.log(f"scan done: {summary}")
    return summary


def prefill_target_fps(data: dict) -> None:
    """Most common nominal fps weighted by total duration (unless user-set)."""
    s = data["settings"]
    if s.get("target_fps_user_set"):
        return
    weight: dict[str, float] = defaultdict(float)
    for f in video_files(data):
        nom = f["probe"]["video"].get("nominal_fps")
        if nom:
            weight[nom] += max(f["probe"].get("duration") or 0, 0.001)
    if weight:
        s["target_fps"] = max(weight.items(), key=lambda kv: kv[1])[0]


# ---------------------------------------------------------------------------
# fps check

def _drift_note(nominal: str, target: str) -> str:
    fn, ft = ff.fps_float(nominal), ff.fps_float(target)
    if not fn or not ft:
        return ""
    ratio = fn / ft
    k = round(ratio) if ratio >= 1 else 1 / round(1 / ratio)
    mult = "" if k == 1 else (f" ({round(k)}×)" if k > 1 else f" (1/{round(1 / k)})")
    residual = abs(ratio / k - 1)
    drift = residual * 60 * ft
    if residual > 0.02:
        return f"{ff.fps_label(nominal)} vs {ff.fps_label(target)} → frames duplicated/dropped unevenly"
    if drift < 0.05:
        return f"{ff.fps_label(nominal)} vs {ff.fps_label(target)}{mult} → frames dropped/duplicated evenly"
    return (f"{ff.fps_label(nominal)} vs {ff.fps_label(target)}{mult} → ~{drift:.1f} frames of drift "
            f"per minute at {ff.fps_label(target)} fps")


def fps_check(data: dict) -> dict:
    target = data["settings"]["target_fps"]
    items = []
    files = data["files"]
    no_video = [f for f in files if f.get("probe") is not None and not f["probe"].get("video")]
    failed = [f for f in files if f.get("probe") is None]
    vids = video_files(data)

    if failed:
        items.append({"level": "error", "title": f"{len(failed)} file(s) could not be probed",
                      "detail": "ffprobe failed — see the scan log", "files": [f["id"] for f in failed]})
    if no_video:
        items.append({"level": "error", "title": f"{len(no_video)} file(s) without a video stream",
                      "detail": "ignored", "files": [f["id"] for f in no_video]})

    by_rate: dict[str, list] = defaultdict(list)
    for f in vids:
        nom = f["probe"]["video"].get("nominal_fps")
        if nom != target:
            by_rate[nom or "?"].append(f)
    for nom, fs in sorted(by_rate.items()):
        items.append({"level": "warn",
                      "title": f"{len(fs)} file(s) at {ff.fps_label(nom)} fps instead of {ff.fps_label(target)}",
                      "detail": _drift_note(nom, target) + " — conformed on export",
                      "files": [f["id"] for f in fs]})

    vfr = [f for f in vids if f["probe"]["video"].get("vfr_suspect")]
    if vfr:
        items.append({"level": "warn", "title": f"{len(vfr)} file(s) look variable-frame-rate",
                      "detail": "will be conformed to constant fps on export",
                      "files": [f["id"] for f in vfr]})

    no_audio = [f for f in vids if not f["probe"].get("audio")]
    if no_audio:
        items.append({"level": "warn", "title": f"{len(no_audio)} file(s) without audio",
                      "detail": "can't be audio-synced; will be placed by metadata, check manually",
                      "files": [f["id"] for f in no_audio]})

    n_ok = sum(1 for f in vids if f["probe"]["video"].get("nominal_fps") == target)
    level = "ok"
    if any(i["level"] == "error" for i in items):
        level = "error"
    elif items:
        level = "warn"
    if not vids and level == "ok":
        level = "empty"
    if not vids:
        summary = "no video files yet"
    elif level == "ok":
        summary = f"all {len(vids)} files at {ff.fps_label(target)} fps"
    else:
        summary = f"{n_ok} ok · {len(vids) - n_ok} off-target"
    return {"level": level, "summary": summary, "target": target, "target_label": ff.fps_label(target),
            "n_ok": n_ok, "n_video": len(vids), "items": items}


# ---------------------------------------------------------------------------
# process

FEATURE_HIGHPASS_SIGMA = 3.0

def _save_npy(path: Path, arr: np.ndarray) -> None:
    tmp = path.with_name(path.stem + ".tmp.npy")
    with open(tmp, "wb") as fh:
        np.save(fh, arr)
    ff.replace_with_retry(tmp, path)


def write_waveform(samples: np.ndarray, sr: int, a0: float, path: Path) -> None:
    """Peak |amplitude| per 20 ms bin, normalized to 0–255."""
    b = max(1, int(round(sr * config.WAVE_BIN_SECONDS)))
    n = len(samples) // b
    if n:
        peaks = np.abs(samples[: n * b].reshape(n, b)).max(axis=1)
    else:
        peaks = np.zeros(0, dtype=np.float32)
    top = float(peaks.max()) if len(peaks) else 0.0
    q = np.round(peaks / top * 255).astype(np.uint8) if top > 0 else np.zeros(len(peaks), np.uint8)
    payload = {"bin": b / sr, "start": a0, "duration": len(samples) / sr, "peaks": q.tolist()}
    tmp = path.with_name(path.stem + ".tmp.json")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, separators=(",", ":"))
    ff.replace_with_retry(tmp, path)


def process_file(paths, f: dict, sr: int, job: Job | None) -> dict:
    fid = f["id"]
    src = paths.source_file(f["rel"])
    probe = f["probe"]
    dur = float(probe.get("duration") or 0)
    assets = {}

    thumb = paths.thumb(fid)
    if not thumb.exists():
        ff.grab_jpeg(src, 0.3 * dur, thumb)
    assets["thumb"] = thumb.exists()
    if job:
        job.check_cancel()

    feat = paths.feature(fid)
    if not feat.exists():
        frames = [ff.grab_gray(src, p * dur) for p in (0.1, 0.3, 0.5, 0.7, 0.9)]
        frames = [x for x in frames if x is not None]
        if frames:
            # per-pixel median = static background (the moving person drops out);
            # a mild high-pass removes the bright-ceiling/dark-floor layout that
            # every view of the same room shares, leaving furniture and edges
            med = np.median(np.stack(frames), axis=0)
            hp = med - ndimage.gaussian_filter(med, FEATURE_HIGHPASS_SIGMA, mode="nearest")
            z = (hp - hp.mean()) / (hp.std() + 1e-6)
            _save_npy(feat, z.astype(np.float32))
    assets["features"] = feat.exists()
    if job:
        job.check_cancel()

    if probe.get("audio"):
        a = paths.audio_npy(fid, sr)
        samples = None
        if not a.exists():
            samples = ff.extract_audio(src, sr)
            _save_npy(a, samples)
        assets["audio"] = True
        wave = paths.wave_json(fid)
        if not wave.exists():
            if samples is None:
                samples = np.load(a)
            a0 = float((probe["audio"] or {}).get("start") or 0) - float(probe.get("format_start") or 0)
            write_waveform(samples, sr, a0, wave)
        assets["wave"] = True
    else:
        assets["audio"] = assets["wave"] = False
    return assets


def load_feature_fn(paths):
    def load(f: dict):
        p = paths.feature(f["id"])
        try:
            return np.load(p) if p.exists() else None
        except (OSError, ValueError):
            return None
    return load


def process(store: SessionStore, name: str, job: Job, start_proxies=None) -> dict:
    paths = store.paths(name).ensure()
    data = store.load(name)
    sr = int(data["settings"]["sync_sample_rate"])
    files = video_files(data)
    if not files:
        raise RuntimeError("no video files to process — drop footage into source/ and re-scan")
    job.log(f"processing {len(files)} files with {config.PROCESS_WORKERS} workers")
    results: dict[str, dict] = {}
    done = 0
    with ThreadPoolExecutor(max_workers=config.PROCESS_WORKERS) as ex:
        futs = {ex.submit(process_file, paths, f, sr, job): f for f in files}
        try:
            for fut in as_completed(futs):
                f = futs[fut]
                try:
                    results[f["id"]] = fut.result()
                except ff.Cancelled:
                    raise
                except Exception as e:  # noqa: BLE001
                    job.log(f"{f['rel']}: {e}", "error")
                done += 1
                job.set_progress(0.9 * done / len(files), f"{done}/{len(files)} files")
        except ff.Cancelled:
            for fu in futs:
                fu.cancel()
            raise
    job.check_cancel()

    with store.edit(name) as data:
        for f in data["files"]:
            if f["id"] in results:
                f["assets"].update(results[f["id"]])
        vids = video_files(data)
        load = load_feature_fn(paths)
        cam_ids = {c["id"] for c in data["cameras"]}
        if any(f.get("camera") in cam_ids for f in vids):
            new = grouping.assign_new(data, vids, load)
            if new:
                job.log(f"assigned {len(new)} new file(s) to cameras")
        else:
            grouping.regroup(data, vids, load)
            job.log(f"grouped into {len(data['cameras'])} camera(s): "
                    + ", ".join(f"{c['name']} ({sum(1 for f in vids if f['camera'] == c['id'])})" for c in data["cameras"]))
        syncing.place_provisional(data)
        data["state"]["processed_at"] = now()
    job.set_progress(1.0, "done")
    if start_proxies:
        start_proxies()
    return {"files": len(files)}


# ---------------------------------------------------------------------------
# proxies

def proxy_ok(paths, f: dict, long_side: int) -> bool:
    return (f["assets"].get("proxy") and f["assets"].get("proxy_size") == long_side
            and paths.proxy(f["id"]).exists())


def run_proxies(store: SessionStore, name: str, job: Job) -> dict:
    """Build scrub proxies, shortest files first (so takes are usable early),
    re-reading the session after each file so new files get picked up."""
    paths = store.paths(name).ensure()
    built, failed = 0, set()
    while True:
        job.check_cancel()
        data = store.load(name)
        st = data["settings"]
        L = int(st["proxy_long_side"])
        pending = [f for f in video_files(data) if not proxy_ok(paths, f, L) and f["id"] not in failed]
        if not pending:
            break
        f = min(pending, key=lambda x: float(x["probe"].get("duration") or 0))
        total = built + len(failed) + len(pending)
        dur = float(f["probe"].get("duration") or 0)
        tmp, dst = paths.proxy_tmp(f["id"]), paths.proxy(f["id"])
        src = paths.source_file(f["rel"])
        base = built + len(failed)
        label = f"{base + 1}/{total} {f['rel']}"
        job.set_progress(base / total, label)

        def prog(x, base=base, total=total, label=label):
            job.set_progress((base + x) / total, label)

        ok = False
        for hw in ([True, False] if st.get("hwaccel") else [False]):
            try:
                rc, err = ff.run_progress(ff.proxy_args(src, tmp, L, hw), dur, prog, job.cancel_event)
            except ff.Cancelled:
                tmp.unlink(missing_ok=True)
                raise
            if rc == 0 and tmp.exists():
                ok = True
                break
            job.log(f"{f['rel']}: proxy failed{' with -hwaccel cuda, retrying on CPU' if hw else ''}: {err[-300:]}",
                    "warn" if hw else "error")
        if not ok:
            tmp.unlink(missing_ok=True)
            failed.add(f["id"])
            continue
        try:
            ff.replace_with_retry(tmp, dst)
        except OSError as e:  # the old proxy is still being streamed somewhere
            tmp.unlink(missing_ok=True)
            failed.add(f["id"])
            job.log(f"{f['rel']}: can't replace the old proxy ({e}); re-run Process to retry", "error")
            continue
        with store.edit(name) as d:
            for x in d["files"]:
                if x["id"] == f["id"]:
                    x["assets"]["proxy"] = True
                    x["assets"]["proxy_size"] = L
        built += 1
        job.log(f"proxy ready: {f['rel']}")
    job.set_progress(1.0, f"{built} built" + (f", {len(failed)} failed" if failed else ""))
    return {"built": built, "failed": len(failed)}


def standard_rates() -> list[dict]:
    return [{"value": r, "label": ff.fps_label(r)} for r in config.STANDARD_FPS]
