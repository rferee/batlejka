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


def test_dropped_connection_still_closes_its_socket():
    import socket
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
