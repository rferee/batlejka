"""Two-stage sync on the scn_* sessions: looping music, a camera minutes off that
stamps end times, no continuous camera, files without metadata."""
from __future__ import annotations

import pytest

from conftest import files_by_rel, sync_errors
from mocap_preprocess import syncing
from mocap_preprocess.jobs import Job

FRAME = 1001 / 30000
SCENARIOS = ["scn_loop", "scn_clock", "scn_nocont", "scn_nometa"]


def camera_of(data: dict, rel: str) -> str:
    return files_by_rel(data)[rel]["camera"]


@pytest.mark.parametrize("name", SCENARIOS)
def test_every_audio_clip_within_one_frame(scenario, name):
    store, name, gt = scenario(name)
    data = store.load(name)
    errs = sync_errors(data, gt)
    assert len(errs) == len(gt["files"])
    bad = {r: round(e * 1000, 1) for r, e in errs.items() if abs(e) > FRAME}
    assert not bad, f"off by more than a frame (ms): {bad}"
    by = files_by_rel(data)
    for rel, t in gt["files"].items():
        assert by[rel]["sync"]["method"] in ("reference", "audio", "search"), (rel, by[rel]["sync"])
        assert not by[rel]["sync"].get("flags"), (rel, by[rel]["sync"])


def test_loop_whole_file_search_picks_a_loop_copy(scenario):
    """The problem being fixed: against the continuous camera, a search over the
    whole file lands a take-1 clip on another repeat of the loop."""
    store, name, gt = scenario("scn_loop")
    paths = store.paths(name)
    data = store.load(name)
    by = files_by_rel(data)
    sr = data["settings"]["sync_sample_rate"]
    cont = by["C0001.MP4"]
    r = syncing.prepare(syncing.load_audio(paths, cont, sr))
    wrong = []
    for rel in ("IMG_10001.MOV", "SM_0001.mp4"):
        m = syncing.ncc_match(r, syncing.prepare(syncing.load_audio(paths, by[rel], sr)), sr)
        found = m.lag / sr + syncing.audio_start(cont) - syncing.audio_start(by[rel])
        wrong.append(abs(found - gt["files"][rel]["offset"]) > 1.0)
    assert any(wrong)
    # ...while the two-stage sync placed them by audio within their windows
    assert all(by[rel]["sync"]["method"] == "audio" and by[rel]["sync"]["via"] == "window"
               for rel in ("IMG_10001.MOV", "SM_0001.mp4"))


def test_clock_minutes_off_and_end_stamps_detected(scenario):
    store, name, gt = scenario("scn_clock")
    data = store.load(name)
    sync = data["sync"]
    gopro = sync["cameras"][camera_of(data, "GX010001.MP4")]
    assert gopro["mode"] == "end" and gopro["mode_basis"] == "measured"
    assert gopro["via"] == "link" and gopro["support"] >= 3
    # camera clock − reference clock = 197.3 − (−1.4); creation dates have 1 s resolution
    assert sync["reference_camera"] == camera_of(data, "P100001.MOV")
    assert -gopro["offset"] == pytest.approx(198.7, abs=1.1)
    for rel in ("IMG_20001.MOV", "PXL_0001.mp4"):
        assert sync["cameras"][camera_of(data, rel)]["mode"] == "start"


def test_no_continuous_camera(scenario):
    store, name, gt = scenario("scn_nocont")
    data = store.load(name)
    by = files_by_rel(data)
    # the long clip bridging takes 1-2 is the reference; take 3 links to nothing but itself
    assert by["DJI_0001.MP4"]["sync"]["method"] == "reference"
    assert {by[r]["sync"]["via"] for r in ("IMG_30003.MOV", "MI_0003.mp4")} == {"group"}
    # take 3 is still placed by its clocks, within their resolution
    ref = by["DJI_0001.MP4"]
    t3 = by["IMG_30003.MOV"]
    true = gt["files"]["IMG_30003.MOV"]["offset"] - gt["files"]["DJI_0001.MP4"]["offset"]
    assert abs((t3["offset"] - ref["offset"]) - true) < 1.5


def test_files_without_metadata_use_full_search(scenario):
    store, name, gt = scenario("scn_nometa")
    by = files_by_rel(store.load(name))
    for rel, t in gt["files"].items():
        if not t["ct"]:
            assert by[rel]["sync"]["method"] == "search", rel
            assert by[rel]["sync"]["reason"] == "no creation time"


def test_no_creation_times_at_all(scenario):
    store, name, gt = scenario("scn_nometa")
    snapshot = store.load(name)
    try:
        with store.edit(name) as d:
            for f in d["files"]:
                f["probe"]["creation_time"] = None
        syncing.sync_session(store, name, Job("sync", name))
        data = store.load(name)
        errs = sync_errors(data, gt)
        assert all(abs(e) <= FRAME for e in errs.values()), errs
        assert sorted(f["sync"]["method"] for f in data["files"]).count("reference") == 1
        assert {f["sync"]["method"] for f in data["files"]} == {"reference", "search"}
    finally:
        store.save(name, snapshot)


def test_manual_offset_overlapping_its_camera_is_flagged(scenario):
    store, name, gt = scenario("scn_clock")
    snapshot = store.load(name)
    try:
        with store.edit(name) as d:
            by = files_by_rel(d)
            a, b = by["IMG_20001.MOV"], by["IMG_20002.MOV"]
            b["offset"], b["sync"] = a["offset"] + 5.0, {"method": "manual"}   # same phone, recording twice at once
        syncing.sync_session(store, name, Job("sync", name))
        after = files_by_rel(store.load(name))
        assert after["IMG_20002.MOV"]["sync"]["method"] == "manual"            # never touched...
        assert after["IMG_20002.MOV"]["offset"] - after["IMG_20001.MOV"]["offset"] == pytest.approx(5.0, abs=1e-3)
        assert any("same camera" in fl for fl in after["IMG_20001.MOV"]["sync"]["flags"])  # ...but its neighbour is flagged
    finally:
        store.save(name, snapshot)


# ------------------------------------------------------------------ no camera clock attached
# Hand-made sessions (no ffmpeg): the audio is written straight into the cache.

EPOCH = 1_700_000_000.0


@pytest.fixture
def bare_store(tmp_path):
    from mocap_preprocess import config
    from mocap_preprocess.session_store import SessionStore
    prev = config.sessions_dir()
    config.set_sessions_dir(tmp_path)
    yield SessionStore()
    config.set_sessions_dir(prev)


def make_session(store, files, takes):
    """``files``: (rel, camera, creation time, has audio, offset, sync method), 20 s each."""
    import numpy as np

    from mocap_preprocess.session_store import file_id
    paths = store.create("s").ensure()
    with store.edit("s") as d:
        sr = int(d["settings"]["sync_sample_rate"])
        noise = np.random.default_rng(1).standard_normal(20 * sr).astype(np.float32)
        d["files"], cams = [], []
        for rel, cam, ct, audio, off, method in files:
            fid = file_id(rel)
            d["files"].append({"id": fid, "rel": rel, "size": 1, "mtime": 1.0, "camera": cam, "offset": off,
                               "sync": {"method": method}, "assets": {},
                               "probe": {"duration": 20.0, "creation_time": ct, "format_start": 0,
                                         "video": {"nominal_fps": "30"},
                                         "audio": {"start": 0, "sample_rate": sr, "channels": 1} if audio else None}})
            if audio:
                np.save(paths.audio_npy(fid, sr), noise)
            if cam not in cams:
                cams.append(cam)
        d["cameras"] = [{"id": c, "name": c, "rotation": 0, "include": True} for c in cams]
        d["takes"] = [{"id": f"t{i}", "name": f"take_{i:02d}", "start": a, "end": b, "export": True}
                      for i, (a, b) in enumerate(takes, 1)]


def test_no_audio_keeps_files_and_takes_where_they_were(bare_store):
    """Timestamped cameras, no audio: nothing to move, least of all by a Unix timestamp."""
    make_session(bare_store, [("a.mov", "c1", EPOCH, False, 0.0, "metadata"),
                              ("b.mov", "c2", EPOCH + 3, False, 3.0, "metadata")], [(5.0, 10.0)])
    syncing.sync_session(bare_store, "s", Job("sync", "s"))
    data = bare_store.load("s")
    by = files_by_rel(data)
    assert (by["a.mov"]["offset"], by["b.mov"]["offset"]) == (0.0, 3.0)
    assert by["a.mov"]["sync"] == {"method": "metadata", "via": "creation time", "reason": "no audio track"}
    assert [(t["start"], t["end"]) for t in data["takes"]] == [(5.0, 10.0)]


@pytest.mark.parametrize("manual", [False, True])
def test_audio_without_timestamps_and_a_clock_only_camera(bare_store, manual):
    """The audio file has no creation time (placed by itself, at 0), the other camera has
    one but no audio: no clock ties that camera to the audio, so it can't be placed by
    its raw creation time, decades away from everything else."""
    other = ("b.mov", "c2", EPOCH, False, 30.0 if manual else 0.0, "manual" if manual else "metadata")
    make_session(bare_store, [("a.mov", "c1", None, True, 21.0, "none"), other], [(25.0, 30.0)])
    syncing.sync_session(bare_store, "s", Job("sync", "s"))
    data = bare_store.load("s")
    by = files_by_rel(data)
    a, b = by["a.mov"], by["b.mov"]
    assert a["sync"]["method"] == "reference"
    assert max(f["offset"] for f in data["files"]) < 100, [(f["rel"], f["offset"]) for f in data["files"]]
    assert data["takes"][0]["start"] - a["offset"] == pytest.approx(4.0)      # still on the same footage
    if manual:
        assert b["sync"]["method"] == "manual" and b["offset"] - a["offset"] == pytest.approx(9.0)
    else:
        assert b["sync"] == {"method": "none"} and b["offset"] > a["offset"] + 20


def test_nudging_a_manual_clip_during_the_sync_is_kept(bare_store, monkeypatch):
    """A clip that was manual when the sync started keeps its offset as it was then; one the
    user nudges while the sync runs keeps the nudge (moved with everything else)."""
    make_session(bare_store, [("a.mov", "c1", None, True, 21.0, "none"),
                              ("b.mov", "c2", EPOCH, False, 30.0, "manual")], [(25.0, 30.0)])
    real_check = syncing.SessionSync.check

    def check(self, offsets, info, delta):           # runs after every file is placed, before the save
        with bare_store.edit("s") as d:
            f = next(x for x in d["files"] if x["rel"] == "b.mov")
            f["offset"], f["sync"] = 30.5, {"method": "manual", "was": "metadata"}
        real_check(self, offsets, info, delta)
    monkeypatch.setattr(syncing.SessionSync, "check", check)
    syncing.sync_session(bare_store, "s", Job("sync", "s"))
    data = bare_store.load("s")
    a, b = files_by_rel(data)["a.mov"], files_by_rel(data)["b.mov"]
    assert b["offset"] - a["offset"] == pytest.approx(9.5)                      # 9.0 before the nudge
    assert b["sync"] == {"method": "manual", "was": "metadata"}
    assert data["takes"][0]["start"] - a["offset"] == pytest.approx(4.0)
