"""Pure-logic tests (no ffmpeg needed)."""
from __future__ import annotations

import numpy as np
import pytest

from mocap_preprocess import ffmpeg_tools as ff
from mocap_preprocess import grouping, pipeline, syncing, takes
from mocap_preprocess.session_store import InvalidName, file_id, sanitize_filename, validate_session_name


# ------------------------------------------------------------------ fps / metadata parsing

@pytest.mark.parametrize("avg,expected", [
    ("2997/100", "30000/1001"), ("1798/60", "30000/1001"), ("30/1", "30"), ("5994/100", "60000/1001"),
    ("60/1", "60"), ("25/1", "25"), ("24000/1001", "24000/1001"), ("119.88", "120000/1001"),
    ("27/1", "27"),  # nothing standard within 0.3 % → raw value
])
def test_snap_fps(avg, expected):
    assert ff.snap_fps(avg) == expected


def test_fps_label():
    assert ff.fps_label("30000/1001") == "29.97"
    assert ff.fps_label("60000/1001") == "59.94"
    assert ff.fps_label("24000/1001") == "23.976"
    assert ff.fps_label("30") == "30"


@pytest.mark.parametrize("text,expected", [
    ("2026-10-19T12:00:05.000000Z", 1792411205.0),
    ("2026-10-19T15:00:05+0300", 1792411205.0),
    ("2026-10-19T15:00:05+03:00", 1792411205.0),
    ("2026-10-19 12:00:05", 1792411205.0),
    ("1904-01-01T00:00:00Z", None),
    ("garbage", None),
    (None, None),
])
def test_parse_time(text, expected):
    assert ff.parse_time(text) == expected


@pytest.mark.parametrize("stream,expected", [
    ({"side_data_list": [{"rotation": -90}]}, 90),
    ({"side_data_list": [{"rotation": 90}]}, 270),
    ({"side_data_list": [{"rotation": 180}]}, 180),
    ({"tags": {"rotate": "90"}}, 90),
    ({"tags": {"rotate": "270"}}, 270),
    ({}, 0),
])
def test_rotation(stream, expected):
    assert ff._rotation_cw(stream) == expected


def test_drift_note_matches_spec_example():
    note = pipeline._drift_note("60", "60000/1001")
    assert "~3.6 frames" in note  # 0.1 % of 59.94 fps × 60 s
    note30 = pipeline._drift_note("60", "30000/1001")
    assert "(2×)" in note30 and "~1.8 frames" in note30


# ------------------------------------------------------------------ names / ids

def test_session_names():
    assert validate_session_name("2026-10-19") == "2026-10-19"
    for bad in ("../x", "a/b", "a\\b", "..", ".hidden", "CON", "x:", "trailing.", ""):
        with pytest.raises(InvalidName):
            validate_session_name(bad)


def test_file_id_stable():
    assert file_id("sub/IMG_0001.MOV") == file_id("sub\\IMG_0001.MOV")
    assert file_id("a").startswith("f_") and len(file_id("a")) == 12


def test_sanitize_filename():
    assert sanitize_filename('take: 1/2?') == "take_ 1_2_"
    assert sanitize_filename("CON") == "_CON"
    assert sanitize_filename("  ...  ", "fallback") == "fallback"


# ------------------------------------------------------------------ correlation

def _room(n, rng):
    x = rng.standard_normal(n).astype(np.float32) * 0.1
    for k in range(5):
        s = int(n * (0.1 + 0.18 * k))
        x[s:s + 200] += rng.standard_normal(200).astype(np.float32) * np.exp(-np.arange(200) / 30)
    return x


def test_ncc_finds_offset_and_handles_partial_overlap():
    sr = 8000
    rng = np.random.default_rng(1)
    r = _room(60 * sr, rng)
    x = r[20 * sr: 32 * sr] + rng.standard_normal(12 * sr).astype(np.float32) * 0.05
    m = syncing.ncc_match(syncing.prepare(r), syncing.prepare(x), sr)
    assert m.lag == 20 * sr and m.score > 0.5 and m.ratio > 3
    # x starts 2 s before r does: negative lag, partial overlap
    y = np.concatenate([rng.standard_normal(2 * sr).astype(np.float32) * 0.1, r[: 10 * sr]])
    m = syncing.ncc_match(syncing.prepare(r), syncing.prepare(y), sr)
    assert m.lag == -2 * sr


def test_ncc_rejects_unrelated_audio():
    sr = 8000
    rng = np.random.default_rng(2)
    a = _room(30 * sr, rng)
    b = _room(10 * sr, np.random.default_rng(3))
    m = syncing.ncc_match(syncing.prepare(a), syncing.prepare(b), sr)
    assert m.score < 0.1 or m.ratio < 1.3


def test_window_and_dot_curves_match_the_full_search():
    sr = 2000
    rng = np.random.default_rng(6)
    r = syncing.prepare(_room(40 * sr, rng))
    x = syncing.prepare(r[12 * sr: 20 * sr] + rng.standard_normal(8 * sr).astype(np.float32) * 0.3)
    m = syncing.ncc_match(r, x, sr)
    w = syncing.window_curve(r, x, m.lag - 50, m.lag + 50, sr)
    assert int(np.nanargmax(w)) == 50 and w[50] == pytest.approx(m.score, rel=1e-4)
    d = syncing.dot_curve(r, x, m.lag - 3, m.lag + 3)
    assert np.allclose(d, w[47:54], atol=1e-4)
    # the longer signal may be either one
    w2 = syncing.window_curve(x, r, -m.lag - 50, -m.lag + 50, sr)
    assert np.allclose(w2[::-1], w, atol=1e-5)


def test_consensus_is_the_largest_cluster_not_the_mean():
    votes = [76.1, 76.4, 75.9, 76.3, -2874.7, -2874.6, 2015.2, 690.5]
    c = syncing.consensus(votes)
    assert c.support == 4 and c.runner_up == 2
    assert c.center == pytest.approx(76.2, abs=0.11)
    assert syncing.consensus([]) is None


def test_stamp_modes_from_cluster_tightness():
    """Camera B stamps the end of its clips: only that reading makes the
    implied clock differences of clips with different lengths agree."""
    d_a, d_b = [15.0, 33.0, 8.0], [20.0, 9.0, 41.0]   # clip lengths of the matched pairs
    k = -30.0                                         # K_A − K_B: B's clock runs 30 s ahead
    # implied K_A − K_B of each match when A is read as `ma` and B as `mb`
    xs = {(ma, mb): syncing.consensus([k + (db if mb == "start" else 0.0) + (da if ma == "end" else 0.0)
                                       for da, db in zip(d_a, d_b)])
          for ma in syncing.MODES for mb in syncing.MODES}
    modes, basis = syncing.SessionSync.choose_modes(["A", "B"], {("A", "B"): xs})
    assert modes == {"A": "start", "B": "end"} and basis["B"] == "measured"


def test_xcorr_matches_scipy_correlate():
    from scipy import signal
    rng = np.random.default_rng(4)
    r = rng.standard_normal(50000)
    x = rng.standard_normal(3000)
    assert np.allclose(syncing.xcorr_full(r, x), signal.correlate(r, x, mode="full", method="fft"))


# ------------------------------------------------------------------ grouping / takes on fake data

def _file(fid, rel, ct, dur, make="Apple", model="iPhone 13", w=1080, h=1920, cam=None, offset=None):
    return {"id": fid, "rel": rel, "camera": cam, "offset": offset, "sync": {"method": "audio"},
            "probe": {"duration": dur, "creation_time": ct, "make": make, "model": model,
                      "video": {"codec": "hevc", "display_width": w, "display_height": h, "nominal_fps": "30000/1001"}}}


def test_grouping_splits_identical_devices_by_overlap_and_background():
    rng = np.random.default_rng(5)
    view_a, view_b = rng.standard_normal((32, 32)), rng.standard_normal((32, 32))
    files = [
        _file("a1", "IMG_1.MOV", 0, 10), _file("b1", "IMG_2.MOV", 2, 10),
        _file("a2", "IMG_3.MOV", 30, 10), _file("b2", "IMG_4.MOV", 31, 10),
        _file("fuji", "DSCF0001.MOV", -5, 100, make="FUJIFILM", model="X-T4", w=1920, h=1080),
    ]
    feats = {"a1": view_a, "a2": view_a + 0.1 * rng.standard_normal((32, 32)),
             "b1": view_b, "b2": view_b + 0.1 * rng.standard_normal((32, 32)), "fuji": rng.standard_normal((32, 32))}
    cams, assign = grouping.group_all(files, lambda f: feats[f["id"]])
    assert len(cams) == 3
    assert assign["a1"] == assign["a2"] != assign["b1"] == assign["b2"]
    assert [c["name"] for c in cams] == ["cam1", "cam2", "cam3"]  # ordered by earliest clip (fuji first)
    assert assign["fuji"] == cams[0]["id"]


def test_grouping_subfolders_win():
    files = [_file("x", "fuji/A.MOV", 0, 10), _file("y", "phone/B.MOV", 0, 10), _file("z", "phone/C.MOV", 20, 10)]
    cams, assign = grouping.group_all(files, lambda f: None)
    names = {c["id"]: c["name"] for c in cams}
    assert names[assign["x"]] == "fuji" and names[assign["y"]] == names[assign["z"]] == "phone"


def test_auto_takes_intersection_and_continuous_ignored():
    data = {"files": [
        _file("fuji", "F.MOV", 0, 100, cam="c1", offset=0.0),
        _file("a", "A.MOV", 0, 12, cam="c2", offset=10.0), _file("b", "B.MOV", 0, 12, cam="c3", offset=11.0),
        _file("c", "C.MOV", 0, 10, cam="c2", offset=50.0), _file("d", "D.MOV", 0, 11, cam="c3", offset=49.5),
    ]}
    t = takes.detect(data)
    assert [(x["start"], x["end"]) for x in t] == [(11.0, 22.0), (50.0, 60.0)]
    assert [x["name"] for x in t] == ["take_01", "take_02"]


def test_takes_validate():
    out = takes.validate([{"id": "t2", "name": " walk ", "start": 5, "end": 9}, {"id": "t1", "name": "", "start": 1, "end": 2}])
    assert [t["id"] for t in out] == ["t1", "t2"] and out[1]["name"] == "walk" and out[0]["name"]
    with pytest.raises(ValueError):
        takes.validate([{"id": "t1", "name": "x", "start": 3, "end": 3}])


def test_metadata_fallback_uses_same_camera_clock():
    files = [_file("g", "G.MOV", 1000.0, 10, cam="c1", offset=5.0), _file("f", "F.MOV", 1030.0, 10, cam="c1"),
             _file("h", "H.MOV", 2000.0, 10, cam="c2", offset=100.0)]
    by_id = {f["id"]: f for f in files}
    off, via = syncing.metadata_offset(by_id["f"], by_id, {"g": 5.0, "h": 100.0})
    assert off == pytest.approx(35.0) and via == "camera clock"
