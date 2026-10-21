"""Background job manager (R5): each job runs in its own thread and reports
progress, a log, and honours cancel requests. The console gets each job's start and end,
its log lines (info and up) and, for long jobs, a progress line every few seconds."""
from __future__ import annotations

import logging
import threading
import time
import traceback
import uuid
from collections import deque
from typing import Callable

from .ffmpeg_tools import Cancelled

ACTIVE = ("queued", "running")
console = logging.getLogger("mocap.jobs")
LEVELS = {"debug": logging.DEBUG, "info": logging.INFO, "warn": logging.WARNING, "error": logging.ERROR}
PROGRESS_EVERY = 10.0     # seconds between a long job's progress lines on the console


class JobConflict(RuntimeError):
    pass


class Job:
    def __init__(self, kind: str, session: str | None, label: str | None = None):
        self.id = uuid.uuid4().hex[:12]
        self.kind = kind
        self.session = session
        self.label = label or kind
        self.status = "queued"
        self.progress = 0.0
        self.message = ""
        self.error: str | None = None
        self.result = None
        self.created = time.time()
        self.started: float | None = None
        self.finished: float | None = None
        self.cancel_event = threading.Event()
        self._log: deque[dict] = deque(maxlen=3000)
        self._seq = 0
        self._lock = threading.Lock()
        self.done_event = threading.Event()
        self.live = None   # optional JSON snapshot a job publishes for a live view
        self._progress_logged = time.time()

    # -- used by job functions -------------------------------------------
    def log(self, msg: str, level: str = "info") -> None:
        with self._lock:
            self._seq += 1
            self._log.append({"seq": self._seq, "t": time.time(), "level": level, "msg": str(msg)})
        console.log(LEVELS.get(level, logging.INFO), "[%s] %s", self.label, msg)

    def set_progress(self, fraction: float, message: str | None = None) -> None:
        self.progress = float(min(1.0, max(0.0, fraction)))
        if message is not None:
            self.message = message
        now = time.time()
        if now - self._progress_logged >= PROGRESS_EVERY and self.progress < 1.0:
            self._progress_logged = now
            console.info("[%s] %d%%%s", self.label, round(self.progress * 100), f"  {self.message}" if self.message else "")

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check_cancel(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    # -- reporting ---------------------------------------------------------
    def info(self, log_since: int | None = None, live: bool = False) -> dict:
        d = {
            "id": self.id,
            "kind": self.kind,
            "session": self.session,
            "label": self.label,
            "status": self.status,
            "progress": round(self.progress, 4),
            "message": self.message,
            "error": self.error,
            "created": self.created,
            "started": self.started,
            "finished": self.finished,
            "result": self.result if isinstance(self.result, (dict, list, str, int, float, type(None))) else None,
        }
        with self._lock:
            d["log_seq"] = self._seq
            if log_since is not None:
                d["log"] = [e for e in self._log if e["seq"] > log_since]
        if live:
            d["live"] = self.live
        return d


class JobManager:
    def __init__(self, max_history: int = 60):
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()
        self._max_history = max_history

    def start(self, kind: str, session: str | None, fn: Callable[[Job], object],
              label: str | None = None) -> Job:
        with self._lock:
            for j in self._jobs.values():
                if j.kind == kind and j.session == session and j.status in ACTIVE:
                    raise JobConflict(f"a {kind} job is already running for {session}")
            job = Job(kind, session, label)
            self._jobs[job.id] = job
            self._prune()
        threading.Thread(target=self._run, args=(job, fn), name=f"job-{kind}-{job.id}", daemon=True).start()
        return job

    def _run(self, job: Job, fn: Callable[[Job], object]) -> None:
        job.status = "running"
        job.started = time.time()
        where = f" ({job.session})" if job.session else ""
        console.info("started: %s%s", job.label, where)
        try:
            job.result = fn(job)
            if job.cancelled:
                job.status = "cancelled"
                job.log("cancelled", "warn")
            else:
                job.status = "done"
                job.progress = 1.0
        except Cancelled:
            job.status = "cancelled"
            job.log("cancelled", "warn")
        except Exception as e:  # noqa: BLE001 - report everything to the UI
            job.status = "error"
            job.error = str(e) or e.__class__.__name__
            job.log(f"error: {job.error}", "error")
            for line in traceback.format_exc().rstrip().splitlines()[-12:]:
                job.log(line, "debug")
        finally:
            job.finished = time.time()
            took = _took(job.finished - job.started)
            if job.status == "done":
                console.info("done: %s%s in %s", job.label, where, took)
            elif job.status == "error":
                console.error("failed: %s%s after %s: %s", job.label, where, took, job.error)
            else:
                console.warning("cancelled: %s%s after %s", job.label, where, took)
            job.done_event.set()

    def _prune(self) -> None:
        done = [j for j in self._jobs.values() if j.status not in ACTIVE]
        if len(done) > self._max_history:
            for j in sorted(done, key=lambda x: x.created)[: len(done) - self._max_history]:
                self._jobs.pop(j.id, None)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, session: str | None = None, *, global_only: bool = False) -> list[Job]:
        """``session``: that session's jobs plus the global ones (session None); ``global_only``:
        just the global ones; neither: every job. Oldest first."""
        if global_only:
            jobs = [j for j in self._jobs.values() if j.session is None]
        elif session is not None:
            jobs = [j for j in self._jobs.values() if j.session in (session, None)]
        else:
            jobs = list(self._jobs.values())
        return sorted(jobs, key=lambda j: j.created)

    def active(self, session: str | None, kind: str | None = None) -> list[Job]:
        return [j for j in self._jobs.values()
                if j.session == session and j.status in ACTIVE and (kind is None or j.kind == kind)]

    def cancel(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job is not None and job.status in ACTIVE:
            job.cancel_event.set()
        return job


def _took(seconds: float) -> str:
    return f"{seconds:.1f} s" if seconds < 90 else f"{int(seconds // 60)} min {int(seconds % 60):02d} s"
