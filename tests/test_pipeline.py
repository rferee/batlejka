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
