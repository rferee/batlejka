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
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", default=str(DEFAULT_OUT), help="sessions folder to create 'synthetic' in")
    ap.add_argument("--force", action="store_true", help="rebuild even if up to date")
    args = ap.parse_args()
    session = build(Path(args.out), force=args.force)
    print(f"synthetic session ready: {session}")


if __name__ == "__main__":
    sys.exit(main())
