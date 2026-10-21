"""Shared fixtures: the synthetic session is generated once (cached by version in
tests/_tmp/) and run through scan → process → proxies → sync once per test run."""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mocap_preprocess import config  # noqa: E402

SESSIONS = Path(__file__).resolve().parent / "_tmp" / "sessions"
NAME = "synthetic"


def _have_tools() -> bool:
    return bool(config.ffmpeg_path() and config.ffprobe_path())


@pytest.fixture(scope="session")
def synthetic():
    if not _have_tools():
        pytest.skip("ffmpeg/ffprobe not on PATH")
    import make_synthetic_session as gen

    if not (gen.CLIPS_DIR / "synced_cam1.mp4").exists():
        pytest.skip(f"test clips not found in {gen.CLIPS_DIR} (set MOCAP_TEST_CLIPS)")
    session = gen.build(SESSIONS)
    gt = json.loads((session / "ground_truth.json").read_text())
    return session, gt


@pytest.fixture(scope="session")
def store():
    """A store on tests/_tmp/sessions. Needs ffmpeg only, not the external clips: the scn_*
    scenarios are generated from scratch, so they run on any machine with ffmpeg."""
    from mocap_preprocess.session_store import SessionStore

    if not _have_tools():
        pytest.skip("ffmpeg/ffprobe not on PATH")
    config.set_sessions_dir(SESSIONS)
    return SessionStore()


def wait_job(job_id: str, timeout: float = 60):
    """Block until the app's job ``job_id`` has ended; returns the Job."""
    from mocap_preprocess.app_state import jobs

    job = jobs.get(job_id)
    assert job is not None, f"no job {job_id}"
    assert job.done_event.wait(timeout), f"{job.label} didn't finish in {timeout} s"
    return job


@pytest.fixture(scope="session")
def scanned(synthetic, store):
    """Fresh .preprocess/ and output/, then a scan."""
    from mocap_preprocess import pipeline
    from mocap_preprocess.jobs import Job

    session, gt = synthetic
    for d in (session / ".preprocess", session / "output"):
        shutil.rmtree(d, ignore_errors=True)
    pipeline.scan(store, NAME, Job("scan", NAME))
    return store, NAME, gt


@pytest.fixture(scope="session")
def processed(scanned):
    from mocap_preprocess import pipeline
    from mocap_preprocess.jobs import Job

    store, name, gt = scanned
    pipeline.process(store, name, Job("process", name))
    job = Job("proxies", name)
    pipeline.run_proxies(store, name, job)
    return store, name, gt


@pytest.fixture(scope="session")
def synced(processed):
    from mocap_preprocess import syncing
    from mocap_preprocess.jobs import Job

    store, name, gt = processed
    job = Job("sync", name)
    syncing.sync_session(store, name, job)
    return store, name, gt


@pytest.fixture(scope="session")
def scenario(store):
    """scenario(name) → (store, name, ground truth): built (cached), scanned,
    processed (no proxies) and synced once per test run."""
    from mocap_preprocess import pipeline, syncing
    from mocap_preprocess.jobs import Job
    import make_synthetic_session as gen

    done = {}

    def get(name):
        if name not in done:
            session = gen.build_scenario(SESSIONS, name)
            for d in (session / ".preprocess", session / "output"):
                shutil.rmtree(d, ignore_errors=True)
            pipeline.scan(store, name, Job("scan", name))
            pipeline.process(store, name, Job("process", name))
            syncing.sync_session(store, name, Job("sync", name))
            done[name] = json.loads((session / "ground_truth.json").read_text())
        return store, name, done[name]
    return get


def files_by_rel(data: dict) -> dict:
    return {f["rel"]: f for f in data["files"]}


def overlap_components(gt: dict) -> list[list[str]]:
    """Audio files grouped by true overlap (> 1 s): offsets can only be checked
    against clips something links them to."""
    rels = [r for r, t in gt["files"].items() if t["audio"]]
    iv = {r: (gt["files"][r]["offset"], gt["files"][r]["offset"] + gt["files"][r]["duration"]) for r in rels}
    parent = {r: r for r in rels}

    def find(x):
        while parent[x] != x:
            x = parent[x]
        return x
    for a in rels:
        for b in rels:
            if a < b and min(iv[a][1], iv[b][1]) - max(iv[a][0], iv[b][0]) > 1.0:
                parent[find(a)] = find(b)
    comps: dict[str, list[str]] = {}
    for r in rels:
        comps.setdefault(find(r), []).append(r)
    return list(comps.values())


def sync_errors(data: dict, gt: dict) -> dict[str, float]:
    """Per audio file: its offset error relative to the longest clip it's linked to."""
    by = files_by_rel(data)
    out = {}
    for comp in overlap_components(gt):
        anchor = max(comp, key=lambda r: gt["files"][r]["duration"])
        for r in comp:
            out[r] = (by[r]["offset"] - by[anchor]["offset"]) - (gt["files"][r]["offset"] - gt["files"][anchor]["offset"])
    return out
