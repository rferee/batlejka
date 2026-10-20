"""Run ``fmc_headless.py`` inside the FreeMoCap venv.

Its stdout and stderr are teed to ``.tools/freemocap-<v>/logs/<timestamp>_<cmd>.log``,
ANSI codes are stripped, and every line is parsed: ``FMC_EVENT {json}`` (progress
events), ``FMC_RESULT {json}`` (always the last line) and tqdm bars on stderr
(``processing video: <cam>.mp4:  42%|...| 93/222``, one per camera, interleaved when
several tracking processes run). Cancel kills the whole process tree, because 2D
tracking spawns worker processes.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

from .. import config
from ..ffmpeg_tools import Cancelled, kill_tree

SCRIPT = config.FMC_PACKAGE_DIR / "fmc_headless.py"

_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b[@-Z\\-_]")
TQDM = re.compile(r"processing video: (?P<name>.+?\.mp4):\s+(?P<pct>\d+)%\|.*?\|\s*(?P<n>\d+)/(?P<total>\d+)")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def parse_line(line: str) -> tuple[str, object]:
    """One line of output (ANSI already stripped) as one of
    ``("event", dict)``, ``("result", dict)``, ``("tqdm", {name, pct, n, total})``, ``("text", str)``."""
    for prefix, kind in (("FMC_EVENT ", "event"), ("FMC_RESULT ", "result")):
        if line.startswith(prefix):
            try:
                value = json.loads(line[len(prefix):])
            except ValueError:
                return "text", line
            if isinstance(value, dict):
                return kind, value
            return "text", line
    m = TQDM.search(line)
    if m:
        return "tqdm", {"name": m["name"], "pct": int(m["pct"]), "n": int(m["n"]), "total": int(m["total"])}
    return "text", line


class Bars:
    """The per-camera tqdm bars of one phase (board detection, or 2D tracking of one take)."""

    def __init__(self):
        self.bars: dict[str, tuple[int, int]] = {}

    def reset(self) -> None:
        self.bars.clear()

    def update(self, bar: dict) -> None:
        self.bars[bar["name"]] = (int(bar["n"]), max(1, int(bar["total"])))

    def fraction(self, expected: int | None = None) -> float:
        """Mean progress over the cameras; cameras without a bar yet count as 0."""
        if not self.bars:
            return 0.0
        done = sum(min(1.0, n / total) for n, total in self.bars.values())
        return done / max(len(self.bars), expected or 0)


def describe_event(ev: dict) -> tuple[str, str] | None:
    """A job-log line (message, level) for an event, or None for chatty ones."""
    t = ev.get("type")
    take = ev.get("take")
    if t == "take_start":
        return f"{take}: starting ({int(ev.get('index', 0)) + 1}/{ev.get('total', '?')})", "info"
    if t == "take_done":
        extra = " with a subset calibration" if ev.get("subset") else ""
        return f"{take}: done in {ev.get('seconds', '?')} s ({len(ev.get('cameras') or [])} cameras{extra})", "done"
    if t == "take_failed":
        return f"{take}: failed — {ev.get('error')}", "error"
    if t == "calibration_done":
        err = ev.get("reprojection_error_px")
        err_s = f"{err:.2f} px" if isinstance(err, (int, float)) else "unknown"
        return f"calibration done: reprojection error {err_s}, cameras {', '.join(ev.get('cameras') or [])}", "done"
    if t == "warn":
        return f"{take + ': ' if take else ''}{ev.get('message')}", "warn"
    if t == "stage":
        return f"{take or ev.get('scope', '')}: {ev.get('stage')}", "info"
    return None


def venv_dir() -> Path:
    return config.fmc_dir() / "venv"


def venv_python() -> Path:
    v = venv_dir()
    return v / "Scripts" / "python.exe" if os.name == "nt" else v / "bin" / "python"


def headless_argv(cmd: str, *args) -> list[str]:
    """``python -I fmc_headless.py --fmc-home <home> <cmd> ...``. ``-I`` ignores PYTHON*
    variables, so unbuffered output and UTF-8 also come from ``-u`` and ``-X utf8``."""
    home = config.fmc_dir() / "home"
    return [str(venv_python()), "-I", "-u", "-X", "utf8", str(SCRIPT), "--fmc-home", str(home), cmd,
            *[str(a) for a in args]]


# Spawned tracking workers re-import freemocap/skellytracker, which resets their console
# logging to DEBUG on stdout; those lines are noise in the job log (the log file keeps them).
_FMC_LOG = re.compile(r"\[\s*(TRACE|DEBUG|INFO|WARNING|SUCCESS)\]| - (DEBUG|INFO|WARNING) - "
                      r"|^(Thank you for using |This is printing from: |Source code for this package |adding base_package_path: )")


def _text_level(line: str, stream: str) -> str:
    if line.startswith("[warn]"):
        return "warn"
    if stream == "stdout" and not _FMC_LOG.search(line):
        return "info"
    return "debug"


def run_headless(
    cmd: str,
    args: list | tuple = (),
    job=None,
    on_event: Callable[[dict], None] | None = None,
    on_tqdm: Callable[[dict], None] | None = None,
    log_text: bool = True,
) -> dict:
    """Run one fmc_headless.py command to the end and return its ``FMC_RESULT`` dict
    (``ok`` plus the command's fields), with ``returncode`` and ``log`` (the log file) added.

    Events and text lines go to ``job.log``; ``on_event`` / ``on_tqdm`` are called (one at
    a time) for every event / tqdm update. Raises :class:`Cancelled` if ``job`` was cancelled.
    A run that ends without ``FMC_RESULT`` (crash, killed) returns ``ok: False`` with the
    tail of its output as ``error``.
    """
    python = venv_python()
    if not python.is_file():
        raise RuntimeError(f"FreeMoCap isn't installed ({python} is missing) — install it on the Session screen")
    fdir = config.fmc_dir()
    (fdir / "home").mkdir(parents=True, exist_ok=True)
    logs = fdir / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    log_path = logs / f"{time.strftime('%Y%m%d-%H%M%S')}_{cmd}.log"
    argv = headless_argv(cmd, *args)
    env = dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")

    state = {"result": None}
    tail: deque[str] = deque(maxlen=40)
    lock = threading.Lock()
    cancelled = threading.Event()

    with open(log_path, "w", encoding="utf-8", errors="replace") as log:
        log.write("$ " + subprocess.list2cmdline(argv) + "\n")
        log.flush()
        if job is not None:
            job.log(f"FreeMoCap {cmd} (log: {log_path})", "debug")
        proc = subprocess.Popen(
            argv, cwd=str(fdir), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, encoding="utf-8", errors="replace", bufsize=1,
            creationflags=config.CREATE_NO_WINDOW,
        )

        def handle(raw: str, stream: str) -> None:
            line = strip_ansi(raw).rstrip("\r\n")
            kind, value = parse_line(line)
            if kind == "result":
                state["result"] = value
            elif kind == "event":
                if job is not None:
                    described = describe_event(value)
                    if described:
                        job.log(*described)
                if on_event:
                    on_event(value)
            elif kind == "tqdm":
                if on_tqdm:
                    on_tqdm(value)
            elif line.strip():
                tail.append(line)
                if job is not None and log_text:
                    job.log(line, _text_level(line, stream))

        def pump(pipe, stream: str) -> None:
            for raw in pipe:
                with lock:
                    log.write(raw if raw.endswith("\n") else raw + "\n")
                    try:
                        handle(raw, stream)
                    except Exception as e:  # noqa: BLE001 - a bad callback must not stop the pump
                        if job is not None:
                            job.log(f"output handler failed: {e!r}", "warn")

        pumps = [threading.Thread(target=pump, args=(proc.stdout, "stdout"), daemon=True),
                 threading.Thread(target=pump, args=(proc.stderr, "stderr"), daemon=True)]
        for t in pumps:
            t.start()
        while proc.poll() is None:
            if job is not None and job.cancel_event.wait(0.25):
                cancelled.set()
                kill_tree(proc)
                break
            if job is None:
                time.sleep(0.25)
        rc = proc.wait()
        for t in pumps:
            t.join(timeout=10)
        with lock:
            log.write(f"[exit code {rc}]\n")

    if cancelled.is_set():
        raise Cancelled()
    result = state["result"]
    if result is None:
        last = " | ".join(list(tail)[-6:])
        result = {"ok": False, "error": f"FreeMoCap runner exited with code {rc} without a result: {last}"}
    result["returncode"] = rc
    result["log"] = str(log_path)
    return result
