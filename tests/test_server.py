"""HTTP API checks: Range requests (§9.6), validation, basic routes."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from conftest import NAME
from mocap_preprocess import server


@pytest.fixture(scope="module")
def client(processed):
    return TestClient(server.app)


def test_health_and_sessions(client):
    h = client.get("/api/health").json()
    assert h["ffmpeg"] and h["ffprobe"]
    names = [s["name"] for s in client.get("/api/sessions").json()]
    assert NAME in names


def test_session_view_has_fps_check(client):
    d = client.get(f"/api/sessions/{NAME}").json()
    assert d["fps_check"]["target"] == "30000/1001"
    assert d["standard_rates"] and d["files"] and d["cameras"]


def test_proxy_range_requests_return_206(client, processed):
    store, name, _ = processed
    f = store.load(name)["files"][0]
    url = f"/media/{name}/proxy/{f['id']}.mp4"
    full = client.get(url)
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
    r = client.get(url, headers={"Range": "bytes=100-1123"})
    assert r.status_code == 206
    assert r.headers["content-range"] == f"bytes 100-1123/{len(full.content)}"
    assert r.content == full.content[100:1124]
    tail = client.get(url, headers={"Range": "bytes=-64"})
    assert tail.status_code == 206 and tail.content == full.content[-64:]


def test_rejects_traversal_and_bad_ids(client):
    assert client.get("/api/sessions/..%2F..%2Fetc").status_code in (400, 404)
    assert client.get(f"/media/{NAME}/proxy/..%2Fsession.json").status_code in (400, 404)
    assert client.get(f"/media/{NAME}/proxy/f_zzzz.mp4").status_code == 400
    assert client.post("/api/sessions", json={"name": "a/b"}).status_code == 400


def test_takes_put_validates(client):
    bad = client.put(f"/api/sessions/{NAME}/takes", json=[{"id": "t1", "name": "x", "start": 5, "end": 2}])
    assert bad.status_code == 400
    original = client.get(f"/api/sessions/{NAME}").json()["takes"]
    ok = client.put(f"/api/sessions/{NAME}/takes", json=[{"id": "t1", "name": "calibration", "start": 1, "end": 4}])
    assert ok.status_code == 200 and ok.json()["takes"][0]["name"] == "calibration"
    client.put(f"/api/sessions/{NAME}/takes", json=original)


def test_cameras_put_rotation_is_only_stored(client):
    d = client.get(f"/api/sessions/{NAME}").json()
    cams = d["cameras"]
    before = {f["id"]: f["assets"] for f in d["files"]}
    cams[0]["rotation"] = 270
    r = client.put(f"/api/sessions/{NAME}/cameras", json={"cameras": cams, "assignments": {}})
    assert r.status_code == 200
    after = r.json()
    assert after["cameras"][0]["rotation"] == 270
    assert {f["id"]: f["assets"] for f in after["files"]} == before   # nothing rebuilt (R1)
    assert client.put(f"/api/sessions/{NAME}/cameras", json={"cameras": [{**cams[0], "rotation": 45}], "assignments": {}}).status_code == 400
    cams[0]["rotation"] = 0
    client.put(f"/api/sessions/{NAME}/cameras", json={"cameras": cams, "assignments": {}})


def _wait(job):
    from mocap_preprocess.app_state import jobs
    assert jobs.get(job["id"]).done_event.wait(20), "job didn't finish"


def test_ui_state_is_kept_on_the_server(client):
    """What the UI remembers per session (audio camera, Preview, ticked mocap takes) lives in
    session.json, so another browser sees the same thing."""
    d = client.get(f"/api/sessions/{NAME}").json()
    assert d["ui"] == {"audio_camera": None, "preview": False, "mocap_selected": []}
    cam = d["cameras"][0]["id"]
    r = client.put(f"/api/sessions/{NAME}/ui", json={"audio_camera": cam, "mocap_selected": ["t1", "t2"]})
    assert r.status_code == 200, r.text
    assert r.json()["ui"] == {"audio_camera": cam, "preview": False, "mocap_selected": ["t1", "t2"]}
    r = client.put(f"/api/sessions/{NAME}/ui", json={"preview": True})           # a partial update keeps the rest
    assert r.json()["ui"] == {"audio_camera": cam, "preview": True, "mocap_selected": ["t1", "t2"]}
    assert client.get(f"/api/sessions/{NAME}").json()["ui"]["preview"] is True
    assert client.put(f"/api/sessions/{NAME}/ui", json={"bogus": 1}).status_code == 400
    assert client.put(f"/api/sessions/{NAME}/ui", json={"mocap_selected": "t1"}).status_code == 400
    assert client.put(f"/api/sessions/{NAME}/ui", json={"audio_camera": 5}).status_code == 400
    client.put(f"/api/sessions/{NAME}/ui", json={"audio_camera": None, "preview": False, "mocap_selected": []})


def test_cameras_put_records_manual_moves(client):
    """Moving a file to another camera by hand is remembered on the file (moved_from); moving it
    back home forgets it; a regroup forgets all of them."""
    d = client.get(f"/api/sessions/{NAME}").json()
    cams = d["cameras"]
    assert len(cams) >= 2
    home, other = cams[0], cams[1]
    f = next(x for x in d["files"] if x["camera"] == home["id"])
    assert "moved_from" not in f
    r = client.put(f"/api/sessions/{NAME}/cameras", json={"cameras": cams, "assignments": {f["id"]: other["id"]}})
    assert r.status_code == 200, r.text
    moved = next(x for x in r.json()["files"] if x["id"] == f["id"])
    assert moved["camera"] == other["id"]
    assert moved["moved_from"]["camera"] == home["id"] and moved["moved_from"]["name"] == home["name"]
    assert moved["moved_from"]["at"] > 0
    # a second move keeps the original home
    third = cams[2] if len(cams) > 2 else home
    r = client.put(f"/api/sessions/{NAME}/cameras", json={"cameras": cams, "assignments": {f["id"]: third["id"]}})
    moved = next(x for x in r.json()["files"] if x["id"] == f["id"])
    if third is home:
        assert "moved_from" not in moved
    else:
        assert moved["moved_from"]["camera"] == home["id"]
    # back home: forgotten
    r = client.put(f"/api/sessions/{NAME}/cameras", json={"cameras": cams, "assignments": {f["id"]: home["id"]}})
    moved = next(x for x in r.json()["files"] if x["id"] == f["id"])
    assert moved["camera"] == home["id"] and "moved_from" not in moved


def test_regroup_clears_manual_moves(client):
    d = client.get(f"/api/sessions/{NAME}").json()
    cams = d["cameras"]
    f = next(x for x in d["files"] if x["camera"] == cams[0]["id"])
    client.put(f"/api/sessions/{NAME}/cameras", json={"cameras": cams, "assignments": {f["id"]: cams[1]["id"]}})
    assert "moved_from" in next(x for x in client.get(f"/api/sessions/{NAME}").json()["files"] if x["id"] == f["id"])
    _wait(client.post(f"/api/sessions/{NAME}/regroup").json())
    after = client.get(f"/api/sessions/{NAME}").json()
    assert all("moved_from" not in x for x in after["files"])
    assert next(x for x in after["files"] if x["id"] == f["id"])["camera"] == cams[0]["id"]   # grouping is deterministic


def test_settings_put_validates_and_prefills(client):
    url = f"/api/sessions/{NAME}/settings"
    before = client.get(f"/api/sessions/{NAME}").json()["settings"]
    try:
        assert client.put(url, json={"target_fps": "abc"}).status_code == 400
        assert client.put(url, json={"sync_sample_rate": 12345}).status_code == 400
        st = client.put(url, json={"proxy_long_side": 10, "sync_min_ratio": 0.5, "sync_window": 99}).json()["settings"]
        assert st["proxy_long_side"] == 160 and st["sync_min_ratio"] == 1.0 and st["sync_window"] == 30.0   # clamped
        st = client.put(url, json={"target_fps": "60"}).json()["settings"]
        assert st["target_fps"] == "60" and st["target_fps_user_set"] is True
        st = client.put(url, json={"target_fps_user_set": False}).json()["settings"]
        assert st["target_fps"] == "30000/1001" and st["target_fps_user_set"] is False      # prefilled from the files again
        st = client.put(url, json={"hwaccel": 1}).json()["settings"]
        assert st["hwaccel"] is True
    finally:
        client.put(url, json={k: before[k] for k in ("proxy_long_side", "sync_min_ratio", "sync_window", "hwaccel")})
        client.put(url, json={"target_fps_user_set": False})


def test_offset_put_marks_the_file_manual(client, processed):
    store, name, _ = processed
    f = next(x for x in store.load(name)["files"] if x.get("offset") is not None)
    was = f["sync"]["method"]
    try:
        r = client.put(f"/api/sessions/{name}/files/{f['id']}/offset", json={"offset": 12.3456789})
        assert r.status_code == 200, r.text
        assert r.json() == {"id": f["id"], "offset": 12.345679, "sync": {"method": "manual", "was": was}}
        r = client.put(f"/api/sessions/{name}/files/{f['id']}/offset", json={"offset": 13})
        assert r.json()["sync"] == {"method": "manual", "was": was}          # the original method is kept
        got = next(x for x in client.get(f"/api/sessions/{name}").json()["files"] if x["id"] == f["id"])
        assert got["offset"] == 13.0 and got["sync"]["method"] == "manual"
        assert client.put(f"/api/sessions/{name}/files/{f['id']}/offset", json={"offset": "x"}).status_code == 400
        assert client.put(f"/api/sessions/{name}/files/f_badid/offset", json={"offset": 1}).status_code == 400
        assert client.put(f"/api/sessions/{name}/files/f_0123456789/offset", json={"offset": 1}).status_code == 404
    finally:
        with store.edit(name) as d:
            x = next(x for x in d["files"] if x["id"] == f["id"])
            x["offset"], x["sync"] = f["offset"], f["sync"]


def test_export_options_put_normalizes(client):
    url = f"/api/sessions/{NAME}/export_options"
    before = client.get(f"/api/sessions/{NAME}").json()["export_options"]
    try:
        o = client.put(url, json={"crf": 99, "encoder": "bogus", "long_side": "keep", "parallel": 7, "layout": "x",
                                  "nvenc_preset": "p9", "camera_ids": "c1"}).json()["export_options"]
        assert (o["crf"], o["encoder"], o["long_side"], o["parallel"], o["layout"], o["nvenc_preset"]) == (51, "libx264", 0, 3, "freemocap", "p5")
        assert "camera_ids" not in o
        assert client.get(f"/api/sessions/{NAME}").json()["export_options"]["crf"] == 51
    finally:
        client.put(url, json=before)


def test_create_session_once(client):
    import shutil
    from mocap_preprocess import config
    name = "zz-created-by-test"
    try:
        assert client.post("/api/sessions", json={"name": name}).status_code == 200
        assert (config.sessions_dir() / name / "source").is_dir()
        assert client.post("/api/sessions", json={"name": name}).status_code == 409
        assert name in {s["name"] for s in client.get("/api/sessions").json()}
    finally:
        shutil.rmtree(config.sessions_dir() / name, ignore_errors=True)


def test_media_kinds_and_auto_takes(client, processed):
    store, name, _ = processed
    f = next(x for x in store.load(name)["files"] if x["assets"].get("wave"))
    r = client.get(f"/media/{name}/thumb/{f['id']}.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    r = client.get(f"/media/{name}/wave/{f['id']}.json")
    assert r.status_code == 200 and "peaks" in r.json()
    assert client.get(f"/media/{name}/nope/{f['id']}.json").status_code == 404
    assert client.get(f"/media/{name}/thumb/{f['id']}.png").status_code == 404        # wrong extension for the kind
    original = client.get(f"/api/sessions/{name}").json()["takes"]
    try:
        client.put(f"/api/sessions/{name}/takes", json=[{"id": "t1", "name": "x", "start": 1, "end": 4}])
        assert client.post(f"/api/sessions/{name}/takes/auto", json={}).status_code == 409    # takes exist
        d = client.post(f"/api/sessions/{name}/takes/auto", json={"replace": True}).json()
        assert all(t["name"] != "x" for t in d["takes"])
    finally:
        client.put(f"/api/sessions/{name}/takes", json=original)


def test_dropped_connection_still_closes_its_socket():
    import sys
    from asyncio import proactor_events

    from mocap_preprocess import aio_fix
    if sys.platform != "win32":
        pytest.skip("Windows proactor loop only")
    assert aio_fix.install()
    closed = []

    class Sock:
        def fileno(self):
            return 5

        def shutdown(self, how):
            raise ConnectionResetError(10054, "An existing connection was forcibly closed by the remote host")

        def close(self):
            closed.append(True)

    class Protocol:
        def connection_lost(self, exc):
            closed.append("protocol")

    t = object.__new__(proactor_events._ProactorBasePipeTransport)
    t._called_connection_lost, t._protocol, t._sock, t._server = False, Protocol(), Sock(), None
    t._call_connection_lost(None)          # used to raise, skipping close()
    assert closed == ["protocol", True] and t._sock is None and t._called_connection_lost
