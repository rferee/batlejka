"""Background job manager (R5): each job runs in its own thread and reports
progress, a log, and honours cancel requests."""
from __future__ import annotations

import threading
import time
import traceback
import uuid
from collections import deque
from typing import Callable

from .ffmpeg_tools import Cancelled

ACTIVE = ("queued", "running")


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

    # -- used by job functions -------------------------------------------
    def log(self, msg: str, level: str = "info") -> None:
        with self._lock:
            self._seq += 1
            self._log.append({"seq": self._seq, "t": time.time(), "level": level, "msg": str(msg)})

    def set_progress(self, fraction: float, message: str | None = None) -> None:
        self.progress = float(min(1.0, max(0.0, fraction)))
        if message is not None:
            self.message = message

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
            else:
                d["log_tail"] = list(self._log)[-3:]
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
            job.done_event.set()

    def _prune(self) -> None:
        done = [j for j in self._jobs.values() if j.status not in ACTIVE]
        if len(done) > self._max_history:
            for j in sorted(done, key=lambda x: x.created)[: len(done) - self._max_history]:
                self._jobs.pop(j.id, None)

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self, session: str | None = None) -> list[Job]:
        jobs = [j for j in self._jobs.values() if session is None or j.session == session]
        return sorted(jobs, key=lambda j: j.created)

    def active(self, session: str | None, kind: str | None = None) -> list[Job]:
        return [j for j in self._jobs.values()
                if j.session == session and j.status in ACTIVE and (kind is None or j.kind == kind)]

    def cancel(self, job_id: str) -> Job | None:
        job = self._jobs.get(job_id)
        if job is not None and job.status in ACTIVE:
            job.cancel_event.set()
        return job
