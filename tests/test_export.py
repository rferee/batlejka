"""Export checks (§9.5): equal frame counts, target fps, rotation, visual
alignment of the burned-in flashes across cameras, and padding."""
from __future__ import annotations

import json
import subprocess
import threading
import time

import numpy as np
import pytest

from conftest import files_by_rel
from mocap_preprocess import config, exporter, ffmpeg_tools as ff, takes
from mocap_preprocess.jobs import Job

FPS = 30000 / 1001
WIDE = {"id": "t9", "name": "wide take", "start": 3.0, "end": 19.5, "export": True}


def luminance(path) -> np.ndarray:
    r = subprocess.run([config.ffmpeg_path(), "-v", "error", "-i", str(path), "-vf", "scale=16:16,format=gray",
                        "-f", "rawvideo", "-"], capture_output=True, check=True)
    return np.frombuffer(r.stdout, np.uint8).reshape(-1, 256).mean(axis=1)


def flash_starts(lum: np.ndarray) -> list[int]:
    hot = lum > 200
    return [int(i) for i in np.where(hot & ~np.r_[False, hot[:-1]])[0]]


@pytest.fixture(scope="module")
def exported(synced):
    store, name, gt = synced
    data = store.load(name)
    snapshot = store.load(name)
    by_rel = files_by_rel(data)
    fuji_cam = by_rel["DSCF0001.MOV"]["camera"]
    with store.edit(name) as d:
        d["takes"] = takes.detect(d) + [WIDE]
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
            info = ff.video_stream_info(v)
            assert info["r_frame_rate"] == "30000/1001", v
        assert json.loads((d / "take_info.json").read_text())["frames"] == n


def test_rotation_applied(exported):
    store, name, _, data, _, _, fuji_cam = exported
    cams = {c["id"]: c["name"] for c in data["cameras"]}
    for _, d in take_dirs(store, name, data):
        fuji = ff.video_stream_info(d / "synchronized_videos" / f"{cams[fuji_cam]}_synced.mp4")
        assert (fuji["width"], fuji["height"]) == (360, 640)   # 640×360 display, rotated 90°
        other = next(c for c in cams if c != fuji_cam)
        phone = ff.video_stream_info(d / "synchronized_videos" / f"{cams[other]}_synced.mp4")
        assert (phone["width"], phone["height"]) == (360, 640)  # untouched portrait


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
    info = ff.video_stream_info(vids[0])
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
    try:
        r = subprocess.run([config.ffmpeg_path(), "-v", "error", "-f", "lavfi", "-i", "testsrc2=s=256x144:d=0.2",
                            "-c:v", "h264_nvenc", "-f", "null", "-"], capture_output=True, timeout=30)
        hw = subprocess.run([config.ffmpeg_path(), "-hide_banner", "-hwaccels"], capture_output=True, text=True, timeout=30)
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
                info = ff.video_stream_info(v)
                assert (info["width"], info["height"]) == (360, 640), v   # rotation, incl. the Fuji's metadata
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
