"""Setup panel: GPU probe cache keying, preferred encoder, /api/setup shape (no GPU needed)."""
from __future__ import annotations

import json
import os
import threading

import pytest
from fastapi.testclient import TestClient

from mocap_preprocess import session_store, setup_check
from mocap_preprocess.fmc import env

FFM = {"ffmpeg": "C:/tools/ffmpeg.exe", "ffprobe": "C:/tools/ffprobe.exe", "version": "8.0-test"}
GPU = {"name": "NVIDIA GeForce RTX 3090", "driver": "581.57", "memory": "24576 MiB"}
REAL_PROBE = setup_check.run_gpu_probe      # runs nothing when there's no GPU


@pytest.fixture(autouse=True)
def tools(tmp_path, monkeypatch):
    monkeypatch.setenv("MOCAP_TOOLS_DIR", str(tmp_path / "tools"))
    setup_check.reset_cache()
    yield tmp_path / "tools"
    setup_check.reset_cache()


def fake_probe(calls, decode_ok=True, nvenc_ok=True):
    def run(ffm, gpu):
        calls.append((ffm.get("version"), (gpu or {}).get("driver")))
        if gpu is None:
            return REAL_PROBE(ffm, gpu)
        return {"key": setup_check.probe_key(ffm, gpu), "at": 1.0,
                "decode": {"ok": decode_ok, "error": None if decode_ok else "cuda init failed"},
                "nvenc": {"ok": nvenc_ok, "error": None if nvenc_ok else "Driver does not support the required nvenc API version"}}
    return run


def test_probe_is_cached_per_ffmpeg_and_driver(monkeypatch, tools):
    calls = []
    monkeypatch.setattr(setup_check, "run_gpu_probe", fake_probe(calls))
    setup_check.gpu_probe(FFM, GPU)
    setup_check.gpu_probe(FFM, GPU)
    assert len(calls) == 1                                    # second call served from gpu_probe.json
    assert json.loads((tools / "gpu_probe.json").read_text())["key"]["driver"] == "581.57"
    setup_check.gpu_probe(FFM, {**GPU, "driver": "590.01"})   # new driver → re-test
    assert len(calls) == 2
    setup_check.gpu_probe({**FFM, "version": "8.1"}, {**GPU, "driver": "590.01"})   # new ffmpeg → re-test
    assert len(calls) == 3
    setup_check.gpu_probe({**FFM, "ffmpeg": "D:/other/ffmpeg.exe", "version": "8.1"}, {**GPU, "driver": "590.01"})
    assert len(calls) == 4                                    # another ffmpeg path → re-test
    setup_check.gpu_probe({**FFM, "ffmpeg": "D:/other/ffmpeg.exe", "version": "8.1"}, {**GPU, "driver": "590.01"},
                          refresh=True)
    assert len(calls) == 5                                    # refresh always re-tests


def write_cache(tools, nvenc_ok, mtime=None):
    tools.mkdir(parents=True, exist_ok=True)
    p = tools / "gpu_probe.json"
    p.write_text(json.dumps({"key": {}, "at": 1.0, "decode": {"ok": True, "error": None},
                             "nvenc": {"ok": nvenc_ok, "error": None if nvenc_ok else "no NVENC"}}))
    if mtime is not None:
        os.utime(p, (mtime, mtime))


def test_preferred_encoder_reads_the_cache(tools):
    assert setup_check.gpu_caps() is None
    assert setup_check.preferred_encoder() == "libx264"       # never probed
    write_cache(tools, True, mtime=1_000_000)
    assert setup_check.preferred_encoder() == "h264_nvenc"
    assert session_store.default_session("x")["export_options"]["encoder"] == "h264_nvenc"
    write_cache(tools, False, mtime=2_000_000)                # a new probe result is picked up (mtime)
    assert setup_check.gpu_caps()["nvenc"] == {"ok": False, "error": "no NVENC"}
    assert setup_check.preferred_encoder() == "libx264"
    assert session_store.default_session("x")["export_options"]["encoder"] == "libx264"


def test_existing_session_keeps_its_encoder(tools):
    write_cache(tools, True, mtime=1_000_000)
    data = session_store._normalize({"export_options": {"encoder": "libx264"}}, "x")
    assert data["export_options"]["encoder"] == "libx264"


# ---------------------------------------------------------------------------
# /api/setup

def fake_machine(monkeypatch, *, gpu=GPU, nvenc_ok=True, fmc=None, ready=True):
    calls = []
    monkeypatch.setattr(setup_check, "ffmpeg_info", lambda: dict(FFM))
    monkeypatch.setattr(setup_check, "nvidia_gpu", lambda: gpu)
    monkeypatch.setattr(setup_check, "run_gpu_probe", fake_probe(calls, nvenc_ok=nvenc_ok))
    monkeypatch.setattr(setup_check, "tool_version", lambda name, exe: {"path": f"C:/bin/{name}.exe", "version": f"{name} 1.0"})
    monkeypatch.setattr(setup_check, "uv_path", lambda: "C:/bin/uv.exe")
    status = fmc or {
        "version": "v1.8.2", "state": "ready", "detail": "ready", "error": None, "installed_at": 1.0,
        "check": {"ok": True, "at": 2.0, "freemocap": "v1.8.2", "python": "3.12.11", "opencv": "4.8.1",
                  "mediapipe": "0.10.14", "error": None},
        "selftest": {"passed": True, "at": 3.0, "reprojection_error_px": 0.26, "nan_fraction": 0.0,
                     "upper_arm_mm": 289.0, "thigh_mm": 391.0, "seconds": 140.0, "checks": {}, "error": None},
        "venv": "C:/x/venv", "size_hint": "~3 GB download"}
    checks = []
    monkeypatch.setattr(env, "status", lambda: status)
    monkeypatch.setattr(env, "ready", lambda: ready)
    monkeypatch.setattr(env, "check", lambda job=None: checks.append(1) or status["check"], raising=False)
    return calls, checks


@pytest.fixture()
def client():
    from mocap_preprocess import server

    return TestClient(server.app)


def test_setup_shape_all_good(monkeypatch, client):
    calls, checks = fake_machine(monkeypatch)
    d = client.get("/api/setup").json()
    assert [r["id"] for r in d["rows"]] == ["ffmpeg", "gpu", "gpu_decode", "nvenc", "git", "uv",
                                           "freemocap", "freemocap_selftest"]
    assert d["ok"] is True and all(r["status"] == "ok" for r in d["rows"])
    assert d["gpu"]["nvenc"]["ok"] and d["gpu"]["decode"]["ok"] and d["gpu"]["gpu"]["name"].endswith("3090")
    fm = {r["id"]: r for r in d["rows"]}
    assert "MediaPipe 0.10.14" in fm["freemocap"]["detail"]
    assert "0.26 px" in fm["freemocap_selftest"]["detail"] and "289 mm" in fm["freemocap_selftest"]["detail"]
    assert d["freemocap"]["state"] == "ready" and d["freemocap"]["busy"] is None
    assert len(calls) == 1 and not checks
    client.get("/api/setup")
    assert len(calls) == 1                         # in-memory cache
    client.get("/api/setup?refresh=1")
    assert len(calls) == 2 and len(checks) == 1    # refresh re-tests the GPU and re-runs FreeMoCap's check


def test_setup_failures_and_neutral_rows(monkeypatch, client):
    fake_machine(monkeypatch, nvenc_ok=False, fmc={"version": "v1.8.2", "state": "not_installed",
                                                     "size_hint": "~3 GB download", "check": None, "selftest": None})
    d = client.get("/api/setup").json()
    rows = {r["id"]: r for r in d["rows"]}
    assert rows["nvenc"]["status"] == "warn" and "nvenc API version" in rows["nvenc"]["detail"]
    assert rows["freemocap"]["status"] == "warn" and [a["id"] for a in rows["freemocap"]["actions"]] == ["install"]
    assert "3 GB" in rows["freemocap"]["detail"]
    assert rows["freemocap_selftest"]["status"] == "neutral" and not rows["freemocap_selftest"]["actions"]
    assert d["ok"] is False

    setup_check.reset_cache()
    fake_machine(monkeypatch, gpu=None)
    rows = {r["id"]: r for r in client.get("/api/setup").json()["rows"]}
    assert rows["gpu"]["status"] == "neutral"                       # no GPU is not an error
    assert rows["gpu_decode"]["status"] == "neutral" and rows["nvenc"]["status"] == "neutral"


def test_freemocap_actions(monkeypatch, client):
    fake_machine(monkeypatch, ready=False)
    assert client.post("/api/setup/freemocap/upgrade").status_code == 400
    release = threading.Event()
    started = []

    def fake_install(job):
        started.append(job.id)
        job.set_progress(0.4, "uv pip sync")
        release.wait(10)

    monkeypatch.setattr(env, "ACTIONS", {"install": (fake_install, "Install FreeMoCap"),
                                         "selftest": (lambda job: None, "FreeMoCap self-test")})
    assert client.post("/api/setup/freemocap/selftest").status_code == 409   # not ready
    try:
        r = client.post("/api/setup/freemocap/install")
        assert r.status_code == 200 and r.json()["kind"] == "setup" and r.json()["session"] is None
        for _ in range(100):
            if started:
                break
            threading.Event().wait(0.02)
        d = client.get("/api/setup").json()
        row = next(x for x in d["rows"] if x["id"] == "freemocap")
        assert row["status"] == "busy" and "uv pip sync" in row["detail"] and d["freemocap"]["busy"]
        assert client.post("/api/setup/freemocap/install").status_code == 409   # one setup job at a time
        jobs = client.get("/api/jobs?global_only=1").json()
        assert any(j["kind"] == "setup" for j in jobs)
    finally:
        release.set()
