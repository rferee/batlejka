"""Mocap API (FreeMoCap calibration + processing) against a fake runner: listing,
camera matching, leftovers, mocap_info.json, stale detection, failures. No FreeMoCap needed."""
from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from mocap_preprocess import config, server
from mocap_preprocess.app_state import jobs, store
from mocap_preprocess.fmc import env, mocap, runner

ROOT = Path(__file__).resolve().parent / "_tmp" / "mocap_sessions"
NAME = "mocapfake"
CAMS = ["A_synced", "B_synced", "C_synced"]
TAKES = [  # id, name, cameras (video stems), extra files in synchronized_videos/
    ("t1", "calibration", CAMS, []),
    ("t2", "jump", ["A_synced", "B_synced"], []),
    ("t3", "walk", CAMS, ["D_synced.mp4"]),
    ("t4", "odd", ["A_synced", "E_synced"], []),
]


def toml_text(cams):
    parts = []
    for i, c in enumerate(cams):
        parts.append(f'[cam_{i}]\nname = "{c}"\nsize = [1920, 1080]\nmatrix = [[1.0, 0.0, 960.0], '
                     f'[0.0, 1.0, 540.0], [0.0, 0.0, 1.0]]\ndistortions = [0.0, 0.0, 0.0, 0.0, 0.0]\n'
                     f'rotation = [0.0, 0.0, 0.0]\ntranslation = [{i * 100.0}, 0.0, 0.0]\n')
    parts.append("[metadata]\nadjusted = false\nerror = 0.0\n")
    return "\n".join(parts)


class FakeRunner:
    """Stands in for runner.run_headless: emits events like fmc_headless.py and writes outputs."""

    def __init__(self):
        self.calls = []
        self.fail = set()      # take dir names that fail
        self.crash_after = None  # stop reporting after this many takes

    def __call__(self, cmd, args=(), job=None, on_event=None, on_tqdm=None, log_text=True):
        args = [str(a) for a in args]
        self.calls.append((cmd, args))
        ev = on_event or (lambda e: None)
        bar = on_tqdm or (lambda b: None)
        if cmd == "calibrate":
            rec = Path(args[0])
            ev({"type": "stage", "scope": "calibration", "take": rec.name, "stage": "detecting_board"})
            for c in CAMS:
                for n in (10, 50, 100):
                    bar({"name": f"{c}.mp4", "pct": n, "n": n, "total": 100})
            toml = rec / f"{rec.name}_camera_calibration.toml"
            toml.write_text(toml_text(CAMS))
            info = {"toml": str(toml), "cameras": CAMS, "reprojection_error_px": 0.42, "board": "7x5"}
            ev({"type": "calibration_done", **info})
            return {"ok": True, **info, "returncode": 0, "log": "fake.log"}
        assert cmd == "process"
        i = args.index("--toml")
        take_dirs = [Path(a) for a in args[:i]]
        failed = {}
        for k, d in enumerate(take_dirs):
            if self.crash_after is not None and k >= self.crash_after:
                return {"ok": False, "error": "FreeMoCap runner exited with code 3 without a result: boom",
                        "returncode": 3, "log": "fake.log"}
            ev({"type": "take_start", "take": d.name, "index": k, "total": len(take_dirs)})
            ev({"type": "stage", "scope": "take", "take": d.name, "stage": "tracking_2d"})
            cams = sorted(p.stem for p in (d / "synchronized_videos").glob("*.mp4"))
            for c in cams:
                bar({"name": f"{c}.mp4", "pct": 50, "n": 5, "total": 10})
            if d.name in self.fail:
                failed[d.name] = "ValueError('bad take')"
                ev({"type": "take_failed", "take": d.name, "error": failed[d.name]})
                continue
            for st in ("triangulating", "skeleton", "saving"):
                ev({"type": "stage", "scope": "take", "take": d.name, "stage": st})
            out = d / "output_data"
            out.mkdir(exist_ok=True)
            (out / f"{d.name}_body_3d_xyz.npy").write_bytes(b"x")
            ev({"type": "take_done", "take": d.name, "path": str(d), "cameras": cams,
                "subset": len(cams) < 3, "toml": str(d / f"{d.name}_camera_calibration.toml"), "seconds": 1.5})
        return {"ok": not failed, "failed": failed, "returncode": 0 if not failed else 1, "log": "fake.log"}


@pytest.fixture()
def fake(monkeypatch):
    prev = config.sessions_dir()
    shutil.rmtree(ROOT, ignore_errors=True)
    config.set_sessions_dir(ROOT)
    store.create(NAME)
    paths = store.paths(NAME)
    with store.edit(NAME) as data:
        data["takes"] = [{"id": tid, "name": nm, "start": 10.0 * k, "end": 10.0 * k + 5, "export": True}
                         for k, (tid, nm, _, _) in enumerate(TAKES)]
        data["cameras"] = [{"id": f"c{k}", "name": c[0], "rotation": 0, "include": True} for k, c in enumerate(CAMS)]
    for k, (tid, nm, cams, extra) in enumerate(TAKES):
        d = paths.output / nm / "synchronized_videos"
        d.mkdir(parents=True)
        for c in cams:
            (d / f"{c}.mp4").write_bytes(b"\0" * (100 + k))
        for e in extra:
            (d / e).write_bytes(b"old")
        info = {"take": nm, "id": tid, "start": 10.0 * k, "end": 10.0 * k + 5, "duration": 5.0, "frames": 150,
                "layout": "freemocap", "exported_at": time.time(),
                "cameras": [{"camera_id": f"c{j}", "name": c[0], "output": f"synchronized_videos/{c}.mp4",
                             "status": "written"} for j, c in enumerate(cams)]}
        (paths.output / nm / "take_info.json").write_text(json.dumps(info))
    fr = FakeRunner()
    monkeypatch.setattr(runner, "run_headless", fr)
    monkeypatch.setattr(env, "ready", lambda: True)
    yield TestClient(server.app), fr, paths
    config.set_sessions_dir(prev)


def wait(client, job):
    j = jobs.get(job["id"])
    assert j.done_event.wait(10), "job didn't finish"
    return client.get(f"/api/jobs/{job['id']}?log_since=0&live=1").json()


def calibrate(client):
    r = client.post(f"/api/sessions/{NAME}/mocap/calibrate",
                    json={"take_id": "t1", "board": "7x5", "square_mm": 39, "groundplane": False})
    assert r.status_code == 200, r.text
    return wait(client, r.json())


def by_id(view):
    return {t["take_id"]: t for t in view["takes"]}


def test_listing_before_calibration(fake):
    client, _, _ = fake
    v = client.get(f"/api/sessions/{NAME}/mocap").json()
    assert v["freemocap"]["ready"] is True
    assert v["suggested_calibration_take"] == "t1"
    assert v["calibration"] is None
    t = by_id(v)
    assert set(t) == {"t1", "t2", "t3", "t4"}
    assert t["t3"]["leftovers"] == ["D_synced.mp4"] and t["t3"]["missing"] == []
    assert t["t2"]["cameras"] == ["A_synced", "B_synced"] and t["t2"]["camera_names"] == ["A", "B"]
    assert all(x["status"] == "not_processed" and x["calib_match"] is None for x in t.values())
    assert 1 <= v["default_processes"] <= 3


def test_calibrate_stores_calibration_and_matches_cameras(fake):
    client, fr, paths = fake
    j = calibrate(client)
    assert j["status"] == "done", j
    cmd, args = fr.calls[-1]
    assert cmd == "calibrate" and args[0] == str(paths.output / "calibration")
    assert args[1:] == ["--square-mm", "39", "--board", "7x5"]
    v = client.get(f"/api/sessions/{NAME}/mocap").json()
    c = v["calibration"]
    assert c["toml"] == "output/calibration/calibration_camera_calibration.toml"
    assert c["reprojection_error_px"] == 0.42 and c["cameras"] == CAMS and c["square_mm"] == 39.0
    assert c["take_id"] == "t1" and c["freemocap"] == config.FMC_VERSION and c["created_at"]
    t = by_id(v)
    assert t["t1"]["is_calibration"]
    assert t["t2"]["calib_match"] == {"n": 2, "of": 3, "missing": ["C_synced"], "unknown": []}
    assert t["t4"]["calib_match"]["unknown"] == ["E_synced"]


def test_validation(fake):
    client, _, _ = fake
    url = f"/api/sessions/{NAME}/mocap"
    assert client.post(url + "/process", json={"take_ids": ["t2"]}).status_code == 400   # no calibration yet
    assert client.post(url + "/calibrate", json={"take_id": "t1", "board": "7x5"}).status_code == 400  # no square size
    assert client.post(url + "/calibrate", json={"take_id": "t1", "board": "9x9", "square_mm": 30}).status_code == 400
    assert client.post(url + "/calibrate", json={"take_id": "nope", "square_mm": 30}).status_code == 400
    calibrate(client)
    r = client.post(url + "/process", json={"take_ids": ["t1"]})
    assert r.status_code == 400 and "calibration take" in r.json()["detail"]
    r = client.post(url + "/process", json={"take_ids": ["t4"]})
    assert r.status_code == 400 and "E_synced" in r.json()["detail"]


def test_not_ready_is_409(fake, monkeypatch):
    client, _, _ = fake
    monkeypatch.setattr(env, "ready", lambda: False)
    r = client.post(f"/api/sessions/{NAME}/mocap/calibrate", json={"take_id": "t1", "square_mm": 30})
    assert r.status_code == 409


def test_leftovers_block_until_deleted(fake):
    client, fr, paths = fake
    calibrate(client)
    url = f"/api/sessions/{NAME}/mocap/process"
    r = client.post(url, json={"take_ids": ["t2", "t3"]})
    assert r.status_code == 409
    assert r.json()["detail"]["leftovers"] == {"t3": ["D_synced.mp4"]}
    assert (paths.output / "walk" / "synchronized_videos" / "D_synced.mp4").exists()
    r = client.post(url, json={"take_ids": ["t2", "t3"], "delete_leftovers": True})
    assert r.status_code == 200, r.text
    j = wait(client, r.json())
    assert j["status"] == "done", j
    assert not (paths.output / "walk" / "synchronized_videos" / "D_synced.mp4").exists()
    assert any("deleted leftover" in e["msg"] for e in j["log"])


def test_process_writes_mocap_info_and_goes_stale(fake):
    client, fr, paths = fake
    calibrate(client)
    r = client.post(f"/api/sessions/{NAME}/mocap/process", json={"take_ids": ["t2"]})
    j = wait(client, r.json())
    assert j["status"] == "done", j
    assert j["live"]["takes"]["t2"]["status"] == "done" and j["progress"] == 1.0
    cmd, args = fr.calls[-1]
    assert cmd == "process" and args[0] == str(paths.output / "jump")
    assert args[args.index("--toml") + 1] == str((paths.root / "output/calibration/calibration_camera_calibration.toml").resolve())
    info = json.loads((paths.output / "jump" / "mocap_info.json").read_text())
    assert info["subset"] is True and info["cameras"] == ["A_synced", "B_synced"]
    assert info["freemocap"] == config.FMC_VERSION and info["calibration"]["reprojection_error_px"] == 0.42
    assert set(info["videos"]) == {"A_synced.mp4", "B_synced.mp4"} and info["videos"]["A_synced.mp4"]["size"] == 101
    t = by_id(client.get(f"/api/sessions/{NAME}/mocap").json())["t2"]
    assert t["status"] == "done" and t["subset"] is True and t["processed_at"] and t["stale_reason"] is None
    sess = store.load(NAME)["mocap"]["takes"]["t2"]
    assert sess["status"] == "done" and sess["seconds"] == 1.5

    # re-export (a video changes) → stale
    vid = paths.output / "jump" / "synchronized_videos" / "A_synced.mp4"
    vid.write_bytes(b"\0" * 500)
    t = by_id(client.get(f"/api/sessions/{NAME}/mocap").json())["t2"]
    assert t["status"] == "stale" and t["stale_reason"] == "videos re-exported"

    # reprocess → fresh, then a new calibration → stale again
    wait(client, client.post(f"/api/sessions/{NAME}/mocap/process", json={"take_ids": ["t2"]}).json())
    assert by_id(client.get(f"/api/sessions/{NAME}/mocap").json())["t2"]["status"] == "done"
    time.sleep(0.01)
    calibrate(client)
    t = by_id(client.get(f"/api/sessions/{NAME}/mocap").json())["t2"]
    assert t["status"] == "stale" and t["stale_reason"] == "calibration changed"


def test_failed_and_unreported_takes(fake):
    client, fr, paths = fake
    calibrate(client)
    (paths.output / "walk" / "synchronized_videos" / "D_synced.mp4").unlink()
    fr.fail = {"jump"}
    j = wait(client, client.post(f"/api/sessions/{NAME}/mocap/process", json={"take_ids": ["t2", "t3"]}).json())
    assert j["status"] == "error" and "jump" in j["error"]
    t = by_id(client.get(f"/api/sessions/{NAME}/mocap").json())
    assert t["t2"]["status"] == "failed" and "bad take" in t["t2"]["error"]
    assert t["t3"]["status"] == "done"           # the other take still counts

    fr.fail = set()
    fr.crash_after = 0
    j = wait(client, client.post(f"/api/sessions/{NAME}/mocap/process", json={"take_ids": ["t2"]}).json())
    assert j["status"] == "error"
    t = by_id(client.get(f"/api/sessions/{NAME}/mocap").json())["t2"]
    assert t["status"] == "failed" and "without a result" in t["error"]


def test_options(fake):
    client, _, _ = fake
    r = client.put(f"/api/sessions/{NAME}/mocap/options", json={"processes": 3, "blender": True, "blender_exe": " C:/b/blender.exe "})
    assert r.json()["options"] == {"processes": 3, "blender": True, "blender_exe": "C:/b/blender.exe"}
    r = client.put(f"/api/sessions/{NAME}/mocap/options", json={"processes": None})
    assert r.json()["options"]["processes"] is None
    assert store.load(NAME)["mocap"]["options"]["blender"] is True


def test_read_toml_cameras_sorted(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(toml_text(["x", "y"]).replace("[cam_0]", "[cam_9]").replace("[cam_1]", "[cam_0]"))
    assert mocap.read_toml_cameras(p) == ["y", "x"]   # aniposelib loads sections in sorted key order
