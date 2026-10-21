"""Export checks (§9.5): equal frame counts, target fps, rotation, visual
alignment of the burned-in flashes across cameras, and padding."""
from __future__ import annotations

import json
import subprocess
import threading
import time
from fractions import Fraction

import numpy as np
import pytest

from conftest import files_by_rel
from mocap_preprocess import config, exporter, ffmpeg_tools as ff, takes
from mocap_preprocess.jobs import Job


def video_stream_info(path) -> dict | None:
    """The first video stream's width, height and frame rates, as ffprobe reports them."""
    r = ff.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                "stream=width,height,r_frame_rate,avg_frame_rate", "-of", "json", path], timeout=60)
    if r.returncode != 0:
        return None
    streams = json.loads(r.stdout.decode("utf-8", "replace") or "{}").get("streams") or []
    return streams[0] if streams else None

FPS = 30000 / 1001
WIDE = {"id": "t9", "name": "wide take", "start": 3.0, "end": 19.5, "export": True}
SPLIT = {"id": "t10", "name": "split take", "start": 12.0, "end": 30.0, "export": True}  # across every phone's break


def luminance(path) -> np.ndarray:
    r = subprocess.run([config.ffmpeg_path(), "-v", "error", "-i", str(path), "-vf", "scale=16:16,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, 256).mean(axis=1)


def display_rotation(path) -> float:
    """Rotation a player applies on top of the stored pixels (display matrix)."""
    r = subprocess.run([config.ffprobe_path(), "-v", "error", "-select_streams", "v:0", "-show_entries",
                        "stream_side_data=rotation", "-of", "json", str(path)], capture_output=True, check=True)
    sd = (json.loads(r.stdout).get("streams") or [{}])[0].get("side_data_list") or []
    return float(next((d["rotation"] for d in sd if "rotation" in d), 0))


def flash_starts(lum: np.ndarray) -> list[int]:
    hot = lum > 200
    return [int(i) for i in np.where(hot & ~np.r_[False, hot[:-1]])[0]]


def audio_track(path, sr: int = 8000) -> np.ndarray:
    r = subprocess.run([config.ffmpeg_path(), "-v", "error", "-i", str(path), "-vn", "-ac", "1", "-ar", str(sr),
                        "-f", "f32le", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.float32)


def test_plan_lays_a_cameras_files_end_to_end():
    """Every file of the camera that overlaps the take is used, in order, with black
    before / between / after them counted in output frames."""
    cam = {"id": "c1", "name": "Cam", "rotation": 0}

    def clip(rel, offset, duration):
        return {"rel": rel, "camera": "c1", "offset": offset,
                "probe": {"video": {"duration": duration}, "audio": {"sample_rate": 48000, "channels": 2}}}
    data = {"cameras": [cam], "files": [clip("b", 20.01, 10.0), clip("a", 4.0, 10.0), clip("c", 28.0, 10.0),
                                         clip("late", 50.0, 5.0)]}
    (plan,), skipped = exporter.plan_take(data, {"start": 10.0, "end": 40.0}, ["c1"], Fraction(30))
    assert not skipped
    got = [(s.file["rel"], s.gap, s.frames, round(s.seek, 6)) for s in plan.segments]
    # a: 6 s into the file at the take start; b after a 6 s break (its video on the
    # nearest frame); c overlaps b, so it starts where b ends (2 s into c); "late"
    # starts after the take
    assert got == [("a", 0, 120, 6.0), ("b", 180, 300, 0.0), ("c", 0, 240, 2.0)]
    assert plan.file["rel"] == "b" and plan.pre == 0 and plan.post == pytest.approx(2.0)
    args = [str(a) for a in exporter.build_joined_command(["a.mov", "b.mov", "c.mov"], "out.mp4", plan, Fraction(30), 900,
                                                          exporter.normalize_options({}, config.DEFAULT_EXPORT_OPTIONS), False)]
    graph = args[args.index("-filter_complex") + 1]
    assert args.count("-i") == 6 and "concat=n=3:v=1:a=0" in graph and "concat=n=3:v=0:a=1" in graph
    # b's sound starts at its exact offset, 6.01 s after its chain starts, not on the frame grid
    assert "adelay=288480S:all=1" in graph


@pytest.fixture(scope="module")
def exported(synced):
    store, name, gt = synced
    data = store.load(name)
    snapshot = store.load(name)
    by_rel = files_by_rel(data)
    fuji_cam = by_rel["DSCF0001.MOV"]["camera"]
    with store.edit(name) as d:
        d["takes"] = takes.detect(d) + [WIDE, SPLIT]
        for c in d["cameras"]:
            c["rotation"] = 90 if c["id"] == fuji_cam else 0
        # what the user does for the amber (metadata-placed) clip: nudge it by eye
        for f in d["files"]:
            if f["rel"] in gt["no_audio"]:
                f["offset"] = gt["files"][f["rel"]]["offset"]
                f["sync"] = {"method": "manual"}
    data = store.load(name)
    # three files at once: everything checked below also covers parallel encoding
    opts = exporter.normalize_options({"long_side": 0, "crf": 23, "overwrite": True, "parallel": 3}, config.DEFAULT_EXPORT_OPTIONS)
    job = Job("export", name)
    result = exporter.export(store, name, job, [t["id"] for t in data["takes"]], [c["id"] for c in data["cameras"]], opts)
    yield store, name, gt, data, result, job, fuji_cam
    store.save(name, snapshot)   # other test modules expect the synced state


def take_dirs(store, name, data):
    out = store.paths(name).output
    return [(t, out / exporter.sanitize_filename(t["name"])) for t in data["takes"]]


def test_export_completed(exported):
    _, _, _, data, result, job, _ = exported
    assert result["errors"] == 0, [e["msg"] for e in job._log if e["level"] == "error"]
    assert result["written"] == len(data["takes"]) * len(data["cameras"])
    assert "3 files at once" in job._log[0]["msg"]


def test_equal_frame_counts_and_fps(exported):
    store, name, _, data, _, _, _ = exported
    for tk, d in take_dirs(store, name, data):
        n = round((tk["end"] - tk["start"]) * FPS)
        vids = sorted((d / "synchronized_videos").glob("*_synced.mp4"))
        assert len(vids) == len(data["cameras"])
        for v in vids:
            assert ff.count_video_frames(v) == n, v
            info = video_stream_info(v)
            assert info["r_frame_rate"] == "30000/1001", v
        assert json.loads((d / "take_info.json").read_text())["frames"] == n


def test_rotation_applied(exported):
    store, name, _, data, _, _, fuji_cam = exported
    cams = {c["id"]: c["name"] for c in data["cameras"]}
    for _, d in take_dirs(store, name, data):
        fuji = video_stream_info(d / "synchronized_videos" / f"{cams[fuji_cam]}_synced.mp4")
        assert (fuji["width"], fuji["height"]) == (360, 640)   # 640×360 display, rotated 90°
        other = next(c for c in cams if c != fuji_cam)
        phone = video_stream_info(d / "synchronized_videos" / f"{cams[other]}_synced.mp4")
        assert (phone["width"], phone["height"]) == (360, 640)  # untouched portrait
        # rotation is baked into the pixels: no display matrix left for players to apply again
        for v in (d / "synchronized_videos").glob("*_synced.mp4"):
            assert display_rotation(v) == 0, v


def test_flashes_line_up_across_cameras(exported):
    store, name, gt, data, _, _, _ = exported
    for tk, d in take_dirs(store, name, data):
        if tk["id"] == WIDE["id"]:
            continue
        expected = [round((t - tk["start"]) * FPS) for t in gt["flashes"] if tk["start"] <= t < tk["end"]]
        assert expected, tk
        for v in sorted((d / "synchronized_videos").glob("*_synced.mp4")):
            got = flash_starts(luminance(v))
            assert len(got) == len(expected), (v, got, expected)
            for g, e in zip(got, expected):
                assert abs(g - e) <= 1, (v, got, expected)


def test_padding_when_camera_starts_late(exported):
    store, name, gt, data, _, _, _ = exported
    d = store.paths(name).output / exporter.sanitize_filename(WIDE["name"])
    info = json.loads((d / "take_info.json").read_text())
    n = round((WIDE["end"] - WIDE["start"]) * FPS)
    by_name = {c["name"]: c for c in info["cameras"]}
    pixel_first = gt["files"][gt["cameras"]["pixel"][0]]["offset"]
    pixel = next(c for c in info["cameras"] if c["source"] == gt["cameras"]["pixel"][0])
    assert pixel["pre_pad"] == pytest.approx(pixel_first - WIDE["start"], abs=0.01)
    assert pixel["post_pad"] > 1.0
    lum = luminance(d / "synchronized_videos" / pixel["output"].split("/")[-1])
    assert len(lum) == n
    black = int(pixel["pre_pad"] * FPS)
    assert lum[: black - 1].max() < 20            # black before the camera starts
    assert lum[black + 2: black + 30].mean() > 30  # real footage after
    # the continuous camera needs no padding at all
    fuji = next(c for c in info["cameras"] if c["source"] == "DSCF0001.MOV")
    assert fuji["pre_pad"] == 0 and fuji["post_pad"] == 0
    assert all(c["frames"] == n for c in by_name.values())


def test_take_across_a_break_joins_both_files(exported):
    """A take spanning a recording break: each phone's two clips are joined with
    black (and silence) in the break, not just the clip that covers the most."""
    store, name, gt, data, _, _, fuji_cam = exported
    d = store.paths(name).output / exporter.sanitize_filename(SPLIT["name"])
    info = json.loads((d / "take_info.json").read_text())
    n = round((SPLIT["end"] - SPLIT["start"]) * FPS)

    def frame(t):
        return round((t - SPLIT["start"]) * FPS)
    for cam in info["cameras"]:
        if cam["camera_id"] == fuji_cam:
            assert [s["source"] for s in cam["sources"]] == ["DSCF0001.MOV"]
            continue
        key = gt["files"][cam["source"]]["camera"]
        clips = [r for r in gt["cameras"][key]
                 if gt["files"][r]["offset"] < SPLIT["end"] and gt["files"][r]["offset"] + gt["files"][r]["duration"] > SPLIT["start"]]
        assert len(clips) == 2 and [s["source"] for s in cam["sources"]] == clips, cam
        first, second = (gt["files"][r] for r in clips)
        end1, start2 = frame(first["offset"] + first["duration"]), frame(second["offset"])
        out = d / "synchronized_videos" / cam["output"].split("/")[-1]
        lum = luminance(out)
        assert len(lum) == n and cam["frames"] == n
        assert lum[end1 - 30: end1 - 2].mean() > 30, cam["name"]       # end of the first clip
        assert lum[end1 + 2: start2 - 2].max() < 20, cam["name"]       # black in the break
        assert lum[start2 + 2: start2 + 30].mean() > 30, cam["name"]   # start of the second clip
        # sound where the clip had some, silence in the break and for the silent clip
        a, sr = audio_track(out), 8000

        def rms(t0, t1):
            return float(np.sqrt(np.mean(a[int((t0 - SPLIT["start"]) * sr): int((t1 - SPLIT["start"]) * sr)] ** 2)))
        assert abs(len(a) / sr - n / FPS) < 0.05, cam["name"]
        assert rms(first["offset"] + first["duration"] + 0.3, second["offset"] - 0.3) < 1e-4, cam["name"]
        for c, t0, t1 in ((first, SPLIT["start"] + 0.3, first["offset"] + first["duration"] - 0.3),
                          (second, second["offset"] + 0.3, SPLIT["end"] - 0.3)):
            assert (rms(t0, t1) > 1e-3) == c["audio"], (cam["name"], c)


def test_flat_layout_and_skip_without_overlap(exported):
    store, name, gt, data, _, _, _ = exported
    with store.edit(name) as d:
        d["takes"].append({"id": "t20", "name": "between takes", "start": 19.5, "end": 21.0, "export": True})
    data = store.load(name)
    opts = exporter.normalize_options({"layout": "flat", "long_side": 320, "audio": False}, config.DEFAULT_EXPORT_OPTIONS)
    job = Job("export", name)
    res = exporter.export(store, name, job, ["t20"], [c["id"] for c in data["cameras"]], opts)
    d = store.paths(name).output / "between takes"
    vids = sorted(d.glob("*_synced.mp4"))
    assert res["skipped"] == 3 and len(vids) == 1     # only the continuous camera covers the gap
    info = video_stream_info(vids[0])
    assert max(info["width"], info["height"]) == 320
    assert len(json.loads((d / "take_info.json").read_text())["skipped"]) == 3


def test_parallel_export_cancels_every_file(exported):
    store, name, _, data, _, _, _ = exported
    opts = exporter.normalize_options({"layout": "flat", "long_side": 0, "parallel": 3}, config.DEFAULT_EXPORT_OPTIONS)
    assert exporter.normalize_options({"parallel": 9}, config.DEFAULT_EXPORT_OPTIONS)["parallel"] == exporter.MAX_PARALLEL
    job = Job("export", name)
    threading.Timer(0.4, job.cancel_event.set).start()
    t0 = time.time()
    with pytest.raises(ff.Cancelled):
        exporter.export(store, name, job, [t["id"] for t in data["takes"]], [c["id"] for c in data["cameras"]], opts)
    assert time.time() - t0 < 10
    assert not list(store.paths(name).output.rglob("*.tmp.mp4"))   # partial files cleaned up


def _gpu_available() -> bool:
    ffmpeg = config.ffmpeg_path()
    if not ffmpeg:                        # no ffmpeg: the whole module skips anyway, don't fail collection
        return False
    try:
        r = subprocess.run([ffmpeg, "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=256x144:d=0.2",
                            "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, timeout=30)
        hw = subprocess.run([ffmpeg, "-hide_banner", "-hwaccels"], capture_output=True, text=True, timeout=30)
        return r.returncode == 0 and "cuda" in hw.stdout
    except (OSError, subprocess.TimeoutExpired):
        return False


@pytest.mark.skipif(not _gpu_available(), reason="needs an NVIDIA GPU (CUDA decode + NVENC)")
def test_gpu_pipeline_gives_the_same_takes(exported):
    """GPU decode/scale (+ CPU only for rotation and padding) → NVENC: same frame
    counts, rotation, flash alignment and padding as the CPU path."""
    store, name, gt, data, _, _, fuji_cam = exported
    with store.edit(name) as d:
        d["settings"]["hwaccel"] = True
    try:
        opts = exporter.normalize_options({"layout": "flat", "long_side": 0, "encoder": "h264_nvenc",
                                           "nvenc_preset": "p3"}, config.DEFAULT_EXPORT_OPTIONS)
        job = Job("export", name)
        res = exporter.export(store, name, job, [t["id"] for t in data["takes"]], [c["id"] for c in data["cameras"]], opts)
        log = [e["msg"] for e in job._log]
        assert res["errors"] == 0, log
        assert "GPU decode/scale" in log[0] and not any("retrying on CPU" in m for m in log), log
        cams = {c["id"]: c["name"] for c in data["cameras"]}
        for tk, _ in take_dirs(store, name, data):
            d = store.paths(name).output / exporter.sanitize_filename(tk["name"])
            n = round((tk["end"] - tk["start"]) * FPS)
            for c in cams.values():
                v = d / f"{c}_synced.mp4"
                assert ff.count_video_frames(v) == n, v
                info = video_stream_info(v)
                assert (info["width"], info["height"]) == (360, 640), v   # rotation, incl. the Fuji's metadata
                assert display_rotation(v) == 0, v                         # ...and not applied twice by players
            if tk["id"] != WIDE["id"]:
                expected = [round((t - tk["start"]) * FPS) for t in gt["flashes"] if tk["start"] <= t < tk["end"]]
                for c in cams.values():
                    got = flash_starts(luminance(d / f"{c}_synced.mp4"))
                    assert len(got) == len(expected) and all(abs(g - e) <= 1 for g, e in zip(got, expected)), (c, got, expected)
        info = json.loads((store.paths(name).output / exporter.sanitize_filename(WIDE["name"]) / "take_info.json").read_text())
        pixel = next(c for c in info["cameras"] if c["source"] == gt["cameras"]["pixel"][0])
        lum = luminance(store.paths(name).output / exporter.sanitize_filename(WIDE["name"]) / pixel["output"])
        black = int(pixel["pre_pad"] * FPS)
        assert lum[: black - 1].max() < 20 and lum[black + 2: black + 30].mean() > 30
    finally:
        with store.edit(name) as d:
            d["settings"]["hwaccel"] = False


def test_audio_is_read_through_its_own_input(exported):
    """One demuxer feeding both video and a long-delayed audio branch can deadlock
    ffmpeg (seen with a 53 s black lead-in); the audio gets a second input."""
    store, name, _, data, _, _, _ = exported
    plans, _ = exporter.plan_take(data, WIDE, [c["id"] for c in data["cameras"]])
    plan = next(p for p in plans if p.pre > 0 and p.file["probe"].get("audio"))
    opts = exporter.normalize_options({}, config.DEFAULT_EXPORT_OPTIONS)
    for hw in (False, True):
        args = [str(a) for a in exporter.build_command("src.mov", "out.mp4", plan, exporter.target_fraction(data), 100, opts, hw)]
        assert args.count("-i") == 2 and args[args.index("-i", args.index("-i") + 1) - 1] == "-vn"
        assert "1:a:0" in args and "0:a:0" not in args
