"""The skeleton endpoint behind the Mocap take viewer: downsampling, NaN → null, 404s."""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from mocap_preprocess import config, server
from mocap_preprocess.app_state import store

ROOT = Path(__file__).resolve().parent / "_tmp" / "skeleton_sessions"
NAME = "skelfake"


def _take(out: Path, folder: str, tid: str, fps: str, frames: int | None, hands: bool = False):
    d = out / folder
    d.mkdir(parents=True)
    (d / "take_info.json").write_text(json.dumps({"id": tid, "take": folder, "fps": fps, "frames": frames or 0}))
    if frames:
        body = np.arange(frames * 33 * 3, dtype=np.float64).reshape(frames, 33, 3) + 0.04
        body[1, 5, :] = np.nan
        (d / "output_data").mkdir()
        np.save(d / "output_data" / "mediapipe_body_3d_xyz.npy", body)
        if hands:   # FreeMoCap writes the hands as their own 21-point arrays
            for side, base in (("left", 1000.0), ("right", 2000.0)):
                hand = np.arange(frames * 21 * 3, dtype=np.float64).reshape(frames, 21, 3) + base
                np.save(d / "output_data" / f"mediapipe_{side}_hand_3d_xyz.npy", hand)
    return d


@pytest.fixture()
def client():
    prev = config.sessions_dir()
    shutil.rmtree(ROOT, ignore_errors=True)
    config.set_sessions_dir(ROOT)
    out = store.create(NAME).output
    _take(out, "walk", "t1", "60000/1001", 10)
    _take(out, "jump", "t2", "30000/1001", None)          # exported, not processed
    _take(out, "wave", "t3", "30000/1001", 4, hands=True)
    try:
        yield TestClient(server.app)
    finally:
        config.set_sessions_dir(prev)
        shutil.rmtree(ROOT, ignore_errors=True)


def test_downsampled_to_max_fps(client):
    r = client.get(f"/api/sessions/{NAME}/mocap/takes/t1/skeleton")
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["step"] == 2 and d["frames"] == 5               # 59.94 fps → every 2nd frame
    assert d["fps"] == pytest.approx(60000 / 1001 / 2)
    assert d["landmarks"] == 33 and d["units"] == "mm" and d["parts"] == {"body": [0, 33]}
    assert len(d["data"]) == 5 and all(len(row) == 99 for row in d["data"])
    assert d["data"][1][0] == pytest.approx(2 * 99 + 0.0, abs=0.05)   # frame 2 (frame 1 skipped), rounded to 0.1
    assert [0, 1, "face"] in d["connections"] and len(d["connections"]) == 35
    assert [15, 17, "hand_l"] in d["connections"]            # no hand arrays: the body's own hand points are drawn
    assert {c[2] for c in d["connections"]} == {"face", "torso", "arm_l", "arm_r", "hand_l", "hand_r", "leg_l", "leg_r"}

    full = client.get(f"/api/sessions/{NAME}/mocap/takes/t1/skeleton?max_fps=60").json()
    assert full["step"] == 1 and full["frames"] == 10


def test_nan_becomes_null(client):
    d = client.get(f"/api/sessions/{NAME}/mocap/takes/t1/skeleton?max_fps=120").json()
    row = d["data"][1]
    assert row[15:18] == [None, None, None]                  # landmark 5 of frame 1
    assert all(v is not None for v in row[:15])


def test_hands_follow_the_body_when_freemocap_wrote_them(client):
    """The 21-point hands are appended after the 33 body points (left, then right), with
    their own bones and a link from each body wrist, in place of the body's three stub
    hand points."""
    d = client.get(f"/api/sessions/{NAME}/mocap/takes/t3/skeleton?max_fps=120").json()
    assert d["landmarks"] == 75 and d["parts"] == {"body": [0, 33], "left_hand": [33, 54], "right_hand": [54, 75]}
    assert d["frames"] == 4 and all(len(row) == 225 for row in d["data"])
    assert d["data"][0][99] == pytest.approx(1000.0, abs=0.05) and d["data"][0][54 * 3] == pytest.approx(2000.0, abs=0.05)
    assert d["data"][1][99] == pytest.approx(1000.0 + 21 * 3, abs=0.05)
    conns = d["connections"]
    assert [15, 33, "hand_l"] in conns and [16, 54, "hand_r"] in conns       # body wrist → hand wrist
    assert [33, 34, "hand_l"] in conns and [54 + 0, 54 + 17, "hand_r"] in conns
    assert not any(c[:2] in ([15, 17], [16, 18], [17, 19]) for c in conns)  # stub hand bones gone
    assert sum(1 for c in conns if c[2] == "hand_l") == 22                  # 21 hand bones + the wrist link


def test_missing_take_or_output_is_404(client):
    r = client.get(f"/api/sessions/{NAME}/mocap/takes/t2/skeleton")
    assert r.status_code == 404 and "processed" in r.json()["detail"]
    r = client.get(f"/api/sessions/{NAME}/mocap/takes/nope/skeleton")
    assert r.status_code == 404 and "exported" in r.json()["detail"]
