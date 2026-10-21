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


def test_range_fallback_implementation(client, processed, monkeypatch):
    """The hand-written range responder (used when Starlette lacks Range support)."""
    store, name, _ = processed
    monkeypatch.setattr(server, "FILE_RESPONSE_RANGE", False)
    f = store.load(name)["files"][0]
    url = f"/media/{name}/proxy/{f['id']}.mp4"
    size = store.paths(name).proxy(f["id"]).stat().st_size
    r = client.get(url, headers={"Range": "bytes=10-19"})
    assert r.status_code == 206 and len(r.content) == 10
    assert r.headers["content-range"] == f"bytes 10-19/{size}"
    assert client.get(url, headers={"Range": f"bytes={size + 5}-"}).status_code == 416


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
