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


def _take(out: Path, folder: str, tid: str, fps: str, frames: int | None):
    d = out / folder
    d.mkdir(parents=True)
    (d / "take_info.json").write_text(json.dumps({"id": tid, "take": folder, "fps": fps, "frames": frames or 0}))
    if frames:
        body = np.arange(frames * 33 * 3, dtype=np.float64).reshape(frames, 33, 3) + 0.04
        body[1, 5, :] = np.nan
        (d / "output_data").mkdir()
        np.save(d / "output_data" / "mediapipe_body_3d_xyz.npy", body)
    return d


@pytest.fixture()
def client():
    prev = config.sessions_dir()
    shutil.rmtree(ROOT, ignore_errors=True)
    config.set_sessions_dir(ROOT)
    out = store.create(NAME).output
    _take(out, "walk", "t1", "60000/1001", 10)
    _take(out, "jump", "t2", "30000/1001", None)          # exported, not processed
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
    assert d["landmarks"] == 33 and d["units"] == "mm"
    assert len(d["data"]) == 5 and all(len(row) == 99 for row in d["data"])
    assert d["data"][1][0] == pytest.approx(2 * 99 + 0.0, abs=0.05)   # frame 2 (frame 1 skipped), rounded to 0.1
    assert [0, 1] in d["connections"] and len(d["connections"]) == 35

    full = client.get(f"/api/sessions/{NAME}/mocap/takes/t1/skeleton?max_fps=60").json()
    assert full["step"] == 1 and full["frames"] == 10


def test_nan_becomes_null(client):
    d = client.get(f"/api/sessions/{NAME}/mocap/takes/t1/skeleton?max_fps=120").json()
    row = d["data"][1]
    assert row[15:18] == [None, None, None]                  # landmark 5 of frame 1
    assert all(v is not None for v in row[:15])


def test_missing_take_or_output_is_404(client):
    r = client.get(f"/api/sessions/{NAME}/mocap/takes/t2/skeleton")
    assert r.status_code == 404 and "processed" in r.json()["detail"]
    r = client.get(f"/api/sessions/{NAME}/mocap/takes/nope/skeleton")
    assert r.status_code == 404 and "exported" in r.json()["detail"]
