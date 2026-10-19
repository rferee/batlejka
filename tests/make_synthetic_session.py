"""Build a synthetic mocap session with known ground truth.

    python tests/make_synthetic_session.py [--out tests/_tmp/sessions] [--force]

Creates ``<out>/synthetic/source/`` with four fake cameras cut from the test
clips in ``F:\\freemocap\\calibration`` (override with ``MOCAP_TEST_CLIPS``):

* ``fuji``      DSCF0001.MOV, one continuous clip, stored rotated with
                display-matrix rotation metadata.
* ``iphone_a``  IMG_01xx.MOV ×3 and ``iphone_b`` IMG_02xx.MOV ×3: identical
                make/model/codec/resolution/fps, different views, overlapping
                in time, realistic creation dates with clock error.
* ``pixel``     VID_*.mp4 ×3 at 30 fps instead of 29.97; one clip has no audio.

Every camera gets the same "room" soundtrack (music + claps at take starts)
through its own EQ, gain and noise. Full-frame white flashes are burned into
the video at known frames so exports can be checked for visual alignment.
``ground_truth.json`` records cameras, true offsets, takes, flashes.

``--scenarios`` also builds small ``scn_*`` sessions for the two-stage sync
(looping music, a camera minutes off that stamps end times, no continuous
camera, files without metadata); see SCENARIOS.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from fractions import Fraction
from pathlib import Path

import numpy as np
from scipy import signal
from scipy.io import wavfile

GENERATOR_VERSION = 4
HERE = Path(__file__).resolve().parent
DEFAULT_OUT = HERE / "_tmp" / "sessions"
CLIPS_DIR = Path(os.environ.get("MOCAP_TEST_CLIPS", r"F:\freemocap\calibration"))

FPS = Fraction(30000, 1001)
SR = 48000
SPAN_FRAMES = 1920                      # 64.06 s at 29.97
EPOCH = datetime(2026, 10, 19, 12, 0, 0, tzinfo=timezone.utc)

# Clip intervals in base frames [start, end) per take.
CLIPS = {
    "iphone_a": [(120, 557), (692, 1133), (1298, 1738)],
    "iphone_b": [(138, 575), (710, 1109), (1286, 1720)],
    "pixel":    [(156, 539), (719, 1145), (1313, 1765)],
}
CLAP_FRAMES = [180, 764, 1355]          # one loud clap per take, all cameras running
FLASH_FRAMES = [268, 418, 835, 990, 1415, 1612]  # 2-frame full-white flashes
FLASH_LEN = 2
NO_AUDIO = ("pixel", 1)                 # (camera, clip index) recorded without audio
CLOCK_ERROR = {"fuji": 3.4, "iphone_a": -2.2, "iphone_b": 1.7, "pixel": 4.1}

CAMERAS = {
    # name: (source clip, crop filter, output size, container, make, model, fps)
    "fuji": ("synchronized_videos/synced_cam3.mp4", "crop=1920:1080:0:1100", (640, 360), "MOV", "FUJIFILM", "X-T4", FPS),
    "iphone_a": ("synced_cam1.mp4", "crop=1080:1920:0:0", (360, 640), "MOV", "Apple", "iPhone 13", FPS),
    "iphone_b": ("synchronized_videos/synced_cam2.mp4", "crop=1080:1920:0:0", (360, 640), "MOV", "Apple", "iPhone 13", FPS),
    "pixel": ("synchronized_videos/synced_cam3.mp4", "crop=1080:1920:840:1494", (360, 640), "mp4", "Google", "Pixel 7", Fraction(30)),
}
MIC = {
    # name: (filter kind, cutoff(s), gain, noise level, seed)
    "fuji": ("lowpass", 6000, 0.7, 0.010, 11),
    "iphone_a": ("highpass", 150, 1.0, 0.016, 12),
    "iphone_b": ("bandpass", (300, 5000), 0.6, 0.012, 13),
    "pixel": ("bandpass", (100, 4000), 0.9, 0.014, 14),
}


def ffmpeg(*args: str) -> None:
    cmd = ["ffmpeg", "-v", "error", "-y", *map(str, args)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"ffmpeg failed: {' '.join(cmd)}\n{r.stderr[-2000:]}")


def frame_time(n: int) -> float:
    return float(Fraction(n) / FPS)


# --------------------------------------------------------------------- audio

def room_soundtrack(seconds: float, rng: np.random.Generator) -> np.ndarray:
    n = int(seconds * SR)
    t = np.arange(n) / SR
    # "music": band-limited noise pumped by a beat, plus a random chord progression
    noise = rng.standard_normal(n)
    b, a = signal.butter(2, [200, 2500], btype="bandpass", fs=SR)
    pad = signal.lfilter(b, a, noise) * 0.05
    beat = 0.5 + 0.5 * (np.sin(2 * np.pi * 2.0 * t) > 0.6)
    music = pad * beat
    bar = 2.0
    notes = [110, 131, 147, 165, 196, 220, 262, 294, 330, 392]
    for k in range(int(seconds / bar) + 1):
        sl = slice(int(k * bar * SR), min(n, int((k + 1) * bar * SR)))
        tt = t[sl] - k * bar
        env = np.exp(-tt * 1.2)
        for f in rng.choice(notes, size=3, replace=False):
            music[sl] += 0.03 * env * np.sin(2 * np.pi * f * tt + rng.uniform(0, 6.28))
    # kick drum every beat (repetitive on purpose)
    for k in range(int(seconds * 2)):
        s = int(k * 0.5 * SR)
        L = min(int(0.12 * SR), n - s)
        if L <= 0:
            break
        tt = np.arange(L) / SR
        music[s:s + L] += 0.12 * np.sin(2 * np.pi * (60 + 40 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 25)
    # claps: sharp broadband burst + short reverb tail
    for fr in CLAP_FRAMES:
        s = int(round(frame_time(fr) * SR))
        L = int(0.25 * SR)
        tt = np.arange(L) / SR
        burst = rng.standard_normal(L) * (0.9 * np.exp(-tt / 0.012) + 0.08 * np.exp(-tt / 0.08))
        music[s:s + L] += burst[: max(0, min(L, n - s))]
    return music.astype(np.float64)


def mic(track: np.ndarray, cam: str) -> np.ndarray:
    kind, cut, gain, noise_level, seed = MIC[cam]
    b, a = signal.butter(2, cut, btype=kind, fs=SR)
    y = signal.lfilter(b, a, track) * gain
    y += np.random.default_rng(seed).standard_normal(len(y)) * noise_level
    return np.clip(y, -1, 1).astype(np.float32)


def write_wav(path: Path, samples: np.ndarray) -> None:
    wavfile.write(str(path), SR, (samples * 32767).astype(np.int16))


# --------------------------------------------------------------------- video

def make_view(cam: str, work: Path) -> Path:
    src_rel, crop, (w, h), *_ = CAMERAS[cam]
    src = CLIPS_DIR / src_rel
    out = work / f"view_{cam}.mp4"
    flashes = "+".join(f"between(n,{f},{f + FLASH_LEN - 1})" for f in FLASH_FRAMES)
    vf = (f"{crop},scale={w}:{h}:flags=area,setsar=1,"
          f"drawbox=x=0:y=0:w=iw:h=ih:color=white@1:t=fill:enable='{flashes}',format=yuv420p")
    ffmpeg("-i", src, "-frames:v", SPAN_FRAMES, "-vf", vf, "-an", "-c:v", "libx264",
           "-preset", "veryfast", "-crf", "18", "-g", "30", out)
    return out


def iso(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def cut_clip(cam: str, view: Path, start: int, end: int, wav: Path | None, out: Path, created: datetime,
             extra_vf: str = "") -> None:
    _, _, _, _, make, model, fps = CAMERAS[cam]
    vf = f"trim=start_frame={start}:end_frame={end},setpts=PTS-STARTPTS"
    if fps != FPS:
        vf += f",fps={fps.numerator}/{fps.denominator}"
    if extra_vf:
        vf += "," + extra_vf
    args = ["-i", view]
    if wav is not None:
        args += ["-i", wav]
    args += ["-map", "0:v:0"]
    if wav is not None:
        args += ["-map", "1:a:0", "-c:a", "aac", "-b:a", "128k"]
    args += ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "30",
             "-pix_fmt", "yuv420p", "-movflags", "use_metadata_tags",
             "-metadata", f"creation_time={iso(created)}"]
    if make == "Apple":
        local = created.astimezone(timezone(timedelta(hours=3)))
        args += ["-metadata", f"com.apple.quicktime.make={make}", "-metadata", f"com.apple.quicktime.model={model}",
                 "-metadata", f"com.apple.quicktime.creationdate={local.strftime('%Y-%m-%dT%H:%M:%S%z')}"]
    else:
        args += ["-metadata", f"make={make}", "-metadata", f"model={model}"]
    ffmpeg(*args, out)


def build(out_root: Path, force: bool = False) -> Path:
    session = out_root / "synthetic"
    source = session / "source"
    gt_path = session / "ground_truth.json"
    if gt_path.exists() and not force:
        try:
            if json.loads(gt_path.read_text())["generator_version"] == GENERATOR_VERSION:
                return session
        except (ValueError, KeyError):
            pass
    if not (CLIPS_DIR / "synced_cam1.mp4").exists():
        raise FileNotFoundError(f"test clips not found in {CLIPS_DIR} (set MOCAP_TEST_CLIPS)")
    if session.exists():
        shutil.rmtree(session)
    source.mkdir(parents=True)
    work = out_root / "_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)

    rng = np.random.default_rng(1234)
    span = frame_time(SPAN_FRAMES)
    track = room_soundtrack(span + 1.0, rng)
    gt = {"generator_version": GENERATOR_VERSION, "fps": "30000/1001", "span": span, "files": {},
          "cameras": {}, "takes": [], "flashes": [frame_time(f) for f in FLASH_FRAMES],
          "flash_frames": FLASH_FRAMES, "claps": [frame_time(f) for f in CLAP_FRAMES], "no_audio": []}

    # fuji: continuous, stored transposed + display-matrix rotation (90° clockwise)
    view = make_view("fuji", work)
    a = mic(track, "fuji")[: int(round(span * SR))]
    wav = work / "fuji.wav"
    write_wav(wav, a)
    tmp = work / "fuji_raw.mov"
    created = EPOCH + timedelta(seconds=CLOCK_ERROR["fuji"])
    ffmpeg("-i", view, "-i", wav, "-map", "0:v:0", "-map", "1:a:0", "-vf", "transpose=2",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-g", "30", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "128k", tmp)
    ffmpeg("-display_rotation", "-90", "-i", tmp, "-c", "copy", "-movflags", "use_metadata_tags",
           "-metadata", f"creation_time={iso(created)}", "-metadata", "make=FUJIFILM",
           "-metadata", "model=X-T4", source / "DSCF0001.MOV")
    gt["files"]["DSCF0001.MOV"] = {"camera": "fuji", "offset": 0.0, "duration": span, "audio": True,
                                   "rotation_meta": 90, "display": [640, 360]}
    gt["cameras"]["fuji"] = ["DSCF0001.MOV"]

    names = {
        "iphone_a": ["IMG_0101.MOV", "IMG_0102.MOV", "IMG_0103.MOV"],
        "iphone_b": ["IMG_0201.MOV", "IMG_0202.MOV", "IMG_0203.MOV"],
        "pixel": ["VID_20261019_120004.mp4", "VID_20261019_120024.mp4", "VID_20261019_120044.mp4"],
    }
    for cam, clips in CLIPS.items():
        view = make_view(cam, work)
        audio = mic(track, cam)
        gt["cameras"][cam] = []
        for i, (s, e) in enumerate(clips):
            rel = names[cam][i]
            t0, t1 = frame_time(s), frame_time(e)
            has_audio = (cam, i) != NO_AUDIO
            wav = None
            if has_audio:
                wav = work / f"{cam}_{i}.wav"
                write_wav(wav, audio[int(round(t0 * SR)): int(round(t1 * SR))])
            # creation dates have whole-second resolution, like real devices
            created = EPOCH + timedelta(seconds=int(t0 + CLOCK_ERROR[cam]))
            cut_clip(cam, view, s, e, wav, source / rel, created)
            gt["files"][rel] = {"camera": cam, "offset": t0, "duration": t1 - t0, "audio": has_audio}
            gt["cameras"][cam].append(rel)
            if not has_audio:
                gt["no_audio"].append(rel)

    for k in range(len(CLAP_FRAMES)):
        starts = [frame_time(CLIPS[c][k][0]) for c in CLIPS]
        ends = [frame_time(CLIPS[c][k][1]) for c in CLIPS]
        gt["takes"].append({"start": max(starts), "end": min(ends)})

    gt_path.write_text(json.dumps(gt, indent=1))
    shutil.rmtree(work, ignore_errors=True)
    return session


# --------------------------------------------------------------------- sync scenarios
#
# Small sessions (tiny static video, real AAC audio) that stress the two-stage
# sync: every camera has its own clock, error and stamp convention.

SCENARIO_VERSION = 2


@dataclass
class ScnCam:
    prefix: str
    make: str | None
    model: str | None
    clips: list[tuple[float, float]]     # true [start, end) in seconds
    clock: float = 0.0                   # camera clock − true time
    stamp: str = "start"                 # creation_time marks the start or the end
    mic: tuple = ("bandpass", (150, 5000), 0.8, 0.010)
    music: float = 1.0                   # gains of the room's parts at this camera
    activity: float = 0.08
    no_ct: tuple[int, ...] = ()          # clips without creation_time
    anon: bool = False                   # no metadata at all
    ext: str = "MOV"


@dataclass
class Scenario:
    span: float
    track: str                           # "room" or "loop"
    cams: dict[str, ScnCam]
    activity: list[tuple[float, float]] = field(default_factory=list)
    speech: list[tuple[float, float]] = field(default_factory=list)
    note: str = ""


SCENARIOS = {
    # (a) one 6 s music loop for the whole session; loud handling noise next to
    # the continuous camera during the first take makes a whole-file search of
    # that take's clips pick a quieter copy of the loop.
    "scn_loop": Scenario(
        span=130, track="loop", activity=[(28, 54)], speech=[(32, 50), (66, 82), (99, 117)],
        note="looping music: a full search of take 1 against the continuous camera picks a loop copy",
        cams={
            "cont": ScnCam("C", "Sony", "ILME-FX3", [(0, 130)], clock=2.6, ext="MP4",
                           mic=("lowpass", 6000, 0.7, 0.008), activity=1.6),
            "a": ScnCam("IMG_1", "Apple", "iPhone 15", [(30, 52), (64, 84), (98, 120)], clock=0.3,
                        mic=("highpass", 150, 1.0, 0.012)),
            "b": ScnCam("SM_", "samsung", "SM-S918B", [(31, 51), (65, 83), (97, 119)], clock=-0.6, ext="mp4",
                        mic=("bandpass", (300, 5000), 0.6, 0.010)),
        }),
    # (b) an action camera whose clock is 3 min fast and that stamps the END of each clip
    "scn_clock": Scenario(
        span=150, track="room",
        cams={
            "cont": ScnCam("P10", "Panasonic", "DC-GH6", [(0, 150)], clock=-1.4, mic=("lowpass", 6000, 0.7, 0.010)),
            "p": ScnCam("IMG_2", "Apple", "iPhone 13 mini", [(10, 32), (48, 61), (85, 128)], clock=0.2,
                        mic=("highpass", 150, 1.0, 0.014)),
            "q": ScnCam("PXL_", "Google", "Pixel 8", [(11, 31), (47, 62), (86, 127)], clock=-0.5, ext="mp4",
                        mic=("bandpass", (100, 4000), 0.9, 0.012)),
            "gopro": ScnCam("GX01", "GoPro", "HERO11 Black", [(9, 33), (50, 58), (84, 129)], clock=197.3,
                            stamp="end", ext="MP4", mic=("bandpass", (200, 6000), 0.8, 0.015)),
        }),
    # (c) no continuous camera: takes 1-2 bridged by one long clip, take 3 on its own
    "scn_nocont": Scenario(
        span=100, track="room",
        cams={
            "a": ScnCam("IMG_3", "Apple", "iPhone 12", [(5, 25), (30, 50), (70, 90)], clock=0.4,
                        mic=("highpass", 150, 1.0, 0.012)),
            "b": ScnCam("MI_", "Xiaomi", "2201123G", [(6, 24), (31, 49), (71, 88)], clock=-1.3, ext="mp4",
                        mic=("bandpass", (300, 5000), 0.6, 0.010)),
            "c": ScnCam("DJI_", "DJI", "Osmo Action 4", [(4, 40)], clock=38.2, ext="MP4",
                        mic=("bandpass", (100, 4000), 0.9, 0.014)),
        }),
    # (d) files without metadata: one phone clip lacks creation_time, one camera has none at all
    "scn_nometa": Scenario(
        span=110, track="room",
        cams={
            "cont": ScnCam("MVI_", "Canon", "EOS R6", [(0, 110)], clock=0.9, ext="MP4", mic=("lowpass", 6000, 0.7, 0.010)),
            "p": ScnCam("IMG_4", "Apple", "iPhone 14", [(12, 30), (45, 63), (80, 100)], clock=-0.3, no_ct=(1,),
                        mic=("highpass", 150, 1.0, 0.012)),
            "anon": ScnCam("REC", None, None, [(20, 35), (60, 78), (85, 104)], anon=True, ext="mp4",
                           mic=("bandpass", (200, 4500), 0.8, 0.012)),
        }),
}


def snap(t: float) -> int:
    return int(round(Fraction(t).limit_denominator(1000) * FPS))


def loop_track(seconds: float, sc: Scenario, rng: np.random.Generator) -> tuple[np.ndarray, np.ndarray]:
    """Music made of one 6 s loop repeated sample-exactly, plus the 'activity'
    (handling noise, footsteps) and per-take speech-like sounds returned
    separately so each camera can hear them at its own level."""
    L = int(6.0 * SR)
    t = np.arange(L) / SR
    b, a = signal.butter(2, [200, 3000], btype="bandpass", fs=SR)
    loop = signal.lfilter(b, a, rng.standard_normal(L)) * 0.05 * (0.65 + 0.35 * np.sin(2 * np.pi * t / 6.0))
    for k in range(4):   # a chord every 1.5 s
        sl = slice(int(k * 1.5 * SR), int((k + 1) * 1.5 * SR))
        tt = t[sl] - k * 1.5
        for f in rng.choice([110, 131, 147, 165, 196, 220, 262, 294], size=3, replace=False):
            loop[sl] += 0.025 * np.exp(-tt * 1.5) * np.sin(2 * np.pi * f * tt + rng.uniform(0, 6.28))
    for k in range(12):  # kick every 0.5 s
        s = int(k * 0.5 * SR)
        tt = np.arange(int(0.12 * SR)) / SR
        loop[s:s + len(tt)] += 0.10 * np.sin(2 * np.pi * (60 + 40 * np.exp(-tt * 30)) * tt) * np.exp(-tt * 25)
    n = int(seconds * SR)
    music = np.tile(loop, n // L + 1)[:n]
    return music, extras(n, sc, rng)


def extras(n: int, sc: Scenario, rng: np.random.Generator) -> np.ndarray:
    act = np.zeros(n)
    b, a = signal.butter(2, [100, 4000], btype="bandpass", fs=SR)
    for t0, t1 in sc.activity:
        s0, s1 = int(t0 * SR), min(n, int(t1 * SR))
        env = np.repeat(np.abs(rng.standard_normal((s1 - s0) // 2400 + 1)), 2400)[: s1 - s0]
        act[s0:s1] += signal.lfilter(b, a, rng.standard_normal(s1 - s0)) * 0.08 * signal.lfilter([1 / 8] * 8, 1, env)
    return act


def speech_track(n: int, sc: Scenario, rng: np.random.Generator) -> np.ndarray:
    """Syllable-like bursts of formant-filtered noise during each take, heard by everyone."""
    out = np.zeros(n)
    for t0, t1 in sc.speech:
        t = t0
        while t < t1:
            dur = rng.uniform(0.08, 0.3)
            s, L = int(t * SR), int(dur * SR)
            f0 = rng.uniform(300, 1800)
            b, a = signal.butter(2, [f0, f0 * 1.6], btype="bandpass", fs=SR)
            out[s:s + L] += signal.lfilter(b, a, rng.standard_normal(L)) * 0.09 * np.hanning(L)
            t += dur + rng.uniform(0.05, 0.4)
    return out


def scn_mic(track: np.ndarray, spec: tuple, seed: int) -> np.ndarray:
    kind, cut, gain, noise_level = spec
    b, a = signal.butter(2, cut, btype=kind, fs=SR)
    y = signal.lfilter(b, a, track) * gain
    y += np.random.default_rng(seed).standard_normal(len(y)) * noise_level
    return np.clip(y, -1, 1).astype(np.float32)


def view_image(path: Path, rng: np.random.Generator) -> None:
    """A static, textured 'room' per camera (blocky noise), as a binary PPM."""
    img = np.kron(rng.integers(30, 220, (9, 16, 3)), np.ones((10, 10, 1))).astype(np.uint8)
    path.write_bytes(b"P6 160 90 255\n" + img.tobytes())


def build_scenario(out_root: Path, name: str, force: bool = False) -> Path:
    sc = SCENARIOS[name]
    session = out_root / name
    source = session / "source"
    gt_path = session / "ground_truth.json"
    if gt_path.exists() and not force:
        try:
            if json.loads(gt_path.read_text())["generator_version"] == SCENARIO_VERSION:
                return session
        except (ValueError, KeyError):
            pass
    if session.exists():
        shutil.rmtree(session)
    source.mkdir(parents=True)
    work = out_root / f"_work_{name}"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    rng = np.random.default_rng(sum(map(ord, name)))
    n = int((sc.span + 1) * SR)
    if sc.track == "loop":
        music, act = loop_track(sc.span + 1, sc, rng)
    else:
        music, act = room_soundtrack(sc.span + 1, rng), extras(n, sc, rng)
    speech = speech_track(n, sc, rng)
    gt = {"generator_version": SCENARIO_VERSION, "fps": "30000/1001", "note": sc.note, "files": {}, "cameras": {}}
    for ci, (key, cam) in enumerate(sc.cams.items()):
        img = work / f"{key}.ppm"
        view_image(img, rng)
        audio = scn_mic(music * cam.music + act * cam.activity + speech, cam.mic, 100 + ci)
        gt["cameras"][key] = {"files": [], "clock": cam.clock, "stamp": cam.stamp}
        for i, (t0, t1) in enumerate(cam.clips):
            f0, f1 = snap(t0), snap(t1)
            start, end = frame_time(f0), frame_time(f1)
            rel = f"{cam.prefix}{i + 1:04d}.{cam.ext}"
            wav = work / f"{key}_{i}.wav"
            write_wav(wav, audio[int(round(start * SR)): int(round(end * SR))])
            args = ["-loop", "1", "-framerate", "30000/1001", "-i", img, "-i", wav, "-map", "0:v", "-map", "1:a",
                    "-frames:v", f1 - f0, "-c:v", "libx264", "-preset", "ultrafast", "-g", "30", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-b:a", "96k", "-map_metadata", "-1", "-movflags", "use_metadata_tags"]
            has_ct = not cam.anon and i not in cam.no_ct
            if has_ct:   # whole seconds, like real devices
                stamp = (start if cam.stamp == "start" else end) + cam.clock
                args += ["-metadata", f"creation_time={iso(EPOCH + timedelta(seconds=math.floor(stamp)))}"]
            if not cam.anon:
                if cam.make == "Apple":
                    args += ["-metadata", f"com.apple.quicktime.make={cam.make}",
                             "-metadata", f"com.apple.quicktime.model={cam.model}"]
                else:
                    args += ["-metadata", f"make={cam.make}", "-metadata", f"model={cam.model}"]
            ffmpeg(*args, source / rel)
            gt["files"][rel] = {"camera": key, "offset": start, "duration": end - start, "audio": True, "ct": has_ct}
            gt["cameras"][key]["files"].append(rel)
    gt_path.write_text(json.dumps(gt, indent=1))
    shutil.rmtree(work, ignore_errors=True)
    return session


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="sessions folder to create the sessions in")
    ap.add_argument("--force", action="store_true", help="rebuild even if up to date")
    ap.add_argument("--scenarios", action="store_true", help="also build the sync scenarios (scn_*)")
    args = ap.parse_args()
    session = build(Path(args.out), force=args.force)
    print(f"synthetic session ready: {session}")
    if args.scenarios:
        for name in SCENARIOS:
            print(f"scenario ready: {build_scenario(Path(args.out), name, force=args.force)}")


if __name__ == "__main__":
    sys.exit(main())
