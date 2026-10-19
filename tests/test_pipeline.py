"""End-to-end checks on the synthetic session (§9): scan, grouping, sync, auto takes."""
from __future__ import annotations

import pytest

from conftest import files_by_rel
from mocap_preprocess import pipeline, syncing, takes
from mocap_preprocess.jobs import Job

FRAME = 1001 / 30000


def test_scan_prefills_fps_and_warns(scanned):
    store, name, gt = scanned
    data = store.load(name)
    assert data["settings"]["target_fps"] == "30000/1001"
    assert len(data["files"]) == len(gt["files"])
    check = pipeline.fps_check(data)
    assert check["level"] == "warn"
    titles = " | ".join(i["title"] for i in check["items"])
    assert "at 30 fps instead of 29.97" in titles
    assert "without audio" in titles
    rate_item = next(i for i in check["items"] if "instead of" in i["title"])
    by_id = {f["id"]: f["rel"] for f in data["files"]}
    assert sorted(by_id[i] for i in rate_item["files"]) == sorted(gt["cameras"]["pixel"])
    no_audio = next(i for i in check["items"] if "without audio" in i["title"])
    assert [by_id[i] for i in no_audio["files"]] == gt["no_audio"]
    fuji = files_by_rel(data)["DSCF0001.MOV"]["probe"]["video"]
    assert fuji["rotation_meta"] == 90 and (fuji["display_width"], fuji["display_height"]) == (640, 360)


def test_grouping_matches_true_cameras(processed):
    store, name, gt = processed
    data = store.load(name)
    got = {}
    for f in data["files"]:
        got.setdefault(f["camera"], set()).add(f["rel"])
    want = {frozenset(v) for v in gt["cameras"].values()}
    assert {frozenset(v) for v in got.values()} == want
    assert len(data["cameras"]) == 4
    assert all(f["assets"]["thumb"] and f["assets"]["proxy"] for f in data["files"])


def test_sync_within_one_frame(synced):
    store, name, gt = synced
    data = store.load(name)
    for rel, f in files_by_rel(data).items():
        truth = gt["files"][rel]
        if truth["audio"]:
            assert f["sync"]["method"] in ("reference", "audio"), rel
            assert abs(f["offset"] - truth["offset"]) <= FRAME, (rel, f["offset"], truth["offset"])
        else:
            assert f["sync"]["method"] == "metadata", rel   # flagged (amber in the UI)
            assert f["sync"]["via"] == "camera clock"
            assert abs(f["offset"] - truth["offset"]) < 1.5   # creation dates have 1 s resolution


def test_auto_takes_match_truth(synced):
    store, name, gt = synced
    found = takes.detect(store.load(name))
    assert len(found) == len(gt["takes"])
    for got, want in zip(found, gt["takes"]):
        assert got["start"] == pytest.approx(want["start"], abs=0.5)
        assert got["end"] == pytest.approx(want["end"], abs=0.5)


def test_sync_without_audio_keeps_manual_offsets(synced):
    store, name, gt = synced
    snapshot = store.load(name)
    try:
        with store.edit(name) as d:
            for f in d["files"]:
                f["probe"]["audio"] = None          # a session recorded without audio
            nudged = next(f for f in d["files"] if f["rel"] == "IMG_0102.MOV")
            nudged["offset"], nudged["sync"] = 7.25, {"method": "manual"}
        syncing.sync_session(store, name, Job("sync", name))
        after = files_by_rel(store.load(name))
        assert after["IMG_0102.MOV"]["sync"]["method"] == "manual"
        others = [f for rel, f in after.items() if rel != "IMG_0102.MOV"]
        assert all(f["sync"]["method"] == "metadata" for f in others)
        # same camera's other clips are placed through the nudged clip's clock
        a1 = after["IMG_0101.MOV"]
        ct = {rel: f["probe"]["creation_time"] for rel, f in after.items()}
        assert a1["offset"] - after["IMG_0102.MOV"]["offset"] == pytest.approx(ct["IMG_0101.MOV"] - ct["IMG_0102.MOV"], abs=1e-3)
    finally:
        store.save(name, snapshot)


def test_resync_anchors_on_trusted_files_not_a_guessed_reference(synced):
    store, name, gt = synced
    snapshot = store.load(name)
    try:
        with store.edit(name) as d:
            d["takes"] = [{"id": "t1", "name": "take_01", "start": 6.0, "end": 12.0, "export": True}]
            fuji = next(f for f in d["files"] if f["rel"] == "DSCF0001.MOV")
            fuji["offset"], fuji["sync"] = 3.7, {"method": "metadata"}   # e.g. added later, only a guess
        before = files_by_rel(store.load(name))
        syncing.sync_session(store, name, Job("sync", name))
        data = store.load(name)
        after = files_by_rel(data)
        shift = after["IMG_0101.MOV"]["offset"] - before["IMG_0101.MOV"]["offset"]
        for rel in ("IMG_0102.MOV", "IMG_0201.MOV", "VID_20261019_120044.mp4"):
            assert after[rel]["offset"] - before[rel]["offset"] == pytest.approx(shift, abs=FRAME)
        assert data["takes"][0]["start"] == pytest.approx(6.0 + shift, abs=1e-6)  # take moved with the phones
        assert after["DSCF0001.MOV"]["sync"]["method"] == "reference"
        assert after["IMG_0101.MOV"]["offset"] - after["DSCF0001.MOV"]["offset"] == pytest.approx(
            gt["files"]["IMG_0101.MOV"]["offset"], abs=FRAME)
    finally:
        store.save(name, snapshot)


def test_resync_against_chosen_file_and_manual_kept(synced):
    store, name, gt = synced
    data = store.load(name)
    by_rel = files_by_rel(data)
    target, ref = by_rel["IMG_0202.MOV"], by_rel["IMG_0102.MOV"]  # phone vs phone, same take
    res = syncing.resync_file(store, name, target["id"], ref["id"])
    assert res["accepted"]
    after = files_by_rel(store.load(name))["IMG_0202.MOV"]
    assert after["sync"]["method"] == "manual"
    assert abs(after["offset"] - gt["files"]["IMG_0202.MOV"]["offset"]) <= FRAME
    # nudge it a bit; a normal re-sync must not touch the manual offset...
    with store.edit(name) as d:
        f = next(x for x in d["files"] if x["id"] == target["id"])
        f["offset"] += 0.5
    syncing.sync_session(store, name, Job("sync", name))
    kept = files_by_rel(store.load(name))["IMG_0202.MOV"]
    assert kept["offset"] == pytest.approx(gt["files"]["IMG_0202.MOV"]["offset"] + 0.5, abs=FRAME)
    # ...unless re-syncing manual files is requested
    syncing.sync_session(store, name, Job("sync", name), resync_manual=True)
    fixed = files_by_rel(store.load(name))["IMG_0202.MOV"]
    assert fixed["sync"]["method"] == "audio"
    assert abs(fixed["offset"] - gt["files"]["IMG_0202.MOV"]["offset"]) <= FRAME
