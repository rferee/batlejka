"""Mocap API (FreeMoCap calibration profiles + processing) against a fake runner: listing,
camera matching, profiles, leftovers, mocap_info.json, stale detection, failures,
migration of pre-profile sessions. No FreeMoCap needed."""
from __future__ import annotations

import json
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from conftest import wait_job
from mocap_preprocess import config, routes_mocap, server
from mocap_preprocess.app_state import store
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
        self.ncal = 0            # calibrations run (written into each TOML, to tell them apart)
        self.params = None       # the last --params file's contents
        self.fail_calibration = False
        self.adjusted = []       # what the headless script reports as rescaled per take

    def __call__(self, cmd, args=(), job=None, on_event=None, on_tqdm=None, log_text=True):
        args = [str(a) for a in args]
        self.calls.append((cmd, args))
        ev = on_event or (lambda e: None)
        bar = on_tqdm or (lambda b: None)
        if cmd == "calibrate":
            rec = Path(args[0])
            ev({"type": "stage", "scope": "calibration", "take": rec.name, "stage": "detecting_board"})
            if self.fail_calibration:
                return {"ok": False, "error": "board not found in any camera", "returncode": 2, "log": "fake.log"}
            for c in CAMS:
                for n in (10, 50, 100):
                    bar({"name": f"{c}.mp4", "pct": n, "n": n, "total": 100})
            self.ncal += 1
            toml = rec / f"{rec.name}_camera_calibration.toml"
            toml.write_text(toml_text(CAMS).replace("error = 0.0", f"error = {self.ncal}.0"))
            info = {"toml": str(toml), "cameras": CAMS, "reprojection_error_px": 0.42, "board": "7x5"}
            ev({"type": "calibration_done", **info})
            return {"ok": True, **info, "returncode": 0, "log": "fake.log"}
        assert cmd == "process"
        i = args.index("--toml")
        take_dirs = [Path(a) for a in args[:i]]
        self.params = json.loads(Path(args[args.index("--params") + 1]).read_text()) if "--params" in args else None
        applied = {**mocap.FMC_DEFAULTS, **(self.params or {})}
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
                "subset": len(cams) < 3, "toml": str(d / f"{d.name}_camera_calibration.toml"), "seconds": 1.5,
                "params": applied, "adjusted": list(self.adjusted)})
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
    # no background preview builds here: they'd outlive the test and read the next one's session
    monkeypatch.setattr(routes_mocap, "after_mocap_job", lambda name, job: None)
    monkeypatch.setattr(env, "ready", lambda: True)
    yield TestClient(server.app), fr, paths
    config.set_sessions_dir(prev)


def wait(client, job):
    wait_job(job["id"], 10)
    return client.get(f"/api/jobs/{job['id']}?log_since=0&live=1").json()


def calibrate(client):
    """The one-shot form: a new profile (7x5-39mm), calibrated now."""
    r = client.post(f"/api/sessions/{NAME}/mocap/calibrate",
                    json={"take_id": "t1", "board": "7x5", "square_mm": 39, "groundplane": False})
    assert r.status_code == 200, r.text
    return wait(client, r.json())


def calibrate_named(client, name, status="done", **kw):
    """The New profile form under a given name; returns the profile's id once the job ended."""
    r = client.post(f"/api/sessions/{NAME}/mocap/calibrate",
                    json={"name": name, "take_id": "t1", "board": "7x5", "square_mm": 39, **kw})
    assert r.status_code == 200, r.text
    j = wait(client, r.json())
    assert j["status"] == status, j
    v = client.get(f"/api/sessions/{NAME}/mocap").json()
    return next(p["id"] for p in v["profiles"] if p["name"] == name)


def profile(view, pid):
    return next(p for p in view["profiles"] if p["id"] == pid)


def by_id(view):
    return {t["take_id"]: t for t in view["takes"]}


def test_listing_before_calibration(fake):
    client, _, _ = fake
    v = client.get(f"/api/sessions/{NAME}/mocap").json()
    assert v["freemocap"]["ready"] is True
    assert v["suggested_calibration_take"] == "t1"
    assert v["profiles"] == [] and v["active_profile"] is None
    t = by_id(v)
    assert set(t) == {"t1", "t2", "t3", "t4"}
    assert t["t3"]["leftovers"] == ["D_synced.mp4"] and t["t3"]["missing"] == []
    assert t["t2"]["cameras"] == ["A_synced", "B_synced"] and t["t2"]["camera_names"] == ["A", "B"]
    assert all(x["status"] == "not_processed" and x["calib_match"] is None for x in t.values())
    assert 1 <= v["default_processes"] <= 3


def test_new_profile_form_calibrates_under_its_name(fake):
    client, _, _ = fake
    body = {"name": "floor 5x3", "take_id": "t1", "board": "5x3", "square_mm": 58, "groundplane": True}
    r = client.post(f"/api/sessions/{NAME}/mocap/calibrate", json=body)
    assert r.status_code == 200, r.text
    assert wait(client, r.json())["status"] == "done"
    v = client.get(f"/api/sessions/{NAME}/mocap").json()
    assert [(p["name"], p["status"], p["board"]) for p in v["profiles"]] == [("floor-5x3", "calibrated", "5x3")]
    r = client.post(f"/api/sessions/{NAME}/mocap/calibrate", json=body)           # that name is taken
    assert r.status_code == 400 and "already exists" in r.text
    r = client.post(f"/api/sessions/{NAME}/mocap/calibrate", json={**body, "name": "walk", "take_id": "t3"})
    assert r.status_code == 409                                                   # leftovers: nothing kept
    assert len(client.get(f"/api/sessions/{NAME}/mocap").json()["profiles"]) == 1


def test_take_folder_without_take_info_still_listed(fake):
    client, _, paths = fake
    (paths.output / "calibration" / "take_info.json").unlink()     # deleted by hand or another tool
    v = client.get(f"/api/sessions/{NAME}/mocap").json()
    t = by_id(v)["t1"]
    assert t["name"] == "calibration" and t["dir"] == "calibration"
    assert t["cameras"] == CAMS and t["camera_names"] == ["A", "B", "C"] and t["missing"] == []
    assert v["suggested_calibration_take"] == "t1"
    assert calibrate(client)["status"] == "done"
    # another folder with a real take_info for the same take wins, even when it's older
    shutil.copytree(paths.output / "calibration", paths.output / "calibration_2")
    (paths.output / "calibration_2" / "take_info.json").write_text(json.dumps(
        {"take": "calibration", "id": "t1", "start": 0.0, "end": 5.0, "layout": "freemocap", "exported_at": 1.0,
         "cameras": [{"name": n, "output": f"synchronized_videos/{n}_synced.mp4"} for n in "ABC"]}))
    assert by_id(client.get(f"/api/sessions/{NAME}/mocap").json())["t1"]["dir"] == "calibration_2"


def test_calibrate_stores_calibration_and_matches_cameras(fake):
    client, fr, paths = fake
    j = calibrate(client)
    assert j["status"] == "done", j
    cmd, args = fr.calls[-1]
    assert cmd == "calibrate" and args[0] == str(paths.output / "calibration")
    assert args[1:] == ["--square-mm", "39", "--board", "7x5"]
    v = client.get(f"/api/sessions/{NAME}/mocap").json()
    p = v["profiles"][0]
    assert p["name"] == "7x5-39mm" and p["status"] == "calibrated" and p["active"] and v["active_profile"] == p["id"]
    assert p["toml"] == "output/calibrations/7x5-39mm.toml" and (paths.root / p["toml"]).is_file()
    assert p["reprojection_error_px"] == 0.42 and p["cameras"] == CAMS and p["square_mm"] == 39.0
    assert p["take_id"] == "t1" and p["freemocap"] == config.FMC_VERSION and p["created_at"]
    assert not p["settings_changed"] and p["used_by"] == 3      # every take but the calibration one
    assert "calibration" not in store.load(NAME)["mocap"]        # no mirror of the active profile any more
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
    assert client.get(url).json()["profiles"][0]["name"] == "7x5-39mm"   # failed one-shot forms left nothing behind
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
    assert args[args.index("--toml") + 1] == str((paths.root / "output/calibrations/7x5-39mm.toml").resolve())
    info = json.loads((paths.output / "jump" / "mocap_info.json").read_text())
    assert info["subset"] is True and info["cameras"] == ["A_synced", "B_synced"]
    assert info["freemocap"] == config.FMC_VERSION and info["calibration"]["reprojection_error_px"] == 0.42
    assert info["calibration"]["profile"] == "p1" and info["calibration"]["profile_name"] == "7x5-39mm"
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

    # reprocess → fresh; the profile's calibration replaced (sessions from before the lock) → stale again
    wait(client, client.post(f"/api/sessions/{NAME}/mocap/process", json={"take_ids": ["t2"]}).json())
    assert by_id(client.get(f"/api/sessions/{NAME}/mocap").json())["t2"]["status"] == "done"
    with store.edit(NAME) as data:
        data["mocap"]["profiles"][0]["created_at"] += 1
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
    assert r.json()["options"] == {"processes": 3, "blender": True, "blender_exe": "C:/b/blender.exe", "params": {}}
    r = client.put(f"/api/sessions/{NAME}/mocap/options", json={"processes": None})
    assert r.json()["options"]["processes"] is None
    assert store.load(NAME)["mocap"]["options"]["blender"] is True


def test_read_toml_cameras_sorted(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(toml_text(["x", "y"]).replace("[cam_0]", "[cam_9]").replace("[cam_1]", "[cam_0]"))
    assert mocap.read_toml_cameras(p) == ["y", "x"]   # aniposelib loads sections in sorted key order


# --------------------------------------------------------------------------- profiles

def test_failed_calibration_stays_and_runs_again(fake):
    """A calibration that fails leaves the profile (status failed) so it can be run again;
    until it succeeds it can't be made active or picked for a take. The form is the one way
    to make a profile: the old draft / edit / duplicate routes are gone."""
    client, fr, paths = fake
    url = f"/api/sessions/{NAME}/mocap"
    fr.fail_calibration = True
    r = client.post(f"{url}/calibrate", json={"name": "floor 5x3!", "take_id": "t1", "board": "5x3", "square_mm": 58, "groundplane": True})
    assert r.status_code == 200, r.text
    assert wait(client, r.json())["status"] == "error"
    v = client.get(url).json()
    p = v["profiles"][0]
    pid = p["id"]
    assert p["name"] == "floor-5x3" and p["status"] == "failed" and not p["calibrated"] and not p["active"]
    assert "board not found" in (p["error"] or "") and v["active_profile"] is None
    assert client.post(f"{url}/profiles/{pid}/activate").status_code == 400
    assert client.put(f"{url}/takes/t2/profile", json={"profile": pid}).status_code == 400
    assert client.post(f"{url}/profiles", json={"name": "x", "square_mm": 30}).status_code in (404, 405)
    assert client.put(f"{url}/profiles/{pid}", json={"name": "y"}).status_code in (404, 405)
    assert client.post(f"{url}/profiles/{pid}/duplicate").status_code in (404, 405)

    fr.fail_calibration = False
    j = wait(client, client.post(f"{url}/profiles/{pid}/calibrate", json={}).json())
    assert j["status"] == "done", j
    v = client.get(url).json()
    p = profile(v, pid)
    assert p["status"] == "calibrated" and p["active"] and p["error"] is None   # the first calibrated profile becomes active
    assert fr.calls[-1][1][1:] == ["--square-mm", "58", "--board", "5x3", "--groundplane"]
    assert (paths.output / "calibrations" / "floor-5x3.toml").is_file()
    assert client.post(f"{url}/profiles/{pid}/calibrate", json={}).status_code == 409   # calibrated: duplicate it instead
    assert client.delete(f"{url}/profiles/{pid}").status_code == 400                     # the active one stays
    assert by_id(v)["t1"]["is_calibration"]                                              # its take isn't listed for processing


def test_takes_use_their_own_profile(fake):
    client, fr, paths = fake
    (paths.output / "walk" / "synchronized_videos" / "D_synced.mp4").unlink()
    a = calibrate_named(client, "a")
    b = calibrate_named(client, "b", square_mm=40)
    url = f"/api/sessions/{NAME}/mocap"
    v = client.get(url).json()
    assert v["active_profile"] == a and by_id(v)["t2"]["profile"] == a and not by_id(v)["t2"]["profile_explicit"]
    v = client.put(f"{url}/takes/t2/profile", json={"profile": b}).json()
    assert by_id(v)["t2"]["profile"] == b and by_id(v)["t2"]["profile_explicit"]
    assert profile(v, b)["used_by"] == 1 and profile(v, a)["used_by"] == 2

    n = len(fr.calls)
    j = wait(client, client.post(f"{url}/process", json={"take_ids": ["t2", "t3"]}).json())
    assert j["status"] == "done", j
    runs = [c for c in fr.calls[n:] if c[0] == "process"]
    tomls = {Path(c[1][0]).name: Path(c[1][c[1].index("--toml") + 1]).name for c in runs}
    assert tomls == {"jump": "b.toml", "walk": "a.toml"}           # one FreeMoCap run per profile
    info = json.loads((paths.output / "jump" / "mocap_info.json").read_text())
    assert info["calibration"]["profile"] == b and info["calibration"]["profile_name"] == "b"
    rec = store.load(NAME)["mocap"]["takes"]["t2"]
    assert rec["profile"] == b and rec["status"] == "done"

    # picking another calibration makes the take stale; back to the active one → follows it
    v = client.put(f"{url}/takes/t2/profile", json={"profile": None}).json()
    t = by_id(v)["t2"]
    assert t["profile"] == a and t["status"] == "stale" and t["stale_reason"] == "other calibration selected"
    # a profile that isn't calibrated can't be picked; deleting a picked profile falls back to the active one
    fr.fail_calibration = True
    c = calibrate_named(client, "c", status="error")
    fr.fail_calibration = False
    assert client.put(f"{url}/takes/t2/profile", json={"profile": c}).status_code == 400
    client.put(f"{url}/takes/t2/profile", json={"profile": b})
    v = client.delete(f"{url}/profiles/{b}").json()
    assert by_id(v)["t2"]["profile"] == a and not (paths.output / "calibrations" / "b.toml").exists()


def test_legacy_calibration_migrates(fake):
    client, fr, paths = fake
    # a session from before profiles: one calibration, its TOML in the take folder
    legacy_toml = paths.output / "calibration" / "calibration_camera_calibration.toml"
    legacy_toml.write_text(toml_text(CAMS).replace("error = 0.0", "error = 99.0"))
    t0 = time.time() - 100
    raw = json.loads(paths.json.read_text())
    raw["mocap"] = {"calibration": {"take_id": "t1", "toml": "output/calibration/calibration_camera_calibration.toml",
                                    "board": "5x3", "square_mm": 58.0, "groundplane": True, "reprojection_error_px": 1.77,
                                    "cameras": CAMS, "created_at": t0, "freemocap": "v1.8.2"},
                    "options": {"processes": None, "blender": False, "blender_exe": None},
                    "takes": {"t2": {"status": "done", "processed_at": t0 + 10, "error": None, "subset": True,
                                     "cameras": ["A_synced", "B_synced"], "seconds": 2.0}}}
    paths.json.write_text(json.dumps(raw))
    jump = paths.output / "jump"
    (jump / "output_data").mkdir()
    (jump / "output_data" / "jump_body_3d_xyz.npy").write_bytes(b"x")
    (jump / "mocap_info.json").write_text(json.dumps({
        "calibration": {"toml": "output/calibration/calibration_camera_calibration.toml", "created_at": t0},
        "videos": mocap.snapshot_videos(jump)}))

    v = client.get(f"/api/sessions/{NAME}/mocap").json()
    p = v["profiles"][0]
    assert p["id"] == "p1" and p["name"] == "5x3-58mm" and p["status"] == "calibrated" and p["active"]
    assert p["toml"] == "output/calibration/calibration_camera_calibration.toml"
    assert by_id(v)["t2"]["status"] == "done"            # the old result still counts
    assert by_id(v)["t2"]["calib_match"]["n"] == 2

    # a new profile on the same take: FreeMoCap overwrites the take's TOML, so p1 gets its own copy first
    calibrate_named(client, "second")
    v = client.get(f"/api/sessions/{NAME}/mocap").json()
    p1 = profile(v, "p1")
    assert p1["toml"] == "output/calibrations/5x3-58mm.toml"
    assert "error = 99.0" in (paths.root / p1["toml"]).read_text()
    assert "error = 99.0" not in legacy_toml.read_text()
    assert by_id(v)["t2"]["status"] == "done"            # still the same calibration (created_at unchanged)


def test_take_detail_and_blend(fake):
    import numpy as np

    client, fr, paths = fake
    calibrate(client)
    fr.adjusted = [{"camera": "B_synced", "calibrated": [2160, 3840], "video": [1080, 1920], "scale": [0.5, 0.5]}]
    j = wait(client, client.post(f"/api/sessions/{NAME}/mocap/process", json={"take_ids": ["t2"]}).json())
    assert j["status"] == "done", j
    info = json.loads((paths.output / "jump" / "mocap_info.json").read_text())
    assert info["calibration_adjustments"] == fr.adjusted         # kept with the run, shown in Details
    fr.adjusted = []
    body = np.zeros((10, 33, 3))
    body[:, 13] = [300.0, 0.0, 0.0]          # left elbow 300 mm from the left shoulder (at the origin)
    body[:, 25] = [0.0, 400.0, 0.0]          # left knee 400 mm from the left hip
    body[3, 0] = np.nan                      # one frame without the whole body
    np.save(paths.output / "jump" / "output_data" / "jump_body_3d_xyz.npy", body)
    d = client.get(f"/api/sessions/{NAME}/mocap/takes/t2/detail").json()
    assert d["info"]["calibration_adjustments"][0]["camera"] == "B_synced"
    q = d["quality"]
    assert q["frames"] == 10 and q["body_tracked_pct"] == 90.0
    assert q["upper_arm_mm"][0] == 300.0 and q["thigh_mm"][0] == 400.0
    assert d["out_dir"] == "output/jump/" and d["info"]["calibration"]["profile"] == "p1"
    assert any(f["name"].endswith("_body_3d_xyz.npy") for f in d["files"]) and d["out_size"] > 0
    assert d["blend"] is None
    assert client.post(f"/api/sessions/{NAME}/mocap/takes/t2/open_blend").status_code == 404
    # a take that wasn't processed has no quality, and no files yet
    d = client.get(f"/api/sessions/{NAME}/mocap/takes/t3/detail").json()
    assert d["quality"] is None and d["files"] == [] and d["info"] is None


# --------------------------------------------------------------------------- processing settings

def test_params_normalize():
    d = {**mocap.FMC_DEFAULTS, "framerate": 59.94}
    p = mocap.normalize_params({"min_detection_confidence": "0.7", "order": 4, "framerate": 59.94,
                                "minimum_cameras_for_triangulation": 99, "target_reprojection_error": -1,
                                "mediapipe_model_complexity": "1", "yolo_model_size": "high_res",
                                "static_image_mode": 1, "cutoff_frequency": None}, d)
    assert p == {"min_detection_confidence": 0.7, "minimum_cameras_for_triangulation": 32,
                 "target_reprojection_error": 0.0001, "mediapipe_model_complexity": 1,
                 "yolo_model_size": "high_res", "static_image_mode": True}   # defaults and nulls dropped, clamped
    for bad in ({"nope": 1}, {"yolo_model_size": "xlarge"}, {"mediapipe_model_complexity": 3},
                {"min_detection_confidence": "x"}, {"framerate": float("nan")}):
        with pytest.raises(mocap.MocapError):
            mocap.normalize_params(bad, d)
    with pytest.raises(mocap.MocapError):
        mocap.check_params({**d, "cutoff_frequency": 30})          # at/above Nyquist (29.97 Hz)
    mocap.check_params({**d, "cutoff_frequency": 30, "run_butterworth_filter": False})
    # a run from before settings existed used FreeMoCap's 30 fps; 29.97 is the same filter, 59.94 isn't
    assert mocap.changed_params(None, {**mocap.FMC_DEFAULTS, "framerate": 29.97}) == []
    assert mocap.changed_params(None, d) == ["framerate"]


def test_headless_params_split():
    import importlib.util
    spec = importlib.util.spec_from_file_location("fmc_headless_params", runner.SCRIPT)
    fh = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fh)
    g = fh.split_params({"min_detection_confidence": 0.6, "framerate": 59.94, "order": 2, "run_3d_triangulation": False})
    assert g == {"tracking": {"min_detection_confidence": 0.6}, "triangulation": {"run_3d_triangulation": False},
                 "post": {"framerate": 59.94}, "filter": {"order": 2}}
    with pytest.raises(ValueError):
        fh.split_params({"use_triangulate_ransac_method": True})   # ignored by FreeMoCap 1.8.2: not offered
    # every setting the app stores has a home in fmc_headless.py
    assert set(mocap.FMC_DEFAULTS) == {k for keys in fh.PARAM_GROUPS.values() for k in keys}


def test_params_go_to_freemocap_and_stale(fake):
    client, fr, paths = fake
    calibrate(client)
    url = f"/api/sessions/{NAME}/mocap"
    v = client.get(url).json()
    assert v["param_defaults"]["framerate"] == 29.97 and v["params"] == v["param_defaults"]
    r = client.put(f"{url}/options", json={"params": {"min_detection_confidence": 0.6, "framerate": 29.97}})
    assert r.status_code == 200, r.text
    assert r.json()["options"]["params"] == {"min_detection_confidence": 0.6}    # the default fps isn't stored
    assert r.json()["params"]["min_detection_confidence"] == 0.6
    assert client.put(f"{url}/options", json={"params": {"bogus": 1}}).status_code == 400

    j = wait(client, client.post(f"{url}/process", json={"take_ids": ["t2"]}).json())
    assert j["status"] == "done", j
    assert fr.params["min_detection_confidence"] == 0.6 and fr.params["framerate"] == 29.97
    info = json.loads((paths.output / "jump" / "mocap_info.json").read_text())
    assert info["params"]["min_detection_confidence"] == 0.6 and info["params"]["order"] == 4
    assert by_id(client.get(url).json())["t2"]["status"] == "done"

    client.put(f"{url}/options", json={"params": {"min_detection_confidence": 0.6, "order": 2}})
    t = by_id(client.get(url).json())["t2"]
    assert t["status"] == "stale" and t["stale_reason"] == "processing settings changed"
    client.put(f"{url}/options", json={"params": {"min_detection_confidence": 0.6}})   # back → fresh again
    assert by_id(client.get(url).json())["t2"]["status"] == "done"
    client.put(f"{url}/options", json={"processes": 7})                                # not a FreeMoCap result setting
    assert by_id(client.get(url).json())["t2"]["status"] == "done"

    # a cutoff FreeMoCap can't filter with, and skipping a stage without earlier results, fail up front
    client.put(f"{url}/options", json={"params": {"cutoff_frequency": 20}})
    r = client.post(f"{url}/process", json={"take_ids": ["t2"]})
    assert r.status_code == 400 and "half the framerate" in r.text
    client.put(f"{url}/options", json={"params": {"run_image_tracking": False}})
    r = client.post(f"{url}/process", json={"take_ids": ["t2"]})
    assert r.status_code == 400 and "no 2D data" in r.text
