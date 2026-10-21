"""Cut Takes previews: FreeMoCap's MPEG-4 Part 2 annotated videos re-encoded for the browser."""
from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mocap_preprocess import config, server
from mocap_preprocess.app_state import jobs, store

ROOT = Path(__file__).resolve().parent / "_tmp" / "preview_sessions"
NAME = "previewfake"
CAMS = [("c0", "A"), ("c1", "B")]


def ffmpeg(*args):
    subprocess.run(["ffmpeg", "-y", "-v", "error", *map(str, args)], check=True)


def codecs(path: Path) -> list[str]:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


@pytest.fixture()
def session():
    if not shutil.which("ffmpeg"):
        pytest.skip("needs ffmpeg")
    prev = config.sessions_dir()
    shutil.rmtree(ROOT, ignore_errors=True)
    config.set_sessions_dir(ROOT)
    store.create(NAME)
    paths = store.paths(NAME)
    with store.edit(NAME) as d:
        d["cameras"] = [{"id": cid, "name": n, "rotation": 0, "include": True} for cid, n in CAMS]
        d["takes"] = [{"id": "t1", "name": "calibration", "start": 0.0, "end": 1.0, "export": True},
                      {"id": "t2", "name": "walk", "start": 5.0, "end": 6.0, "export": True},
                      {"id": "t3", "name": "idle", "start": 9.0, "end": 10.0, "export": True}]
        d["settings"]["proxy_long_side"] = 160
    for tid, nm in (("t1", "calibration"), ("t2", "walk"), ("t3", "idle")):
        vids = paths.output / nm / "synchronized_videos"
        vids.mkdir(parents=True)
        for _, cam in CAMS:   # exported takes: H.264 with audio
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=1", "-f", "lavfi", "-i", "sine=duration=1",
                   "-c:v", "libx264", "-c:a", "aac", "-shortest", vids / f"{cam}_synced.mp4")
        (paths.output / nm / "take_info.json").write_text(json.dumps({
            "take": nm, "id": tid, "start": 0.0, "end": 1.0, "layout": "freemocap", "exported_at": time.time(),
            "cameras": [{"camera_id": cid, "name": cam, "output": f"synchronized_videos/{cam}_synced.mp4"} for cid, cam in CAMS]}))
    # what FreeMoCap leaves: board videos for the calibration take, skeleton videos for a processed one
    for nm, sub, suffix in (("calibration", "charuco_annotated_videos", "charuco"), ("walk", "annotated_videos", "mediapipe")):
        d = paths.output / nm / sub
        d.mkdir()
        for _, cam in CAMS:
            ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=1", "-c:v", "mpeg4", d / f"{cam}_synced_{suffix}.mp4")
    yield TestClient(server.app), paths
    config.set_sessions_dir(prev)


def wait_build(client):
    r = client.post(f"/api/sessions/{NAME}/previews/build")
    assert r.status_code == 200, r.text
    job = jobs.get(r.json()["job"]["id"])
    assert job.done_event.wait(60), "preview build didn't finish"
    assert job.status == "done", job.info(log_since=0)
    return job


def test_previews_listed_built_and_served(session):
    client, paths = session
    v = client.get(f"/api/sessions/{NAME}/previews").json()
    kinds = {t["take_id"]: t["kind"] for t in v["takes"]}
    assert kinds == {"t1": "calibration", "t2": "mocap"}          # t3: FreeMoCap never touched it
    assert v["missing"] == 4 and v["building"] is False
    assert all(c["url"] is None for t in v["takes"] for c in t["cameras"].values())

    wait_build(client)
    v = client.get(f"/api/sessions/{NAME}/previews").json()
    assert v["missing"] == 0
    url = next(t for t in v["takes"] if t["take_id"] == "t2")["cameras"]["c1"]["url"]
    assert url.startswith(f"/media/{NAME}/preview/t2/c1.mp4?v=")
    out = paths.pre / "previews" / "t2" / "c1.mp4"
    assert codecs(out) == ["h264", "aac"]                           # playable, with the take's audio
    r = client.get(url, headers={"Range": "bytes=0-99"})
    assert r.status_code in (200, 206) and r.content[:100] == out.read_bytes()[:100]
    assert client.get(f"/media/{NAME}/preview/t2/..%2Fx.mp4").status_code == 404

    # a re-run calibration rewrites the board videos: that preview is stale again
    time.sleep(0.05)
    src = paths.output / "calibration" / "charuco_annotated_videos" / "A_synced_charuco.mp4"
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=1", "-c:v", "mpeg4", "-q:v", "9", src)
    v = client.get(f"/api/sessions/{NAME}/previews").json()
    assert v["missing"] == 1 and next(t for t in v["takes"] if t["take_id"] == "t1")["cameras"]["c0"]["url"] is None


def test_unreadable_source_is_not_retried(session):
    client, paths = session
    # a cancelled calibration leaves its board video without the MP4 index
    src = paths.output / "calibration" / "charuco_annotated_videos" / "B_synced_charuco.mp4"
    src.write_bytes(src.read_bytes()[:-2000][:4000])
    wait_build(client)
    v = client.get(f"/api/sessions/{NAME}/previews").json()
    cam = next(t for t in v["takes"] if t["take_id"] == "t1")["cameras"]["c1"]
    assert cam["broken"] is True and cam["url"] is None and v["missing"] == 0
    assert "0 preview(s) made" in wait_build(client).message      # not tried again until it changes


def test_mocap_jobs_queue_a_build_when_they_end(session):
    client, _ = session
    from mocap_preprocess import routes_previews
    done = jobs.start("mocap_process", NAME, lambda job: None, label="fake mocap")
    routes_previews.after_mocap_job(NAME, done)
    deadline = time.time() + 30
    while time.time() < deadline and not [j for j in jobs._jobs.values() if j.kind == "previews" and j.session == NAME]:
        time.sleep(0.05)
    built = [j for j in jobs._jobs.values() if j.kind == "previews" and j.session == NAME]
    assert built, "no preview build after the mocap job"
    assert built[-1].done_event.wait(60) and built[-1].status == "done"


def png_size(data: bytes) -> tuple[int, int]:
    assert data[1:4] == b"PNG"
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def test_full_quality_frames(session):
    client, paths = session
    # FreeMoCap's own video, full size (the preview is the proxy-sized copy)
    r = client.get(f"/api/sessions/{NAME}/previews/t2/c1/frame?t=0.5")
    assert r.status_code == 200 and r.headers["content-type"] == "image/png"
    assert png_size(r.content) == (320, 240)
    assert client.get(f"/api/sessions/{NAME}/previews/t3/c1/frame?t=0.5").status_code == 404   # never processed
    # a source file, turned the way the export turns it; a time past its end gives its last frame
    ffmpeg("-f", "lavfi", "-i", "testsrc=size=320x240:rate=30:duration=1", "-c:v", "libx264", paths.source / "clip.mp4")
    with store.edit(NAME) as d:
        d["cameras"][0]["rotation"] = 90
        d["files"] = [{"id": "f_0123456789", "rel": "clip.mp4", "camera": "c0", "probe": {"duration": 1.0}}]
    r = client.get(f"/api/sessions/{NAME}/files/f_0123456789/frame?t=5")
    assert r.status_code == 200 and png_size(r.content) == (240, 320)
    assert client.get(f"/api/sessions/{NAME}/files/f_nope/frame?t=0").status_code == 404
