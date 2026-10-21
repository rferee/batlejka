"""Two-stage placement of every clip on one session timeline (§6.5).

Stage 1, camera clocks. Every camera has its own clock and stamps
``creation_time`` either when a recording starts or when it ends. The rough
guess for a file is ``creation_time`` (or ``creation_time − duration``) in its
camera's clock. Clips of different cameras whose rough guesses come within
LINK_WINDOW_S of each other are matched by a full audio search on decimated
audio; every match implies an offset between the two cameras' clocks. Per
camera pair the consensus wins (largest cluster within CLUSTER_S, not the
mean), both stamp hypotheses are tried per camera, and cameras are chained to
a reference camera through the strongest links. Cameras that link to nothing
are attached later by a full search against the placed cameras, checked
against their own other clips.

Stage 2, fine alignment. Each file's prediction (guess + its camera's clock
offset) is refined by audio only within ±W of the prediction, so looping music
or rhythmic noise can't pull a clip to a bar that merely sounds the same. Clips
that overlap in time form a group and vote together: their correlation curves
are summed, so one ambiguous clip can't drift on its own.

The core math is skelly_synchronize's ``cross_correlate`` (z-scored audio →
FFT correlation → argmax), but every file gets an offset on one session
timeline: ``session_time = file.offset + file_time``.
"""
from __future__ import annotations

import itertools
import math
import os
import statistics
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from fractions import Fraction

import numpy as np
from scipy import signal

from . import ffmpeg_tools as ff
from .jobs import Job
from .session_store import video_files
from .session_store import SessionStore, now

PRE_EMPHASIS = 0.97
EXCLUDE_S = 0.1          # ratio ignores peaks within ±0.1 s of the best one
MIN_OVERLAP_S = 5.0
MAX_PASSES = 4
RATIO_CAP = 99.0
BLOCK = 1 << 20          # lags normalized per block to bound memory
AUDIO_METHODS = ("reference", "audio", "search")
TRUSTED = AUDIO_METHODS + ("manual",)

LINK_SR = 2000           # clock links only need ~1 s, so their full searches run on decimated audio
LINK_WINDOW_S = 600.0    # rough guesses this far apart may still be the same moment
LINK_PAIRS = 16          # full searches per camera pair, longest rough overlap first
SEARCH_PARTNERS = 24     # full searches per file when a camera has to be attached by search
SEARCH_BUDGET = 400
CLUSTER_S = 1.0          # consensus = largest cluster of implied offsets within ~1 s
SPREAD_FACTOR = 3.0      # W = max(sync_window, 3 × consensus spread)
CLOCK_PRIOR_S = 300.0    # choosing start/end stamps: 5 min more clock difference costs one agreeing match
VOTE_SCORE_FACTOR = 0.5  # a link vote needs score ≥ ½ sync_min_score; the cluster does the rest
WINDOW_SCORE_FACTOR = 0.25 # within ±W the ratio does most of the work; distant mics score low
REFINE_S = 0.02          # full-search hits are refined at the full rate within ±20 ms
REFINE_FULL_S = 0.0015   # window results (LINK_SR) settle at the full rate within ±1.5 ms
OVERLAP_TOL_S = 0.25     # clips of one camera may touch, never overlap
MODES = ("start", "end")
WORKERS = max(1, min(8, (os.cpu_count() or 2) // 2))


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


def decimate(x: np.ndarray, sr_from: int, sr_to: int) -> np.ndarray:
    if sr_to >= sr_from:
        return np.asarray(x, dtype=np.float32)
    q = Fraction(sr_to, sr_from)
    return signal.resample_poly(x, q.numerator, q.denominator).astype(np.float32)


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


def _gather(cs: np.ndarray, start: int, stop: int, lo: int, hi: int) -> np.ndarray:
    """``cs[clip(arange(start, stop), lo, hi)]`` built from slices (no index arrays)."""
    n1 = max(0, min(stop, lo) - start)
    m0, m1 = max(start, lo), min(stop, hi + 1)
    n3 = max(0, stop - max(start, hi + 1))
    parts = [np.full(n1, cs[lo]), cs[m0:m1] if m1 > m0 else cs[:0], np.full(n3, cs[hi])]
    return np.concatenate(parts)


def _ratio(peak: float, second: float) -> float:
    if peak <= 0:
        return 0.0
    return RATIO_CAP if second <= 1e-9 else float(min(RATIO_CAP, peak / second))


def ncc_match(r: np.ndarray, x: np.ndarray, sr: int, cs_r: np.ndarray | None = None,
              min_overlap_s: float = MIN_OVERLAP_S, exclude_s: float = EXCLUDE_S) -> Match | None:
    """Best alignment of prepared signal ``x`` anywhere inside prepared signal ``r``.

    The raw correlation is normalized per lag by the energies of the
    overlapping parts, so partial overlaps compare fairly; lags whose overlap
    is shorter than ``min(5 s, 50 % of the shorter clip)`` are ignored.
    """
    Lr, Lx = len(r), len(x)
    if Lr < 2 or Lx < 2:
        return None
    min_ov = int(math.ceil(min(min_overlap_s * sr, 0.5 * min(Lr, Lx))))
    k_lo, k_hi = min_ov - Lx, Lr - min_ov       # overlap = min(Lx, Lr, k + Lx, Lr - k) ≥ min_ov
    if k_hi < k_lo:
        return None
    c = xcorr_full(r, x)  # index i <-> lag i - (Lx - 1); see signal.correlation_lags
    if cs_r is None:
        cs_r = energy_cumsum(r)
    cs_x = energy_cumsum(x)
    excl = int(round(exclude_s * sr))
    peaks: list[tuple[float, int]] = []
    for ka in range(k_lo, k_hi + 1, BLOCK):
        kb = min(k_hi + 1, ka + BLOCK)
        er = _gather(cs_r, ka + Lx, kb + Lx, 0, Lr) - _gather(cs_r, ka, kb, 0, Lr)
        ex = (_gather(cs_x, Lr - kb + 1, Lr - ka + 1, 0, Lx) - _gather(cs_x, 1 - kb, 1 - ka, 0, Lx))[::-1]
        den = np.sqrt(np.maximum(er * ex, 0.0))
        v = c[ka + Lx - 1: kb + Lx - 1] / np.where(den > 1e-9, den, np.inf)
        j = int(np.argmax(v))
        peaks.append((float(v[j]), ka + j))
        # also keep this block's best peak outside the exclusion window
        lo, hi = max(0, j - excl), min(len(v), j + excl + 1)
        side = []
        if lo > 0:
            jj = int(np.argmax(v[:lo]))
            side.append((float(v[jj]), ka + jj))
        if hi < len(v):
            jj = hi + int(np.argmax(v[hi:]))
            side.append((float(v[jj]), ka + jj))
        if side:
            peaks.append(max(side))
    peaks = [p for p in peaks if np.isfinite(p[0])]
    if not peaks:
        return None
    best_v, best_k = max(peaks)
    others = [v for v, k in peaks if abs(k - best_k) > excl]
    return Match(lag=int(best_k), score=best_v, ratio=_ratio(best_v, max(others) if others else 0.0))


def dot_curve(r: np.ndarray, x: np.ndarray, lag_lo: int, lag_hi: int) -> np.ndarray:
    """Like ``window_curve`` but by direct dot products: cheaper for a handful of lags."""
    Lr, Lx = len(r), len(x)
    out = np.full(max(0, lag_hi - lag_lo + 1), np.nan)
    for i, k in enumerate(range(lag_lo, lag_hi + 1)):
        r0, r1 = max(k, 0), min(k + Lx, Lr)
        if r1 - r0 < 2:
            continue
        xs, rs = x[r0 - k: r1 - k], r[r0:r1]
        den = math.sqrt(float(np.dot(xs, xs)) * float(np.dot(rs, rs)))
        if den > 1e-9:
            out[i] = float(np.dot(xs, rs)) / den
    return out


def window_curve(r: np.ndarray, x: np.ndarray, lag_lo: int, lag_hi: int, sr: int,
                 min_overlap_s: float = MIN_OVERLAP_S) -> np.ndarray:
    """Normalized correlation of prepared ``x`` against prepared ``r`` for lags
    ``lag_lo..lag_hi`` only (NaN where the overlap is too short). Only the
    stretch of the longer signal that those lags can touch is transformed, so a
    ±1.5 s window costs about one FFT of the shorter clip."""
    n = lag_hi - lag_lo + 1
    out = np.full(max(0, n), np.nan)
    Lr, Lx = len(r), len(x)
    if n <= 0 or Lr < 2 or Lx < 2:
        return out
    if Lx > Lr:   # cut the longer one instead: lag' = -lag
        return window_curve(x, r, -lag_hi, -lag_lo, sr, min_overlap_s)[::-1].copy()
    a, b = max(lag_lo, 0), min(lag_hi + Lx, Lr)
    if b - a < 2:
        return out
    seg = r[a:b]
    c = xcorr_full(seg, x)  # index i <-> lag a + i - (Lx - 1)
    k = np.arange(lag_lo, lag_hi + 1, dtype=np.int64)
    idx = k - a + Lx - 1
    ov = np.minimum(np.minimum(k + Lx, Lr - k), min(Lx, Lr))
    cs_s, cs_x = energy_cumsum(seg), energy_cumsum(x)
    ns = len(seg)
    er = cs_s[np.clip(np.minimum(k + Lx, Lr) - a, 0, ns)] - cs_s[np.clip(np.maximum(k, 0) - a, 0, ns)]
    ex = cs_x[np.clip(Lr - k, 0, Lx)] - cs_x[np.clip(-k, 0, Lx)]
    den = np.sqrt(np.maximum(er * ex, 0.0))
    ok = (idx >= 0) & (idx < len(c)) & (ov >= min(min_overlap_s * sr, 0.5 * min(Lr, Lx))) & (den > 1e-9)
    out[ok] = c[idx[ok]] / den[ok]
    return out


# ---------------------------------------------------------------------------
# consensus

@dataclass
class Cluster:
    center: float        # median of the supporting values
    support: int         # values within ±CLUSTER_S of the center
    spread: float        # their standard deviation
    members: list[int]
    runner_up: int       # size of the densest cluster among the other values


def _densest(xs: list[float], tol: float) -> tuple[int, int, int]:
    best, j = (0, 0, -1), 0
    for i in range(len(xs)):
        while xs[i] - xs[j] > tol:
            j += 1
        if i - j + 1 > best[0]:
            best = (i - j + 1, j, i)
    return best


def consensus(values: list[float], weights: list[float] | None = None, tol: float = CLUSTER_S) -> Cluster | None:
    """Largest group of values within ``tol`` of each other (ties: larger total
    weight), widened to everything within ±tol of its median."""
    if not values:
        return None
    w = weights or [1.0] * len(values)
    order = sorted(range(len(values)), key=values.__getitem__)
    xs = [values[i] for i in order]
    cw = list(itertools.accumulate((w[i] for i in order), initial=0.0))
    best, j = None, 0
    for i in range(len(xs)):
        while xs[i] - xs[j] > tol:
            j += 1
        key = (i - j + 1, cw[i + 1] - cw[j])
        if best is None or key > best[0]:
            best = (key, j, i)
    _, j, i = best
    mid = statistics.median(xs[j:i + 1])
    members = [k for k in range(len(values)) if abs(values[k] - mid) <= tol]
    sup = [values[k] for k in members]
    inside = set(members)
    rest = sorted(values[k] for k in range(len(values)) if k not in inside)
    return Cluster(center=statistics.median(sup), support=len(sup),
                   spread=statistics.pstdev(sup) if len(sup) > 1 else 0.0,
                   members=members, runner_up=_densest(rest, tol)[0] if rest else 0)


def _overlap(a: tuple[float, float], b: tuple[float, float]) -> float:
    return min(a[1], b[1]) - max(a[0], b[0])


def _overlap_groups(ids: list[str], iv: dict[str, tuple[float, float]]) -> list[list[str]]:
    groups: list[list[str]] = []
    end = -math.inf
    for f in sorted(ids, key=lambda f: iv[f][0]):
        if groups and iv[f][0] < end:
            groups[-1].append(f)
            end = max(end, iv[f][1])
        else:
            groups.append([f])
            end = iv[f][1]
    return groups


# ---------------------------------------------------------------------------
# helpers on session data

def duration(f: dict) -> float:
    return float(f["probe"].get("duration") or 0.0)


def creation_time(f: dict) -> float | None:
    ct = f["probe"].get("creation_time")
    return None if ct is None else float(ct)


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

@dataclass
class Vote:
    a: str               # file of camera A
    b: str               # file of camera B
    delta: float         # S(a) − S(b) according to the audio
    score: float
    ratio: float


@dataclass
class Clock:
    """session_time = guess(file, mode) + offset, for every file of the camera."""
    cam: str
    comp: int
    offset: float = 0.0
    mode: str = "start"
    mode_basis: str = "assumed"   # "measured": the clusters told start from end apart
    support: int = 0
    spread: float = 0.0
    window: float = 1.5
    via: str = "none"             # reference | link | search | manual offset | median clock
    parent: str | None = None
    attached: bool = False        # its clock is known in the session's frame
    votes: int = 0


class SessionSync:
    def __init__(self, store: SessionStore, name: str, job: Job, resync_manual: bool = False):
        self.store, self.name, self.job = store, name, job
        self.paths = store.paths(name)
        self.data = data = store.load(name)
        st = data["settings"]
        self.sr = int(st["sync_sample_rate"])
        self.min_score, self.min_ratio = float(st["sync_min_score"]), float(st["sync_min_ratio"])
        self.win_min = float(st.get("sync_window") or 1.5)
        self.vote_min = VOTE_SCORE_FACTOR * self.min_score
        self.win_score = WINDOW_SCORE_FACTOR * self.min_score
        self.files = video_files(data)
        if not self.files:
            raise RuntimeError("no video files — scan and process first")
        self.by_id = {f["id"]: f for f in self.files}
        self.resync_manual = resync_manual
        self.kept = {f["id"]: f["offset"] for f in self.files
                     if f["sync"].get("method") == "manual" and f.get("offset") is not None and not resync_manual}
        self.cam_of = {f["id"]: f.get("camera") for f in self.files}
        self.cam_name = {c["id"]: c["name"] for c in data["cameras"]}
        self.dur = {f["id"]: duration(f) for f in self.files}
        self.a0 = {f["id"]: audio_start(f) for f in self.files}
        self.ct = {f["id"]: creation_time(f) for f in self.files}
        self.sig: dict[str, np.ndarray] = {}
        self.sig_link: dict[str, np.ndarray] = {}
        self.clocks: dict[str, Clock] = {}
        self.by_pair: dict[tuple[str, str], list[Vote]] = {}
        self.residuals: dict[str, list[float]] = {}
        self.search_hits: dict[str, tuple[str, float, float, float]] = {}
        self.pos: dict[str, float] = {}       # fresh frame: the reference camera's clock
        self.info: dict[str, dict] = {}
        self.ref_cam: str | None = None
        self.modes: dict[str, str] = {}
        self.phase = "audio"
        self.active: set[str] = set()
        self.live_pairs: dict[tuple[str, str | None], dict] = {}
        self._published = 0.0

    # -- small helpers ------------------------------------------------------
    def rel(self, fid: str) -> str:
        return self.by_id[fid]["rel"]

    def cname(self, cam: str | None) -> str:
        return self.cam_name.get(cam, cam or "?")

    def guess(self, fid: str, mode: str) -> float:
        return self.ct[fid] - (self.dur[fid] if mode == "end" else 0.0)

    def clock(self, fid: str) -> Clock | None:
        return self.clocks.get(self.cam_of[fid]) if self.ct[fid] is not None else None

    def predict(self, fid: str) -> float | None:
        c = self.clock(fid)
        return None if c is None else self.guess(fid, c.mode) + c.offset

    def window(self, fid: str) -> float:
        c = self.clock(fid)
        return c.window if c is not None else self.win_min

    def log(self, msg: str, level: str = "info") -> None:
        self.job.log(msg, level)

    def parallel(self, fn, items: list, lo: float, hi: float, what: str, on_result=None) -> list:
        out = [None] * len(items)
        if not items:
            return out
        done = 0
        with ThreadPoolExecutor(WORKERS) as ex:
            futs = {ex.submit(fn, it): i for i, it in enumerate(items)}
            try:
                for fut in as_completed(futs):
                    i = futs[fut]
                    out[i] = fut.result()
                    if on_result is not None:
                        on_result(i, out[i])
                    done += 1
                    self.job.set_progress(lo + (hi - lo) * done / len(items), f"{what} {done}/{len(items)}")
                    self.job.check_cancel()
            except BaseException:
                for f in futs:
                    f.cancel()
                raise
        return out

    # -- live view (polled by the Sync screen) ---------------------------------
    def publish(self, phase: str | None = None, force: bool = False) -> None:
        if phase:
            self.phase = phase
            force = True
        t = time.monotonic()
        if force or t - self._published > 0.25:
            self._published = t
            self.job.live = self.snapshot()   # a fresh dict each time: readers never see it half-built

    def snapshot(self) -> dict:
        ref = self.clocks.get(self.ref_cam)
        cams = []
        for c in [c["id"] for c in self.data["cameras"]] + sorted({x for x in self.cam_of.values() if x} - set(self.cam_name)):
            clk = self.clocks.get(c)
            stamped = any(self.ct[f] is not None for f in self.by_id if self.cam_of[f] == c)
            row = {"id": c, "name": self.cname(c), "state": "pending" if stamped else "none"}
            if clk is not None:
                known = clk.attached or clk.via in ("median clock", "manual offset")
                row.update(mode=clk.mode, basis=clk.mode_basis, support=clk.support, votes=clk.votes,
                           spread=round(clk.spread, 3), window=round(clk.window, 3), via=clk.via,
                           state=clk.via if known else "pending",
                           ahead=round(-(clk.offset - (ref.offset if ref else 0.0)), 3) if known else None)
            cams.append(row)
        clips = []
        for f in self.files:
            fid = f["id"]
            clk = self.clock(fid)
            pred = self.predict(fid) if clk is not None and (clk.attached or clk.via in ("median clock", "manual offset")) else None
            inf = self.info.get(fid, {})
            clips.append({"id": fid, "cam": self.cam_of[fid], "name": f["rel"].split("/")[-1], "dur": round(self.dur[fid], 3),
                          "raw": self.ct[fid], "pred": pred, "pos": self.pos.get(fid), "w": round(self.window(fid), 3),
                          "method": inf.get("method"), "kept": fid in self.kept, "active": fid in self.active,
                          "audio": fid in self.sig})
        pairs = []
        for (a, b), p in self.live_pairs.items():
            pairs.append({"a": a, "b": b, "name": f"{self.cname(a)} ↔ {self.cname(b) if b else 'placed cameras'}",
                          **{k: p.get(k) for k in ("votes", "center", "support", "ok")}})
        return {"phase": self.phase, "ref_cam": self.ref_cam, "cams": cams, "clips": clips, "pairs": pairs,
                "frame": getattr(self, "live_frame", None)}

    def live_pair(self, key: tuple[str, str | None], values: list[float], ok: bool | None = None) -> None:
        cl = consensus(values)
        self.live_pairs[key] = {"votes": [round(v, 3) for v in values], "center": round(cl.center, 3) if cl else None,
                                "support": cl.support if cl else 0, "ok": ok}

    def full_match(self, a: str, b: str) -> tuple[float, float, float] | None:
        """Full search on decimated audio → (S(a) − S(b), score, ratio)."""
        A, B = self.sig_link[a], self.sig_link[b]
        if len(A) >= len(B):    # x = b inside r = a: S(b) − S(a) = a0(a) − a0(b) + lag/sr
            m = ncc_match(A, B, LINK_SR)
            return None if m is None else (-(self.a0[a] - self.a0[b] + m.lag / LINK_SR), m.score, m.ratio)
        m = ncc_match(B, A, LINK_SR)
        return None if m is None else (self.a0[b] - self.a0[a] + m.lag / LINK_SR, m.score, m.ratio)

    # -- 0. audio -----------------------------------------------------------
    def load_signals(self) -> None:
        audio = [f for f in self.files if f["probe"].get("audio")]
        for i, f in enumerate(audio):
            self.job.check_cancel()
            self.job.set_progress(0.08 * i / max(1, len(audio)), f"loading audio {i + 1}/{len(audio)}")
            try:
                raw = load_audio(self.paths, f, self.sr)
            except Exception as e:  # noqa: BLE001
                self.log(f"{f['rel']}: can't read audio: {e}", "warn")
                continue
            if len(raw) < self.sr:
                self.log(f"{f['rel']}: audio shorter than 1 s, skipped", "warn")
                continue
            self.sig[f["id"]] = prepare(raw)
            self.sig_link[f["id"]] = prepare(decimate(raw, self.sr, LINK_SR))
        for f in self.files:
            if not f["probe"].get("audio"):
                self.log(f"{f['rel']}: no audio track — placed by its camera clock", "warn")
        no_ct = [f for f in self.files if self.ct[f["id"]] is None]
        if no_ct:
            self.log(f"{len(no_ct)} file(s) without creation time — full search only", "warn")

    # -- 1. camera clocks ---------------------------------------------------
    def link_stage(self) -> None:
        clock_cams = sorted({self.cam_of[f] for f in self.by_id if self.cam_of[f] and self.ct[f] is not None})
        voters: dict[str, list[str]] = {}
        for fid in self.sig:
            if self.cam_of[fid] and self.ct[fid] is not None:
                voters.setdefault(self.cam_of[fid], []).append(fid)
        plan = []
        for A, B in itertools.combinations(sorted(voters), 2):
            cand = []
            for a in voters[A]:
                for b in voters[B]:
                    ov = max(_overlap((self.guess(a, ma), self.guess(a, ma) + self.dur[a]),
                                      (self.guess(b, mb), self.guess(b, mb) + self.dur[b]))
                             for ma in MODES for mb in MODES)
                    if ov > -LINK_WINDOW_S:
                        cand.append((ov, a, b))
            cand.sort(key=lambda t: -t[0])
            pairs = [(a, b) for _, a, b in cand[:LINK_PAIRS]]
            if pairs:
                plan.append((sum(len(self.sig_link[a]) + len(self.sig_link[b]) for a, b in pairs), A, B, pairs))
        plan.sort()
        if plan:
            self.log(f"camera clocks: full searches at {LINK_SR} Hz between clips of different cameras "
                     f"whose timestamps are ≤ {LINK_WINDOW_S / 60:.0f} min apart")
        # cheap camera pairs first, all at once; an expensive pair (a long continuous
        # camera) is skipped once its two cameras are already firmly connected
        cheap = 3 * statistics.median(p[0] for p in plan) if plan else 0
        waves = [[p for p in plan if p[0] <= cheap]] + [[p] for p in plan if p[0] > cheap]
        total = sum(p[0] for p in plan) or 1
        done, votes = 0, []
        uf = {c: c for c in voters}

        def find(c):
            while uf[c] != c:
                uf[c] = uf[uf[c]]
                c = uf[c]
            return c
        by_cams: dict[tuple[str, str], list[Vote]] = {}

        def best_combo(A, B):   # clock difference K_A − K_B under the stamp modes that cluster best
            vs = by_cams.get((A, B), [])
            tries = [[self.vote_x(v, {A: ma, B: mb}) for v in vs] for ma in MODES for mb in MODES]
            return max(((consensus(xs), xs) for xs in tries if xs),
                       key=lambda t: t[0].support - abs(t[0].center) / CLOCK_PRIOR_S, default=(None, []))

        def on_vote(pairs):
            def cb(i, m):
                if m is None or m[1] < self.vote_min:
                    return
                a, b = pairs[i]
                by_cams.setdefault((self.cam_of[a], self.cam_of[b]), []).append(Vote(a, b, *m))
                self.live_pair((self.cam_of[a], self.cam_of[b]), best_combo(self.cam_of[a], self.cam_of[b])[1])
                self.publish()
            return cb
        for wave in waves:
            if len(wave) == 1 and wave[0][0] > cheap and find(wave[0][1]) == find(wave[0][2]):
                self.log(f"  {self.cname(wave[0][1])} ↔ {self.cname(wave[0][2])}: already linked through "
                         f"other cameras — long searches skipped")
                done += wave[0][0]
                continue
            pairs = [pr for p in wave for pr in p[3]]
            cost = sum(p[0] for p in wave)
            self.active = {f for pr in pairs for f in pr}
            self.publish(force=True)
            matches = self.parallel(lambda p: self.full_match(*p), pairs, 0.08 + 0.27 * done / total,
                                    0.08 + 0.27 * (done + cost) / total, "linking cameras", on_vote(pairs))
            done += cost
            votes += [Vote(a, b, *m) for (a, b), m in zip(pairs, matches) if m is not None and m[1] >= self.vote_min]
            for _, A, B, _ in wave:
                best = best_combo(A, B)[0]
                if best is not None and best.support >= 3 and best.support >= 2 * best.runner_up:
                    uf[find(A)] = find(B)
        self.active = set()
        self.solve_clocks(clock_cams, votes)

    def vote_x(self, v: Vote, modes: dict[str, str]) -> float:
        """Implied K_A − K_B of a vote (a ∈ A, b ∈ B)."""
        return v.delta - self.guess(v.a, modes[self.cam_of[v.a]]) + self.guess(v.b, modes[self.cam_of[v.b]])

    def solve_clocks(self, clock_cams: list[str], votes: list[Vote]) -> None:
        for v in votes:
            A, B = self.cam_of[v.a], self.cam_of[v.b]
            if A > B:
                v, (A, B) = Vote(v.b, v.a, -v.delta, v.score, v.ratio), (B, A)
            self.by_pair.setdefault((A, B), []).append(v)
        table: dict[tuple[str, str], dict[tuple[str, str], Cluster]] = {}
        for (A, B), vs in self.by_pair.items():
            table[(A, B)] = {(ma, mb): consensus([self.vote_x(v, {A: ma, B: mb}) for v in vs], [v.score for v in vs])
                             for ma in MODES for mb in MODES}
        modes, basis = self.choose_modes(sorted({c for p in self.by_pair for c in p}), table)
        for c in clock_cams:
            modes.setdefault(c, "start")
        self.modes = modes
        links: dict[tuple[str, str], Cluster] = {}
        for (A, B), vs in sorted(self.by_pair.items()):
            c = table[(A, B)][(modes[A], modes[B])]
            single = len(vs) == 1 and vs[0].score >= self.min_score and vs[0].ratio >= self.min_ratio
            ok = (c.support >= 2 and c.support >= 2 * c.runner_up) or single
            self.log(f"  {self.cname(A)} ↔ {self.cname(B)}: {c.support}/{len(vs)} matches agree"
                     f"{f' (next cluster {c.runner_up})' if c.runner_up else ''}, clocks differ by {c.center:+.2f} s, "
                     f"spread {c.spread:.2f} s" + ("" if ok else " — not used"), "info" if ok else "warn")
            if ok:
                links[(A, B)] = c
            self.live_pair((A, B), [self.vote_x(v, modes) for v in vs], ok)
        parent = {c: c for c in clock_cams}

        def find(c):
            while parent[c] != c:
                parent[c] = parent[parent[c]]
                c = parent[c]
            return c
        for A, B in links:
            parent[find(A)] = find(B)
        comps: dict[str, list[str]] = {}
        for c in clock_cams:
            comps.setdefault(find(c), []).append(c)
        for i, cams in enumerate(sorted(comps.values(), key=lambda cs: (-len(cs), cs))):
            strength = {c: sum(l.support for p, l in links.items() if c in p) for c in cams}
            ref = max(cams, key=lambda c: (strength[c], self.longest_audio(c), c))
            K, par = {ref: 0.0}, {ref: None}
            while True:   # Prim: strongest link from the tree to a new camera
                best = None
                for (A, B), c in links.items():
                    if (A in K) == (B in K):
                        continue
                    if best is None or (c.support, -c.spread) > best[0]:
                        best = ((c.support, -c.spread), A, B, c)
                if best is None:
                    break
                _, A, B, c = best
                if A in K:
                    K[B], par[B] = K[A] - c.center, A
                else:
                    K[A], par[A] = K[B] + c.center, B
            for c in cams:
                self.clocks[c] = Clock(cam=c, comp=i, offset=K[c], mode=modes[c], mode_basis=basis.get(c, "assumed"),
                                       via="reference" if c == ref else "link", parent=par[c])
            for c in K:   # per-camera consensus over every link, not just the tree edge
                vals, ws = self.implied(c, K)
                near = [(x, w) for x, w in zip(vals, ws) if abs(x - K[c]) <= 2 * CLUSTER_S]
                if not near:
                    continue
                cl = consensus([x for x, _ in near], [w for _, w in near])
                if c != ref:
                    K[c] = self.clocks[c].offset = cl.center
                self.clocks[c].support, self.clocks[c].spread, self.clocks[c].votes = cl.support, cl.spread, len(vals)
        for clk in self.clocks.values():
            clk.window = max(self.win_min, SPREAD_FACTOR * clk.spread)
        for (A, B), vs in self.by_pair.items():
            if A in self.clocks and B in self.clocks and self.clocks[A].comp == self.clocks[B].comp:
                expect = self.clocks[A].offset - self.clocks[B].offset
                for v in vs:
                    r = self.vote_x(v, modes) - expect
                    if abs(r) <= CLUSTER_S:
                        self.residuals.setdefault(v.a, []).append(r)
                        self.residuals.setdefault(v.b, []).append(-r)

    def implied(self, cam: str, K: dict[str, float]) -> tuple[list[float], list[float]]:
        vals, ws = [], []
        for (A, B), vs in self.by_pair.items():
            if A == cam and B in K:
                vals += [self.vote_x(v, self.modes) + K[B] for v in vs]
            elif B == cam and A in K:
                vals += [K[A] - self.vote_x(v, self.modes) for v in vs]
            else:
                continue
            ws += [v.score for v in vs]
        return vals, ws

    def longest_audio(self, cam: str) -> float:
        return max((self.dur[f] for f in self.sig if self.cam_of[f] == cam), default=0.0)

    @staticmethod
    def choose_modes(cams: list[str], table) -> tuple[dict[str, str], dict[str, str]]:
        """Start/end stamp per camera: the assignment whose links cluster best
        (more agreeing matches, tighter, and clocks closer together: when a
        camera's clips all last about as long, the two hypotheses only differ
        by a constant and the nearer clock is the likelier one)."""
        def quality(c: Cluster) -> float:
            return c.support - 0.5 * c.spread - abs(c.center) / CLOCK_PRIOR_S

        def total(m: dict[str, str]) -> float:
            return sum(quality(t[(m[A], m[B])]) for (A, B), t in table.items())

        def flip(m, c):
            alt = dict(m)
            alt[c] = "end" if m[c] == "start" else "start"
            return alt
        if not cams:
            return {}, {}
        if len(cams) <= 12:
            best = None
            for combo in itertools.product(MODES, repeat=len(cams)):
                m = dict(zip(cams, combo))
                s = total(m)
                if best is None or s > best[0] + 1e-12:
                    best = (s, m)
            modes = best[1]
        else:
            modes = {c: "start" for c in cams}
            improved = True
            while improved:
                improved = False
                for c in cams:
                    if total(flip(modes, c)) > total(modes) + 1e-9:
                        modes, improved = flip(modes, c), True
        basis = {}
        for c in cams:
            alt = flip(modes, c)
            same = all(t[(modes[A], modes[B])].support == t[(alt[A], alt[B])].support
                       and abs(t[(modes[A], modes[B])].spread - t[(alt[A], alt[B])].spread) < 0.1
                       for (A, B), t in table.items() if c in (A, B))
            basis[c] = "assumed" if same else "measured"
        return modes, basis

    def attach_stage(self) -> str | None:
        """Pick the main component (it holds the longest file with audio and a
        creation time, which becomes the reference) and attach the others by a
        full search against it, checked by consensus over each camera's clips."""
        cands = [f for f in self.sig if f not in self.kept and self.clock(f) is not None]
        if not cands:
            return None
        seed = max(cands, key=lambda f: (self.dur[f], f))
        comps: dict[int, list[str]] = {}
        for c, clk in self.clocks.items():
            comps.setdefault(clk.comp, []).append(c)
        main = self.clocks[self.cam_of[seed]].comp
        for c in comps[main]:
            self.clocks[c].attached = True
        self.ref_cam = next(c for c in comps[main] if self.clocks[c].via == "reference")
        if self.ref_cam != self.cam_of[seed]:   # name the reference after the seed's camera
            shift = self.clocks[self.cam_of[seed]].offset
            for c in comps[main]:
                self.clocks[c].offset -= shift
            self.clocks[self.ref_cam].via = "link"
            self.ref_cam = self.cam_of[seed]
            self.clocks[self.ref_cam].via, self.clocks[self.ref_cam].parent = "reference", None
        pending = sorted((k for k in comps if k != main), key=lambda k: (-len(comps[k]), k))
        progress = True
        while pending and progress:
            progress = False
            for k in list(pending):
                if self.attach(comps[k]):
                    pending.remove(k)
                    progress = True
        return seed

    def attach(self, cams: list[str]) -> bool:
        zs = [f for f in self.sig if self.cam_of[f] in cams and self.ct[f] is not None and f not in self.kept]
        partners = sorted((q for q in self.sig if q not in self.kept and self.clock(q) is not None
                           and self.clock(q).attached), key=lambda q: -self.dur[q])
        names = ", ".join(self.cname(c) for c in cams)
        if not zs or not partners:
            return False
        per = max(4, min(SEARCH_PARTNERS, SEARCH_BUDGET // len(zs)))
        tasks = [(z, q) for z in zs for q in partners[:per]]
        self.log(f"{names}: no clock link to the placed cameras — full search of {len(zs)} clip(s) "
                 f"({len(tasks)} pair(s)), then checked against each other")
        self.active = set(zs)
        self.publish(force=True)
        matches = self.parallel(lambda t: self.full_match(*t), tasks, 0.35, 0.42, f"attaching {names}")
        self.active = set()
        hits = [(z, q, m) for (z, q), m in zip(tasks, matches) if m is not None and m[1] >= self.vote_min]
        for z, q, m in hits:
            if m[1] >= self.min_score and m[2] >= self.min_ratio and (z not in self.search_hits
                                                                        or m[1] > self.search_hits[z][2]):
                self.search_hits[z] = (q, m[0], m[1], m[2])
        if not hits:
            self.log(f"  {names}: no usable match", "warn")
            return False
        single_cam = len(cams) == 1   # a lone camera's stamp mode is only known from this search
        tried = []
        for mode in (MODES if single_cam else (None,)):
            vals = []
            for z, q, m in hits:
                clk = self.clocks[self.cam_of[z]]
                vals.append(self.predict(q) + m[0] - (self.guess(z, mode or clk.mode) + clk.offset))
            cl = consensus(vals, [m[1] for _, _, m in hits])
            tried.append(((cl.support, -cl.spread, -abs(cl.center)), mode, cl, vals))
        _, mode, cl, vals = max(tried, key=lambda t: t[0])
        measured = len(tried) == 2 and tried[0][0][:2] != tried[1][0][:2]
        single = len(hits) == 1 and hits[0][2][1] >= self.min_score and hits[0][2][2] >= self.min_ratio
        ok = (cl.support >= 2 and cl.support >= 2 * cl.runner_up) or single
        self.log(f"  {names}: {cl.support}/{len(hits)} matches agree"
                 f"{f' (next cluster {cl.runner_up})' if cl.runner_up else ''}, clock {-cl.center:+.2f} s, "
                 f"spread {cl.spread:.2f} s" + ("" if ok else " — not attached"), "info" if ok else "warn")
        self.live_pair((cams[0], None), vals, ok)
        self.publish(force=True)
        if not ok:
            return False
        for c in cams:
            clk = self.clocks[c]
            clk.offset += cl.center
            clk.attached = True
            if mode:
                clk.mode, clk.mode_basis = mode, "measured" if measured else "assumed"
            if clk.via == "reference" or single_cam:
                clk.via, clk.support, clk.spread, clk.votes = "search", cl.support, cl.spread, len(hits)
                clk.window = max(self.win_min, SPREAD_FACTOR * clk.spread)
        for (z, q, _), x in zip(hits, vals):
            r = x - cl.center
            if abs(r) <= CLUSTER_S:
                self.residuals.setdefault(z, []).append(r)
                self.residuals.setdefault(q, []).append(-r)
        return True

    def log_clocks(self) -> None:
        ref = self.clocks.get(self.ref_cam)
        for c in sorted(self.clocks, key=lambda c: (not self.clocks[c].attached, c)):
            clk = self.clocks[c]
            ahead = -(clk.offset - (ref.offset if ref else 0.0))
            stamp = f"stamps {clk.mode}" + (" (assumed)" if clk.mode_basis != "measured" else "")
            if c == self.ref_cam:
                what = "reference clock"
            elif not clk.attached:
                what = "clock unknown"
            else:
                what = f"clock {ahead:+.2f} s vs {self.cname(self.ref_cam)}"
            self.log(f"camera {self.cname(c)}: {what}, {stamp}, support {clk.support}/{clk.votes}, "
                     f"spread {clk.spread:.2f} s, window ±{clk.window:.2f} s ({clk.via})",
                     "info" if clk.attached else "warn")

    # -- 2. fine alignment ---------------------------------------------------
    def fine_align(self, todo: list[str], anchors: dict[str, float], lo: float, hi: float,
                   clock_via: str = "camera clock") -> dict[str, tuple[float, dict]]:
        """Windows are searched at LINK_SR (distant mics agree far better below
        1 kHz), then every clip placed by audio settles at the full rate."""
        sr = LINK_SR
        todo = [f for f in todo if f in self.sig and self.predict(f) is not None]
        if not todo:
            return {}
        P = {f: self.predict(f) for f in todo}
        W = {f: self.window(f) for f in todo}
        iv = {f: (P[f] - W[f], P[f] + self.dur[f] + W[f]) for f in todo}
        b = {f: int(round((P[f] + self.a0[f]) * sr)) for f in todo}
        w = {f: max(1, int(round(W[f] * sr))) for f in todo}
        pq = {q: int(round((p + self.a0[q]) * sr)) for q, p in anchors.items() if q in self.sig}
        groups = _overlap_groups(todo, iv)
        tasks = []
        for G in groups:
            for f in G:
                for q in pq:
                    if _overlap(iv[f], (anchors[q], anchors[q] + self.dur[q])) > 1.0:
                        c0 = b[f] - pq[q]
                        tasks.append(("q", f, q, c0 - w[f], c0 + w[f]))
            for f, g in itertools.combinations(G, 2):
                if _overlap(iv[f], iv[g]) > 1.0:
                    c0, half = b[f] - b[g], w[f] + w[g]
                    tasks.append(("m", f, g, c0 - half, c0 + half))
        cq: dict[str, list] = {f: [] for f in todo}
        cm: dict[str, list] = {f: [] for f in todo}
        group_of = {f: gi for gi, G in enumerate(groups) for f in G}
        remaining = [0] * len(groups)
        for t in tasks:
            remaining[group_of[t[1]]] += 1
        out: dict[str, tuple[float, dict]] = {}

        def solve(gi):   # a group is solved as soon as its last curve is in, so clips land one group at a time
            res = self.solve_group(groups[gi], cq, cm, w, b, clock_via)
            out.update(res)
            for f, (p, inf) in res.items():
                self.pos[f], self.info[f] = p, inf
                self.active.discard(f)
            self.publish()

        def on_curve(i, arr):
            kind, f, g, _, _ = tasks[i]
            if np.isfinite(arr).any():
                if kind == "q":
                    cq[f].append((g, arr))
                else:
                    half = w[f] + w[g]
                    cm[f].append((g, arr, half))
                    cm[g].append((f, arr[::-1], half))
            remaining[group_of[f]] -= 1
            if remaining[group_of[f]] == 0:
                solve(group_of[f])
        self.active |= set(todo)
        self.publish(force=True)
        for gi in range(len(groups)):
            if remaining[gi] == 0:
                solve(gi)
        self.parallel(lambda t: window_curve(self.sig_link[t[2]], self.sig_link[t[1]], t[3], t[4], sr),
                      tasks, lo, lo + 0.75 * (hi - lo), "aligning within the windows", on_curve)
        self.refine_full(out, anchors, lo + 0.75 * (hi - lo), hi)
        return out

    def refine_full(self, placed: dict[str, tuple[float, dict]], anchors: dict[str, float],
                    lo: float, hi: float) -> None:
        """Settle audio-placed clips at the full sample rate within ±REFINE_FULL_S
        of the window result, against the clip each matched best; clips whose
        partner is already final go first."""
        sr = self.sr
        r = max(1, int(math.ceil(REFINE_FULL_S * sr)))
        pos = {f: p for f, (p, _) in placed.items()}
        final = {q: p for q, p in anchors.items() if q in self.sig}
        pending = {f for f, (_, inf) in placed.items()
                   if inf.get("method") == "audio" and inf.get("ref") in self.sig}

        def settle(f):
            q = placed[f][1]["ref"]
            bf = int(round((pos[f] + self.a0[f]) * sr))
            bq = int(round((final.get(q, pos.get(q)) + self.a0[q]) * sr))
            arr = dot_curve(self.sig[q], self.sig[f], bf - bq - r, bf - bq + r)
            if not np.isfinite(arr).any():
                return pos[f]
            return (bf + int(np.nanargmax(arr)) - r) / sr - self.a0[f]
        total = len(pending)
        while pending:
            ready = sorted(f for f in pending if placed[f][1]["ref"] in final) or sorted(pending)
            done = total - len(pending)
            for f, p in zip(ready, self.parallel(settle, ready, lo + (hi - lo) * done / total,
                                                 lo + (hi - lo) * (done + len(ready)) / total, "settling")):
                pos[f] = final[f] = p
                pending.discard(f)
        for f, (p, inf) in list(placed.items()):
            if pos[f] != p:
                inf["dev"] = round(inf.get("dev", 0.0) + pos[f] - p, 4)
                placed[f] = (pos[f], inf)

    def solve_group(self, G: list[str], cq, cm, w, b, clock_via: str) -> dict[str, tuple[float, dict]]:
        """Clips overlapping in time vote together. Relative structure from the
        strongest pairwise peaks, then one shift for the whole group from the
        summed curves against placed files, then coordinate ascent on every
        clip's summed curves; each clip's search stays within ±W of its prediction."""
        sr = LINK_SR
        excl = int(round(EXCLUDE_S * sr))
        e = {f: 0 for f in G}

        # 1. relative structure: maximum spanning forest over member↔member peaks
        edges = []
        for f in G:
            for g, arr, half in cm[f]:
                if f < g:
                    k = int(np.nanargmax(arr))
                    edges.append((float(arr[k]), f, g, k - half))   # e_f − e_g at the peak
        edges.sort(reverse=True)
        uf = {f: f for f in G}

        def find(x):
            while uf[x] != x:
                uf[x] = uf[uf[x]]
                x = uf[x]
            return x
        adj: dict[str, list] = {f: [] for f in G}
        for wgt, f, g, d in edges:
            if wgt <= 0:
                break
            if find(f) != find(g):
                uf[find(f)] = find(g)
                adj[f].append((g, d))
                adj[g].append((f, -d))
        rel: dict[str, int] = {}
        trees = []
        for root in sorted(G, key=lambda f: (-len(cq[f]), -self.dur[f], f)):
            if root in rel:
                continue
            rel[root], tree, stack = 0, [root], [root]
            while stack:
                u = stack.pop()
                for v, d in adj[u]:
                    if v not in rel:
                        rel[v] = rel[u] - d
                        tree.append(v)
                        stack.append(v)
            trees.append(tree)

        # 2. one shift per tree from the summed curves against placed files
        def shift_curve(tree, base, s_grid):
            tot = np.zeros(len(s_grid))
            n = 0
            for f in tree:
                for _, arr in cq[f]:
                    idx = base[f] + s_grid + w[f]
                    ok = (idx >= 0) & (idx < len(arr))
                    tot[ok] += np.nan_to_num(arr[idx[ok]])
                    n += 1
            return tot, n
        for tree in trees:
            lo = max(-w[f] - rel[f] for f in tree)
            hi = min(w[f] - rel[f] for f in tree)
            if lo > hi:
                lo, hi = -w[tree[0]], w[tree[0]]
            if any(cq[f] for f in tree):
                s_grid = np.arange(lo, hi + 1)
                tot, _ = shift_curve(tree, rel, s_grid)
                s = int(s_grid[int(np.argmax(tot))])
            else:
                s = min(max(int(round(-sum(rel[f] for f in tree) / len(tree))), lo), hi)
            for f in tree:
                e[f] = int(np.clip(rel[f] + s, -w[f], w[f]))

        # 3. coordinate ascent on each clip's summed curves
        def total(f):
            T = np.zeros(2 * w[f] + 1)
            n = np.zeros(2 * w[f] + 1)
            parts = []
            for q, arr in cq[f]:
                T += np.nan_to_num(arr)
                n += np.isfinite(arr)
                parts.append((q, arr))
            for g, arr, half in cm[f]:
                seg = arr[half - w[f] - e[g]: half + w[f] - e[g] + 1]
                T += np.nan_to_num(seg)
                n += np.isfinite(seg)
                parts.append((g, seg))
            return T, n, parts
        for _ in range(8):
            changed = False
            for f in G:
                if not cq[f] and not cm[f]:
                    continue
                T, _, _ = total(f)
                k = int(np.argmax(T))
                if k - w[f] != e[f] and T[k] > T[e[f] + w[f]] + 1e-9:
                    e[f], changed = k - w[f], True
            if not changed:
                break

        # 4. confidence: the group against placed files, then each clip
        out = {}
        for tree in trees:
            has_q = any(cq[f] for f in tree)
            group_ok, why = True, None
            if has_q:
                S = max(w[f] for f in tree)
                s_grid = np.arange(-S, S + 1)
                tot, n = shift_curve(tree, e, s_grid)
                far = np.abs(s_grid) > excl
                gscore, gratio = tot[S] / n, _ratio(tot[S], float(tot[far].max()) if far.any() else 0.0)
                group_ok = gscore >= self.win_score and gratio >= self.min_ratio
                if not group_ok:
                    names = sorted({self.rel(q) for f in tree for q, _ in cq[f]})
                    why = (f"weak audio peak against {', '.join(names[:2])}{'…' if len(names) > 2 else ''} "
                           f"(score {gscore:.3f}, ratio {gratio:.2f})")
                    m = int(round(sum(e[f] for f in tree) / len(tree)))
                    for f in tree:   # keep the predictions on average, and the group's own alignment
                        e[f] = int(np.clip(e[f] - m, -w[f], w[f]))
            judged = {}
            for f in tree:
                T, n, parts = total(f)
                k = e[f] + w[f]
                info: dict = {}
                member_ok = False
                if n[k] > 0:
                    far = np.abs(np.arange(len(T)) - k) > excl
                    sc = float(T[k] / n[k])
                    rt = _ratio(float(T[k]), float(T[far].max()) if far.any() else 0.0)
                    best = max(parts, key=lambda p: np.nan_to_num(p[1][k]))
                    info.update(score=round(sc, 4), ratio=round(rt, 3), ref=best[0])
                    if len(tree) > 1:
                        info["group"] = len(tree)
                    member_ok = sc >= self.win_score and rt >= self.min_ratio
                if group_ok and member_ok:
                    info.update(method="audio", via="window" if has_q else "group")
                else:
                    reason = why or ("weak audio peak in the window" if n[k] > 0 else "no overlapping audio")
                    info.update(method="metadata", via=clock_via, reason=reason)
                judged[f] = (member_ok, info)
            for f, (member_ok, info) in judged.items():
                if not member_ok:
                    e[f] = 0     # nothing to trust: keep the prediction
                info["dev"] = round(e[f] / sr, 4)
                out[f] = ((b[f] + e[f]) / sr - self.a0[f], info)
        return out

    # -- 3. full search -----------------------------------------------------
    def refine(self, f: str, q: str, pos0: float) -> float:
        sr = self.sr
        bf = int(round((pos0 + self.a0[f]) * sr))
        bq = int(round((self.pos[q] + self.a0[q]) * sr))
        w = int(round(REFINE_S * sr))
        arr = window_curve(self.sig[q], self.sig[f], bf - bq - w, bf - bq + w, sr)
        if not np.isfinite(arr).any():
            return pos0
        return (bf + int(np.nanargmax(arr)) - w) / sr - self.a0[f]

    def same_camera_conflict(self, f: str, start: float) -> str | None:
        cam = self.cam_of[f]
        if not cam:
            return None
        for g, p in self.pos.items():
            if g != f and self.cam_of[g] == cam and _overlap((start, start + self.dur[f]), (p, p + self.dur[g])) > OVERLAP_TOL_S:
                return g
        return None

    def search_place(self, todo: list[str], reason: str, lo: float, hi: float) -> None:
        for pass_no in range(1, MAX_PASSES + 1):
            pending = [f for f in todo if f not in self.pos and f in self.sig]
            anchors = sorted((q for q in self.pos if q in self.sig and self.info[q]["method"] in AUDIO_METHODS),
                             key=lambda q: -self.dur[q])
            if not pending or not anchors:
                return
            per = max(4, min(SEARCH_PARTNERS, SEARCH_BUDGET // len(pending)))
            tasks = [(f, q) for f in pending for q in anchors[:per]]
            self.log(f"full search pass {pass_no}: {len(pending)} file(s) against {min(per, len(anchors))} placed file(s)")
            matches = self.parallel(lambda t: self.full_match(*t), tasks, lo, hi, "full search")
            found = 0
            for f in pending:
                cands = sorted(((m[1], m[2], q, m[0]) for (g, q), m in zip(tasks, matches)
                                if g == f and m is not None and m[1] >= self.min_score and m[2] >= self.min_ratio),
                               reverse=True)
                for sc, rt, q, delta in cands:
                    start = self.refine(f, q, self.pos[q] + delta)
                    clash = self.same_camera_conflict(f, start)
                    if clash:
                        self.log(f"  {self.rel(f)}: match against {self.rel(q)} would overlap "
                                 f"{self.rel(clash)} of the same camera — skipped", "warn")
                        continue
                    self.pos[f] = start
                    self.info[f] = {"method": "search", "via": "full search", "reason": reason, "ref": q,
                                    "score": round(sc, 4), "ratio": round(rt, 3), "pass": pass_no}
                    self.log(f"  {self.rel(f)} → by full search against {self.rel(q)} "
                             f"(score {sc:.3f}, ratio {rt:.2f}) — check it", "warn")
                    self.publish()
                    found += 1
                    break
            if not found:
                return

    # -- frames ---------------------------------------------------------------
    def frame_delta(self) -> float:
        """Shift from the fresh frame (reference camera's clock) into the
        session's existing frame, so takes and manual offsets stay attached to
        the same footage. Anchors: files that were trusted before and are placed
        by audio now (the previous reference first); else the kept manual
        files; else wherever the files were before."""
        anchors = []
        for fid, p in self.pos.items():
            f = self.by_id[fid]
            if (fid in self.kept or f.get("offset") is None or self.info[fid]["method"] not in AUDIO_METHODS
                    or (f.get("sync") or {}).get("method") not in TRUSTED):
                continue
            anchors.append((f["sync"]["method"] == "reference", f["offset"] - p))
        if anchors:
            prev_ref = [d for is_ref, d in anchors if is_ref]
            return prev_ref[0] if prev_ref else statistics.median(d for _, d in anchors)
        manual = [off - self.predict(fid) for fid, off in self.kept.items() if self.predict(fid) is not None]
        if manual:
            return statistics.median(manual)
        fresh = {fid: p for fid, p in self.pos.items()}
        for fid in self.by_id:
            if fid not in fresh and self.predict(fid) is not None and self.clock(fid).attached:
                fresh[fid] = self.predict(fid)
        prev = [self.by_id[fid]["offset"] - p for fid, p in fresh.items()
                if fid not in self.kept and self.by_id[fid].get("offset") is not None]
        return statistics.median(prev) if prev else 0.0

    # -- the whole job --------------------------------------------------------
    def run(self) -> dict:
        job = self.job
        if self.kept:
            self.log(f"keeping {len(self.kept)} manual offset(s)")
        self.publish("audio")
        self.load_signals()
        self.publish("links")
        self.link_stage()
        seed = self.attach_stage()
        self.publish(force=True)

        # 2. reference + fine alignment of everything with a known clock
        if seed is not None:
            dev = statistics.median(self.residuals[seed]) if self.residuals.get(seed) else 0.0
            self.pos[seed] = self.predict(seed) + dev
            self.info[seed] = {"method": "reference"}
            self.log(f"reference: {self.rel(seed)} ({self.dur[seed]:.1f} s, camera {self.cname(self.cam_of[seed])})")
            self.publish("align")
            todo = [f for f in self.sig if f != seed and f not in self.kept and self.clock(f) is not None
                    and self.clock(f).attached]
            self.log(f"fine alignment: {len(todo)} file(s), each within ±W of its camera clock prediction")
            for f, (p, inf) in self.fine_align(todo, {seed: self.pos[seed]}, 0.42, 0.8).items():
                self.pos[f], self.info[f] = p, inf
        elif self.sig:
            seed = max((f for f in self.sig if f not in self.kept), key=lambda f: (self.dur[f], f), default=None)
            if seed is not None:
                self.pos[seed], self.info[seed] = 0.0, {"method": "reference"}
                self.log(f"reference: {self.rel(seed)} ({self.dur[seed]:.1f} s) — no creation times, full search only", "warn")
        else:
            self.log("no usable audio in this session — placing by metadata", "warn")

        # cameras whose clock stayed unknown: their manual offsets, else the median clock
        unattached = sorted({c for c, clk in self.clocks.items() if not clk.attached},
                            key=lambda c: self.clocks[c].comp)
        known = [clk.offset for clk in self.clocks.values() if clk.attached]
        median_k = statistics.median(known) if known else 0.0
        comps: dict[int, list[str]] = {}
        for c in unattached:
            comps.setdefault(self.clocks[c].comp, []).append(c)
        for cams in comps.values():
            for c in cams:
                self.clocks[c].offset += median_k
                self.clocks[c].via = "median clock"
        delta = self.frame_delta()
        for cams in comps.values():
            manual = [off - delta - self.predict(fid) for fid, off in self.kept.items()
                      if self.cam_of[fid] in cams and self.predict(fid) is not None]
            if manual:
                d = statistics.median(manual)
                for c in cams:
                    self.clocks[c].offset += d
                    self.clocks[c].via = "manual offset"
        if self.clocks:
            self.log_clocks()
        for cams in comps.values():
            via = self.clocks[cams[0]].via
            todo = [f for f in self.sig if self.cam_of[f] in cams and f not in self.kept and self.ct[f] is not None]
            for f in todo:   # camera with no confident links: today's full search, checked against its own clock
                hit = self.search_hits.get(f)
                if hit is None or hit[0] not in self.pos:
                    continue
                q, d, sc, rt = hit
                start = self.refine(f, q, self.pos[q] + d)
                if self.same_camera_conflict(f, start):
                    continue
                self.pos[f] = start
                self.info[f] = {"method": "search", "via": "full search", "reason": "camera clock not linked",
                                "ref": q, "score": round(sc, 4), "ratio": round(rt, 3)}
            anchors = {q: p for q, p in self.pos.items() if self.info[q]["method"] in AUDIO_METHODS}
            rest = [f for f in todo if f not in self.pos]
            for f, (p, inf) in self.fine_align(rest, anchors, 0.8, 0.85, clock_via=via).items():
                self.pos[f], self.info[f] = p, inf

        # 3. files without a creation time: full search only
        no_clock = [f for f in self.sig if f not in self.kept and f not in self.pos and self.predict(f) is None]
        if no_clock:
            self.publish("search")
            self.search_place(no_clock, "no creation time", 0.85, 0.95)
            for f in no_clock:
                if f not in self.pos:
                    self.log(f"  {self.rel(f)}: no confident match and no creation time — put after the end", "error")

        # no audio (or nothing usable): the camera clock's prediction
        for f in self.by_id:
            if f in self.kept or f in self.pos:
                continue
            p = self.predict(f)
            if p is None:
                continue
            clk = self.clock(f)
            via = "camera clock" if clk.via in ("reference", "link", "search") else clk.via
            if not known and not self.kept:
                via = "creation time"
            reason = "no audio track" if not self.by_id[f]["probe"].get("audio") else "audio unusable"
            self.pos[f], self.info[f] = p, {"method": "metadata", "via": via, "reason": reason}

        # into the session's existing frame; unknown files after the end
        offsets = dict(self.kept)
        info = {fid: self.by_id[fid]["sync"] for fid in self.kept}
        for f, p in self.pos.items():
            offsets[f], info[f] = p + delta, self.info[f]
        for fid, off in place_after_end(self.files, offsets).items():
            offsets[fid], info[fid] = off, {"method": "none"}
        self.publish("check")
        self.check(offsets, info, delta)

        shift = -min(offsets.values())
        job.set_progress(0.97, "saving")
        summary = self.summary(delta + shift)
        with self.store.edit(self.name) as d:
            for f in d["files"]:
                if f["id"] not in offsets:
                    continue
                if not self.resync_manual and f["id"] not in self.kept and f["sync"].get("method") == "manual":
                    # nudged by the user while we were busy: keep it, but move it with everything else
                    if f.get("offset") is not None:
                        f["offset"] = round(f["offset"] + shift, 6)
                    continue
                f["offset"] = round(offsets[f["id"]] + shift, 6)
                f["sync"] = info[f["id"]]
            if abs(shift) > 1e-9:
                for t in d["takes"]:
                    t["start"] = round(t["start"] + shift, 6)
                    t["end"] = round(t["end"] + shift, 6)
            d["sync"] = summary
            d["state"]["synced_at"] = now()
        counts: dict[str, int] = {}
        for inf in info.values():
            counts[inf.get("method", "none")] = counts.get(inf.get("method", "none"), 0) + 1
        flagged = sum(1 for inf in info.values() if inf.get("flags"))
        for f in self.files:   # final picture for the live view, including manual and unplaced files
            if f["id"] not in self.pos and f["id"] in offsets:
                self.pos[f["id"]] = offsets[f["id"]] - delta
            self.info[f["id"]] = info.get(f["id"], {})
        self.live_frame = delta + shift
        self.publish("done")
        self.log("done: " + ", ".join(f"{v} {k}" for k, v in sorted(counts.items()))
                 + (f" · {flagged} flagged" if flagged else ""))
        return {"counts": counts, "shift": shift, "flagged": flagged}

    def check(self, offsets: dict[str, float], info: dict[str, dict], delta: float) -> None:
        """No two clips of one camera at once; flag files far off their camera's clock."""
        def flag(fid, msg):
            if fid in self.kept:
                return
            info[fid] = dict(info[fid])
            info[fid].setdefault("flags", []).append(msg)
        by_cam: dict[str, list[tuple[float, str]]] = {}
        for fid, off in offsets.items():
            if self.cam_of[fid]:
                by_cam.setdefault(self.cam_of[fid], []).append((off, fid))
        for items in by_cam.values():
            items.sort()
            end, end_f = -math.inf, None
            for off, fid in items:
                if end_f is not None and end - off > OVERLAP_TOL_S:
                    ov = end - off
                    self.log(f"  {self.rel(fid)} overlaps {self.rel(end_f)} of the same camera by {ov:.2f} s", "warn")
                    flag(fid, f"overlaps {self.rel(end_f)} by {ov:.2f} s (same camera)")
                    flag(end_f, f"overlaps {self.rel(fid)} by {ov:.2f} s (same camera)")
                if off + self.dur[fid] > end:
                    end, end_f = off + self.dur[fid], fid
        for fid, off in offsets.items():
            clk = self.clock(fid)
            if fid in self.kept or clk is None or clk.via not in ("reference", "link", "search", "manual offset"):
                continue
            dev = off - delta - self.predict(fid)
            info[fid] = dict(info[fid], clock_dev=round(dev, 3))
            if abs(dev) > clk.window + 1e-6 and info[fid].get("method") != "reference":
                self.log(f"  {self.rel(fid)}: {dev:+.2f} s off its camera's clock (window ±{clk.window:.2f} s)", "warn")
                flag(fid, f"{dev:+.2f} s off the camera clock (window ±{clk.window:.2f} s)")

    def summary(self, frame: float) -> dict:
        """Saved as data["sync"]. ``frame`` maps the fresh frame (reference clock,
        where file f's prediction is ``guess(f) + k``) to session time."""
        ref = self.clocks.get(self.ref_cam)
        cams = {}
        for c, clk in self.clocks.items():
            cams[c] = {"offset": round(clk.offset - (ref.offset if ref else 0.0), 3), "k": clk.offset, "mode": clk.mode,
                       "mode_basis": clk.mode_basis, "support": clk.support, "votes": clk.votes,
                       "spread": round(clk.spread, 3), "window": round(clk.window, 3), "via": clk.via,
                       "linked_to": clk.parent, "attached": clk.attached}
        return {"reference_camera": self.ref_cam, "cameras": cams, "frame": frame, "sample_rate": self.sr,
                "link_sample_rate": LINK_SR, "window_min": self.win_min,
                "pairs": [{"a": a, "b": b, **p} for (a, b), p in self.live_pairs.items()]}


def sync_session(store: SessionStore, name: str, job: Job, resync_manual: bool = False) -> dict:
    return SessionSync(store, name, job, resync_manual).run()


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
