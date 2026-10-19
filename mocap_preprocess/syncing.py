"""Audio cross-correlation placement of every clip on one session timeline (§6.5).

Same core math as skelly_synchronize's ``cross_correlate`` (z-scored audio →
FFT correlation → ``correlation_lags`` → argmax), but instead of trimming one
clip per camera to a common overlap, every file gets an offset on a single
session timeline: ``session_time = file.offset + file_time``.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass

import numpy as np
from scipy import signal

from . import ffmpeg_tools as ff
from .jobs import Job
from .session_store import SessionStore, now

PRE_EMPHASIS = 0.97
EXCLUDE_S = 0.1          # ratio ignores peaks within ±0.1 s of the best one
MIN_OVERLAP_S = 5.0
MAX_PASSES = 4
RATIO_CAP = 99.0
BLOCK = 1 << 20          # lags normalized per block to bound memory
TRUSTED = ("reference", "audio", "manual")


@dataclass
class Match:
    lag: int             # x's sample 0 lines up with r's sample `lag`
    score: float         # normalized correlation at the best lag
    ratio: float         # score / best score outside ±EXCLUDE_S


# ---------------------------------------------------------------------------
# signal math

def prepare(x: np.ndarray) -> np.ndarray:
    """Pre-emphasis (favors transients such as claps), then z-score."""
    x = np.asarray(x, dtype=np.float32)
    if len(x) < 2:
        return x.copy()
    y = np.empty_like(x)
    y[0] = x[0]
    y[1:] = x[1:] - PRE_EMPHASIS * x[:-1]
    sd = float(y.std())
    if sd < 1e-9:
        return np.zeros_like(y)
    return ((y - y.mean()) / sd).astype(np.float32)


def energy_cumsum(x: np.ndarray) -> np.ndarray:
    cs = np.empty(len(x) + 1, dtype=np.float64)
    cs[0] = 0.0
    np.cumsum(np.square(x, dtype=np.float64), out=cs[1:])
    return cs


def xcorr_full(r: np.ndarray, x: np.ndarray) -> np.ndarray:
    """Same result as ``scipy.signal.correlate(r, x, mode="full", method="fft")``;
    overlap-add when one signal is much longer (a long continuous camera
    against a short take) keeps memory proportional to the long one."""
    a, b = len(r), len(x)
    if min(a, b) > 4096 and max(a, b) > 4 * min(a, b):
        return signal.oaconvolve(r, x[::-1], mode="full")
    return signal.fftconvolve(r, x[::-1], mode="full")


def ncc_match(r: np.ndarray, x: np.ndarray, sr: int, cs_r: np.ndarray | None = None,
              min_overlap_s: float = MIN_OVERLAP_S, exclude_s: float = EXCLUDE_S) -> Match | None:
    """Best alignment of prepared signal ``x`` inside prepared signal ``r``.

    The raw correlation is normalized per lag by the energies of the
    overlapping parts, so partial overlaps compare fairly; lags whose overlap
    is shorter than ``min(5 s, 50 % of the shorter clip)`` are ignored.
    """
    Lr, Lx = len(r), len(x)
    if Lr < 2 or Lx < 2:
        return None
    c = xcorr_full(r, x)  # index i <-> lag i - (Lx - 1); see signal.correlation_lags
    if cs_r is None:
        cs_r = energy_cumsum(r)
    cs_x = energy_cumsum(x)
    min_ov = min(min_overlap_s * sr, 0.5 * min(Lr, Lx))
    excl = int(round(exclude_s * sr))
    peaks: list[tuple[float, int]] = []
    n = len(c)
    for i0 in range(0, n, BLOCK):
        i1 = min(n, i0 + BLOCK)
        k = np.arange(i0, i1, dtype=np.int64) - (Lx - 1)
        r0, r1 = np.clip(k, 0, Lr), np.clip(k + Lx, 0, Lr)
        x0, x1 = np.clip(-k, 0, Lx), np.clip(Lr - k, 0, Lx)
        den = np.sqrt((cs_r[r1] - cs_r[r0]) * (cs_x[x1] - cs_x[x0]))
        ok = ((r1 - r0) >= min_ov) & (den > 1e-9)
        if not ok.any():
            continue
        v = np.full(i1 - i0, -np.inf)
        v[ok] = c[i0:i1][ok] / den[ok]
        j = int(np.argmax(v))
        peaks.append((float(v[j]), i0 + j))
        # also keep this block's best peak outside the exclusion window
        lo, hi = max(0, j - excl), min(len(v), j + excl + 1)
        side = []
        if lo > 0:
            jj = int(np.argmax(v[:lo]))
            side.append((float(v[jj]), i0 + jj))
        if hi < len(v):
            jj = hi + int(np.argmax(v[hi:]))
            side.append((float(v[jj]), i0 + jj))
        if side:
            peaks.append(max(side))
    peaks = [p for p in peaks if np.isfinite(p[0])]
    if not peaks:
        return None
    best_v, best_i = max(peaks)
    others = [v for v, i in peaks if abs(i - best_i) > excl]
    second = max(others) if others else 0.0
    ratio = RATIO_CAP if second <= 1e-9 else min(RATIO_CAP, best_v / second)
    return Match(lag=int(best_i - (Lx - 1)), score=best_v, ratio=float(ratio))


# ---------------------------------------------------------------------------
# helpers on session data

def video_files(data: dict) -> list[dict]:
    return [f for f in data["files"] if f.get("probe") and f["probe"].get("video")]


def duration(f: dict) -> float:
    return float(f["probe"].get("duration") or 0.0)


def audio_start(f: dict) -> float:
    """a0 = audio.start − format_start (where audio sample 0 sits in file time)."""
    a = f["probe"].get("audio") or {}
    return float(a.get("start") or 0.0) - float(f["probe"].get("format_start") or 0.0)


def load_audio(paths, f: dict, sr: int) -> np.ndarray:
    p = paths.audio_npy(f["id"], sr)
    if not p.exists():
        samples = ff.extract_audio(paths.source_file(f["rel"]), sr)
        tmp = p.with_name(p.stem + ".tmp.npy")
        paths.ensure()
        with open(tmp, "wb") as fh:
            np.save(fh, samples)
        ff.replace_with_retry(tmp, p)
    return np.load(p)


def metadata_offset(f: dict, by_id: dict[str, dict], placed: dict[str, float]) -> tuple[float, str] | None:
    """Place ``f`` by creation time through a trusted-placed file's clock:
    the same camera's if possible, else the median clock of all placed files."""
    ct = f["probe"].get("creation_time")
    if ct is None:
        return None
    same = [(by_id[g], off) for g, off in placed.items()
            if by_id[g].get("camera") == f.get("camera") and f.get("camera")
            and by_id[g]["probe"].get("creation_time") is not None]
    if same:
        g, off = min(same, key=lambda go: abs(go[0]["probe"]["creation_time"] - ct))
        return off + (ct - g["probe"]["creation_time"]), "camera clock"
    clocks = [off - by_id[g]["probe"]["creation_time"] for g, off in placed.items()
              if by_id[g]["probe"].get("creation_time") is not None]
    if clocks:
        return ct + statistics.median(clocks), "median clock"
    return None


def metadata_only(files: list[dict]) -> dict[str, tuple[float, dict]]:
    """Provisional timeline: offset = creation_time − min(creation_time)."""
    out = {}
    cts = [f["probe"]["creation_time"] for f in files if f["probe"].get("creation_time") is not None]
    base = min(cts) if cts else 0.0
    for f in files:
        ct = f["probe"].get("creation_time")
        if ct is not None:
            out[f["id"]] = (ct - base, {"method": "metadata", "via": "creation time"})
    return out


def place_after_end(files: list[dict], offsets: dict[str, float]) -> dict[str, float]:
    """Files we know nothing about go after the end, one after another."""
    end = max((offsets[f["id"]] + duration(f) for f in files if f["id"] in offsets), default=0.0)
    out = {}
    for f in sorted((f for f in files if f["id"] not in offsets), key=lambda f: f["rel"]):
        start = end + 1.0
        out[f["id"]] = start
        end = start + duration(f)
    return out


def place_provisional(data: dict) -> None:
    """Process step 6: timeline usable before syncing; never touches trusted offsets."""
    files = video_files(data)
    by_id = {f["id"]: f for f in files}
    placed = {f["id"]: f["offset"] for f in files
              if f.get("offset") is not None and f["sync"].get("method") in TRUSTED}
    if not placed:
        meta = metadata_only(files)
        offsets = {fid: off for fid, (off, _) in meta.items()}
        for f in files:
            if f["id"] in meta:
                f["offset"], f["sync"] = meta[f["id"]][0], meta[f["id"]][1]
        after = place_after_end(files, offsets)
        for f in files:
            if f["id"] in after:
                f["offset"], f["sync"] = after[f["id"]], {"method": "none"}
        if files:
            shift = -min(f["offset"] for f in files)
            for f in files:
                f["offset"] += shift
        return
    offsets = {f["id"]: f["offset"] for f in files if f.get("offset") is not None}
    for f in files:
        if f.get("offset") is None:
            m = metadata_offset(f, by_id, placed)
            if m is not None:
                f["offset"], f["sync"] = m[0], {"method": "metadata", "via": m[1]}
                offsets[f["id"]] = m[0]
    after = place_after_end(files, offsets)
    for f in files:
        if f["id"] in after:
            f["offset"], f["sync"] = after[f["id"]], {"method": "none"}


# ---------------------------------------------------------------------------
# the sync job

def sync_session(store: SessionStore, name: str, job: Job, resync_manual: bool = False) -> dict:
    paths = store.paths(name)
    data = store.load(name)
    st = data["settings"]
    sr = int(st["sync_sample_rate"])
    min_score, min_ratio = float(st["sync_min_score"]), float(st["sync_min_ratio"])
    files = video_files(data)
    if not files:
        raise RuntimeError("no video files — scan and process first")
    by_id = {f["id"]: f for f in files}
    kept = {f["id"]: f["offset"] for f in files
            if f["sync"].get("method") == "manual" and f.get("offset") is not None and not resync_manual}
    if kept:
        job.log(f"keeping {len(kept)} manual offset(s)")

    # 1. audio
    sig: dict[str, np.ndarray] = {}
    audio_files = [f for f in files if f["probe"].get("audio")]
    for i, f in enumerate(audio_files):
        job.check_cancel()
        job.set_progress(0.1 * i / max(1, len(audio_files)), f"loading audio {i + 1}/{len(audio_files)}")
        try:
            raw = load_audio(paths, f, sr)
        except Exception as e:  # noqa: BLE001
            job.log(f"{f['rel']}: can't read audio: {e}", "warn")
            continue
        if len(raw) < sr:
            job.log(f"{f['rel']}: audio shorter than 1 s, skipped", "warn")
            continue
        sig[f["id"]] = prepare(raw)
    with_audio = [f for f in audio_files if f["id"] in sig]
    for f in files:
        if not f["probe"].get("audio"):
            job.log(f"{f['rel']}: no audio track — will be placed by metadata", "warn")

    offsets: dict[str, float] = {}
    info: dict[str, dict] = {}
    best_seen: dict[str, tuple[str, Match]] = {}
    ref = max(with_audio, key=duration) if with_audio else None
    if ref is not None:
        # Anchor the new placement on the reference's previous position so that
        # takes and kept manual offsets stay attached to the same footage.
        anchor = ref["offset"] if ref.get("offset") is not None else 0.0
        offsets[ref["id"]] = kept.get(ref["id"], anchor)
        info[ref["id"]] = ref["sync"] if ref["id"] in kept else {"method": "reference"}
        job.log(f"reference: {ref['rel']} ({duration(ref):.1f} s)")
        for fid, off in kept.items():
            offsets[fid] = off
            info[fid] = by_id[fid]["sync"]
        layer = [ref] + [f for f in with_audio if f["id"] in kept and f["id"] != ref["id"]]
        pending = [f for f in with_audio if f["id"] not in offsets]
        cs_cache: dict[str, np.ndarray] = {}
        total = max(1, len(pending))
        done = 0
        for pass_no in range(1, MAX_PASSES + 1):
            if not pending or not layer:
                break
            job.log(f"pass {pass_no}: {len(pending)} file(s) against {len(layer)} placed file(s)")
            found: dict[str, tuple[dict, Match]] = {}
            for f in pending:
                best = None
                for g in layer:
                    job.check_cancel()
                    if g["id"] not in cs_cache:
                        cs_cache[g["id"]] = energy_cumsum(sig[g["id"]])
                    m = ncc_match(sig[g["id"]], sig[f["id"]], sr, cs_r=cs_cache[g["id"]])
                    if m is None:
                        continue
                    seen = best_seen.get(f["id"])
                    if seen is None or m.score > seen[1].score:
                        best_seen[f["id"]] = (g["id"], m)
                    if m.score >= min_score and m.ratio >= min_ratio and (best is None or m.score > best[1].score):
                        best = (g, m)
                if best is not None:
                    found[f["id"]] = best
                done += 1
                job.set_progress(0.1 + 0.85 * min(1.0, done / total), f"correlating {done}/{total}")
            layer = []
            for f in pending:
                if f["id"] not in found:
                    continue
                g, m = found[f["id"]]
                off = offsets[g["id"]] + audio_start(g) + m.lag / sr - audio_start(f)
                offsets[f["id"]] = off
                info[f["id"]] = {"method": "audio", "ref": g["id"], "score": round(m.score, 4),
                                 "ratio": round(m.ratio, 3), "pass": pass_no}
                job.log(f"  {f['rel']} → {off - offsets[ref['id']]:+.3f} s from reference start "
                        f"(vs {g['rel']}, score {m.score:.3f}, ratio {m.ratio:.2f})")
                layer.append(f)
            pending = [f for f in pending if f["id"] not in found]
            total = done + len(pending) * max(1, len(layer))
        for f in pending:
            seen = best_seen.get(f["id"])
            if seen:
                job.log(f"  {f['rel']}: no confident match (best score {seen[1].score:.3f}, "
                        f"ratio {seen[1].ratio:.2f} vs {by_id[seen[0]]['rel']})", "warn")
            else:
                job.log(f"  {f['rel']}: no usable overlap with any placed file", "warn")
    else:
        job.log("no usable audio in this session — placing everything by metadata", "warn")

    # 2. fallback: creation time through a placed file's clock
    placed = dict(offsets)
    if not placed:
        for fid, (off, inf) in metadata_only(files).items():
            offsets[fid], info[fid] = off, inf
    else:
        for f in files:
            if f["id"] in offsets:
                continue
            m = metadata_offset(f, by_id, placed)
            if m is not None:
                offsets[f["id"]] = m[0]
                inf = {"method": "metadata", "via": m[1]}
                if f["id"] in best_seen:
                    inf.update(score=round(best_seen[f["id"]][1].score, 4), ratio=round(best_seen[f["id"]][1].ratio, 3))
                info[f["id"]] = inf
                rel = m[0] - offsets[ref["id"]] if ref is not None else m[0]
                job.log(f"  {f['rel']} → {rel:+.3f} s by {m[1]} (check manually)", "warn")
    for fid, off in place_after_end(files, offsets).items():
        offsets[fid], info[fid] = off, {"method": "none"}
        job.log(f"  {by_id[fid]['rel']}: no audio match and no creation time — put after the end", "error")

    # 3. earliest file starts at 0
    shift = -min(offsets.values())
    job.set_progress(0.97, "saving")
    with store.edit(name) as d:
        for f in d["files"]:
            if f["id"] not in offsets:
                continue
            if not resync_manual and f["id"] not in kept and f["sync"].get("method") == "manual":
                continue  # nudged by the user while we were busy
            f["offset"] = round(offsets[f["id"]] + shift, 6)
            f["sync"] = info[f["id"]]
        if abs(shift) > 1e-9:
            for t in d["takes"]:
                t["start"] = round(t["start"] + shift, 6)
                t["end"] = round(t["end"] + shift, 6)
        d["state"]["synced_at"] = now()
    counts: dict[str, int] = {}
    for inf in info.values():
        counts[inf.get("method", "none")] = counts.get(inf.get("method", "none"), 0) + 1
    job.log("done: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items())))
    return {"counts": counts, "shift": shift}


def resync_file(store: SessionStore, name: str, fid: str, ref_fid: str) -> dict:
    """User-chosen re-sync of one file against a placed reference file."""
    paths = store.paths(name)
    data = store.load(name)
    by_id = {f["id"]: f for f in video_files(data)}
    f, g = by_id.get(fid), by_id.get(ref_fid)
    if f is None or g is None:
        raise LookupError("unknown file")
    if fid == ref_fid:
        raise ValueError("pick a different reference file")
    if not f["probe"].get("audio") or not g["probe"].get("audio"):
        raise ValueError("both files need an audio track")
    if g.get("offset") is None:
        raise ValueError("the reference file isn't placed on the timeline yet")
    st = data["settings"]
    sr = int(st["sync_sample_rate"])
    m = ncc_match(prepare(load_audio(paths, g, sr)), prepare(load_audio(paths, f, sr)), sr)
    if m is None:
        raise ValueError("the files don't overlap enough to compare")
    with store.edit(name) as d:
        cur = {x["id"]: x for x in d["files"]}
        ref_off = cur[ref_fid]["offset"]
        off = ref_off + audio_start(g) + m.lag / sr - audio_start(f)
        cur[fid]["offset"] = round(off, 6)
        cur[fid]["sync"] = {"method": "manual", "via": "re-sync", "ref": ref_fid,
                            "score": round(m.score, 4), "ratio": round(m.ratio, 3)}
    accepted = m.score >= float(st["sync_min_score"]) and m.ratio >= float(st["sync_min_ratio"])
    return {"offset": off, "score": m.score, "ratio": m.ratio, "accepted": accepted}
