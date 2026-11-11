"""The job manager and its routes, without ffmpeg: cancel, status transitions, log paging,
the session / global listing, and the history cap."""
from __future__ import annotations

import os
import threading

import pytest
from fastapi.testclient import TestClient

from mocap_preprocess import config, server
from mocap_preprocess.app_state import jobs
from mocap_preprocess.ffmpeg_tools import Cancelled
from mocap_preprocess.jobs import JobConflict, JobManager


@pytest.fixture(scope="module")
def client():
    return TestClient(server.app, base_url="http://127.0.0.1:8765")


def wait(job, timeout=10):
    assert job.done_event.wait(timeout), "job didn't finish"
    return job


def test_cancel_through_the_route(client):
    started = threading.Event()

    def run(job):
        job.log("working")
        started.set()
        job.cancel_event.wait(10)
        job.check_cancel()            # raises Cancelled: the usual way a job notices

    job = jobs.start("test_cancel", "jobs-test", run, label="Cancel me")
    assert started.wait(5)
    r = client.post(f"/api/jobs/{job.id}/cancel")
    assert r.status_code == 200 and r.json()["id"] == job.id
    wait(job)
    info = client.get(f"/api/jobs/{job.id}?log_since=0").json()
    assert info["status"] == "cancelled" and info["finished"] and info["error"] is None
    assert [(e["level"], e["msg"]) for e in info["log"]] == [("info", "working"), ("warn", "cancelled")]
    assert client.post("/api/jobs/nope/cancel").status_code == 404
    assert client.get("/api/jobs/nope").status_code == 404


def test_a_job_that_returns_after_a_cancel_is_still_cancelled():
    job = jobs.start("test_cancel2", "jobs-test", lambda j: (j.cancel_event.wait(10), "ignored")[1])
    jobs.cancel(job.id)
    wait(job)
    assert job.status == "cancelled" and job.result == "ignored"


def test_error_keeps_the_message_and_a_trace_tail():
    def boom(job):
        raise ValueError("bad input")

    job = wait(jobs.start("test_error", "jobs-test", boom))
    assert job.status == "error" and job.error == "bad input"
    levels = [e["level"] for e in job.info(log_since=0)["log"]]
    assert levels[0] == "error" and "debug" in levels            # the traceback goes to the debug log


def test_one_active_job_per_kind_and_session():
    gate = threading.Event()
    job = jobs.start("test_conflict", "jobs-test", lambda j: gate.wait(10))
    with pytest.raises(JobConflict):
        jobs.start("test_conflict", "jobs-test", lambda j: None)
    other = jobs.start("test_conflict", "jobs-other", lambda j: None)      # another session is fine
    gate.set()
    wait(job)
    wait(other)
    assert job.status == other.status == "done" and job.progress == 1.0


def test_log_paging_and_live(client):
    def run(job):
        for i in range(3):
            job.log(f"line {i}", "debug" if i == 1 else "info")
        job.live = {"takes": {"t1": "running"}}

    job = wait(jobs.start("test_log", "jobs-test", run))
    info = job.info()
    assert info["log_seq"] == 3 and "log" not in info and "live" not in info
    page = client.get(f"/api/jobs/{job.id}?log_since=1&live=1").json()
    assert [e["seq"] for e in page["log"]] == [2, 3] and page["live"] == {"takes": {"t1": "running"}}
    assert client.get(f"/api/jobs/{job.id}?log_since=3").json()["log"] == []


def test_listing_by_session_includes_global_jobs(client):
    g = wait(jobs.start("test_list_global", None, lambda j: None, label="global"))
    s = wait(jobs.start("test_list_session", "jobs-list", lambda j: None, label="mine"))
    ids = lambda r: {j["id"] for j in r.json()}
    assert {g.id, s.id} <= ids(client.get("/api/jobs?session=jobs-list"))
    assert s.id not in ids(client.get("/api/jobs?session=jobs-elsewhere"))
    glob = ids(client.get("/api/jobs?global_only=1"))
    assert g.id in glob and s.id not in glob
    assert {g.id, s.id} <= ids(client.get("/api/jobs"))
    assert client.get("/api/jobs?session=../x").status_code == 400


def test_history_is_capped_when_jobs_start():
    """Finished jobs beyond the cap are dropped whenever a new job starts (the oldest first)."""
    m = JobManager(max_history=2)
    done = [wait(m.start("k", "s", lambda j: None)) for _ in range(4)]
    kept = {j.id for j in m.list()}
    assert len(kept) == 3 and done[0].id not in kept          # the 4th start pruned the oldest finished one
    live = wait(m.start("k", "s", lambda j: None))
    kept = {j.id for j in m.list()}
    assert live.id in kept and len(kept) == 3 and done[1].id not in kept


def test_cancelled_exception_from_a_tool_counts_as_cancel():
    def run(job):
        raise Cancelled()

    job = wait(jobs.start("test_cancelled_exc", "jobs-test", run))
    assert job.status == "cancelled"


def test_foreign_host_and_cross_site_writes_are_refused(client, monkeypatch):
    """DNS rebinding (a foreign Host) and cross-site forms (a foreign Origin) can't reach the API."""
    monkeypatch.delenv("MOCAP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(config, "_bind_host", "127.0.0.1")
    job = wait(jobs.start("test_guard", "jobs-test", lambda j: None))
    cancel = f"/api/jobs/{job.id}/cancel"
    assert client.get("/api/jobs", headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/jobs", headers={"Host": "evil.example:8765"}).status_code == 400
    assert client.get("/", headers={"Host": "127.0.0.1.evil.example"}).status_code == 400
    assert client.post(cancel, headers={"Host": "evil.example"}).status_code == 400
    assert client.get("/api/jobs", headers={"Host": "[::1]:8765"}).status_code == 200
    assert client.get("/api/jobs", headers={"Host": "localhost:8765"}).status_code == 200
    assert client.get("/api/jobs", headers={"Host": "[::1]evil:8765"}).status_code == 400
    assert client.post(cancel, headers={"Origin": "http://evil.example"}).status_code == 403
    assert client.post(cancel, headers={"Origin": "null"}).status_code == 403
    assert client.post(cancel, headers={"Origin": "http://127.0.0.1:9999"}).status_code == 403
    assert client.post(cancel, headers={"Sec-Fetch-Site": "cross-site"}).status_code == 403
    assert client.post(cancel, headers={"Origin": "http://127.0.0.1:8765"}).status_code == 200
    assert client.post(cancel, headers={"Origin": "http://localhost:8765", "Host": "localhost:8765"}).status_code == 200
    assert client.post(cancel).status_code == 200                    # no Origin: a script, curl
    assert client.get("/api/jobs", headers={"Origin": "http://evil.example"}).status_code == 200   # reads: Host only


def test_host_check_follows_the_bind_address(client, monkeypatch):
    monkeypatch.delenv("MOCAP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setattr(config, "_bind_host", "192.168.1.5")
    assert client.get("/api/jobs", headers={"Host": "192.168.1.5:8765"}).status_code == 200
    assert client.get("/api/jobs", headers={"Host": "pc.lan:8765"}).status_code == 400
    monkeypatch.setenv("MOCAP_ALLOWED_HOSTS", "pc.lan, [fe80::1]")
    assert client.get("/api/jobs", headers={"Host": "pc.lan:8765"}).status_code == 200
    assert client.get("/api/jobs", headers={"Host": "[fe80::1]:8765"}).status_code == 200
    monkeypatch.delenv("MOCAP_ALLOWED_HOSTS")
    monkeypatch.setattr(config, "_bind_host", "0.0.0.0")             # LAN use: any name the machine has
    assert client.get("/api/jobs", headers={"Host": "pc.lan:8765"}).status_code == 200
    assert client.post("/api/jobs/nope/cancel", headers={"Host": "pc.lan:8765",
                                                         "Origin": "http://evil.example"}).status_code == 403


def test_static_files_never_resolve_the_request_path(client, monkeypatch):
    """realpath on a request path would let \\\\host\\share make Windows connect out (and send
    its NTLM hash); the static mount must only compare paths, never touch the filesystem first."""
    assert client.get("/static/app.js").status_code == 200
    assert client.get("/static/%2e%2e/server.py").status_code == 404
    assert client.get("/static/..%2Fserver.py").status_code == 404

    def boom(*a, **kw):
        raise AssertionError("realpath called on a static request")

    monkeypatch.setattr(os.path, "realpath", boom)
    assert client.get("/static/style.css").status_code == 200


def test_the_ui_refuses_to_be_framed(client):
    r = client.get("/")
    assert r.status_code == 200
    assert r.headers["x-frame-options"] == "DENY"
    assert r.headers["content-security-policy"] == "frame-ancestors 'none'"
