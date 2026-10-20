"""Opt-in end-to-end check of the FreeMoCap integration (needs the env installed):

    python -m pytest -m fmc

Runs the self-test through the API: downloads FreeMoCap's 3-camera sample (~38 MB),
calibrates one copy, processes another and checks the skeleton (a few minutes on CPU).
"""
import time

import pytest
from fastapi.testclient import TestClient

from mocap_preprocess import server
from mocap_preprocess.fmc import env

pytestmark = pytest.mark.fmc


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app)


def test_selftest_through_the_api(client):
    if not env.ready():
        pytest.skip(f"FreeMoCap isn't ready: {env.status()['detail']} (install it on the Session screen)")
    r = client.post("/api/setup/freemocap/selftest")
    assert r.status_code == 200, r.text
    job = r.json()
    assert job["kind"] == "setup"
    deadline = time.time() + 30 * 60
    while job["status"] in ("queued", "running"):
        assert time.time() < deadline, "self-test took over 30 min"
        time.sleep(2)
        job = client.get(f"/api/jobs/{job['id']}").json()
    assert job["status"] == "done", f"{job['status']}: {job['error']}"
    res = job["result"]
    assert res["passed"], res
    assert res["reprojection_error_px"] < 2.0
    assert res["nan_fraction"] < 0.2
    assert 150 < res["upper_arm_mm"] < 450
    assert 250 < res["thigh_mm"] < 600
    st = env.status()["selftest"]
    assert st and st["passed"] and st["at"] >= job["started"] - 1
