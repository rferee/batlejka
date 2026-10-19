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
def store(synthetic):
    from mocap_preprocess.session_store import SessionStore

    config.set_sessions_dir(SESSIONS)
    return SessionStore()


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


def files_by_rel(data: dict) -> dict:
    return {f["rel"]: f for f in data["files"]}
