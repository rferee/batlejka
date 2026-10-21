"""``session.json`` persistence: per-session locks, read-modify-write, atomic
replace, defaults, and name/id validation (no path traversal)."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from . import config
from .ffmpeg_tools import replace_with_retry

SESSION_VERSION = 1

_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_FID_RE = re.compile(r"^f_[0-9a-f]{10}$")
_RESERVED = {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)}


class InvalidName(ValueError):
    pass


class SessionNotFound(LookupError):
    pass


def validate_session_name(name) -> str:
    if not isinstance(name, str):
        raise InvalidName("session name must be a string")
    name = name.strip()
    if (not name or len(name) > 100 or _BAD_CHARS.search(name) or name.startswith(".")
            or name.endswith((".", " ")) or name.split(".")[0].upper() in _RESERVED):
        raise InvalidName(f"invalid session name: {name!r}")
    return name


def validate_fid(fid) -> str:
    if not isinstance(fid, str) or not _FID_RE.match(fid):
        raise InvalidName(f"invalid file id: {fid!r}")
    return fid


def file_id(rel: str) -> str:
    return "f_" + hashlib.sha1(rel.replace("\\", "/").encode("utf-8")).hexdigest()[:10]


def sanitize_filename(name: str, fallback: str = "untitled") -> str:
    """Make ``name`` safe as a single Windows path component."""
    s = _BAD_CHARS.sub("_", str(name or "")).strip().rstrip(". ")
    if not s:
        s = fallback
    if s.split(".")[0].upper() in _RESERVED:
        s = "_" + s
    return s[:120]


class SessionPaths:
    def __init__(self, name: str):
        name = validate_session_name(name)
        base = config.sessions_dir().resolve()
        root = (base / name).resolve()
        if root.parent != base:
            raise InvalidName(f"invalid session name: {name!r}")
        self.name = name
        self.root = root
        self.source = root / "source"
        self.output = root / "output"
        self.pre = root / ".preprocess"
        self.json = self.pre / "session.json"
        self.thumbs = self.pre / "thumbs"
        self.features = self.pre / "features"
        self.audio = self.pre / "audio"
        self.wave = self.pre / "wave"
        self.proxies = self.pre / "proxies"

    def ensure(self) -> "SessionPaths":
        for d in (self.pre, self.thumbs, self.features, self.audio, self.wave, self.proxies):
            d.mkdir(parents=True, exist_ok=True)
        return self

    def thumb(self, fid: str) -> Path:
        return self.thumbs / f"{fid}.jpg"

    def feature(self, fid: str) -> Path:
        return self.features / f"{fid}.npy"

    def audio_npy(self, fid: str, sr: int) -> Path:
        return self.audio / f"{fid}_{int(sr)}.npy"

    def wave_json(self, fid: str) -> Path:
        return self.wave / f"{fid}.json"

    def proxy(self, fid: str) -> Path:
        return self.proxies / f"{fid}.mp4"

    def proxy_tmp(self, fid: str) -> Path:
        return self.proxies / f"{fid}.mp4.tmp.mp4"

    def source_file(self, rel: str) -> Path:
        p = (self.source / rel).resolve()
        if self.source.resolve() not in p.parents:
            raise InvalidName(f"file outside source/: {rel!r}")
        return p

    def cache_files(self, fid: str) -> list[Path]:
        files = [self.thumb(fid), self.feature(fid), self.wave_json(fid), self.proxy(fid), self.proxy_tmp(fid)]
        if self.audio.is_dir():
            files += list(self.audio.glob(f"{fid}_*.npy"))
        return files


def default_mocap() -> dict:
    return {"profiles": [], "active_profile": None,
            "options": {"processes": None, "blender": False, "blender_exe": None, "params": {}}, "takes": {}}


def _migrate_mocap(mocap: dict) -> None:
    """Sessions from before calibration profiles had one ``mocap.calibration``: it becomes
    the first (active) profile and the old key goes (later versions mirrored the active
    profile there; nothing reads it now). Pure data — its TOML is copied away lazily, when
    another calibration of the same take would overwrite it (fmc.mocap)."""
    old = mocap.pop("calibration", None)
    if isinstance(mocap.get("profiles"), list):
        mocap.setdefault("active_profile", None)
        return
    mocap["profiles"] = []
    mocap["active_profile"] = None
    if not isinstance(old, dict) or not old.get("toml"):
        return
    sq = old.get("square_mm")
    name = f"{old.get('board') or 'board'}-{sq:g}mm" if isinstance(sq, (int, float)) else str(old.get("board") or "calibration")
    settings = {"take_id": old.get("take_id"), "board": old.get("board"), "square_mm": sq,
                "groundplane": bool(old.get("groundplane"))}
    mocap["profiles"].append({
        "id": "p1", "name": name, **settings, "status": "calibrated", "toml": old.get("toml"),
        "reprojection_error_px": old.get("reprojection_error_px"), "cameras": old.get("cameras") or [],
        "created_at": old.get("created_at"), "updated_at": old.get("created_at"), "error": None,
        "groundplane_result": old.get("groundplane_result"), "freemocap": old.get("freemocap"),
        "calibrated_with": dict(settings),
    })
    mocap["active_profile"] = "p1"


def default_export_options() -> dict:
    opts = copy.deepcopy(config.DEFAULT_EXPORT_OPTIONS)
    try:
        from .setup_check import preferred_encoder   # h264_nvenc when its GPU test passed
        opts["encoder"] = preferred_encoder()
    except Exception:  # noqa: BLE001 - never let a probe cache problem break loading a session
        pass
    return opts


def default_ui() -> dict:
    """What the UI remembers per session, so every browser sees the same: the camera whose
    audio plays in Cut Takes, its Preview toggle, and the takes ticked on the Mocap step."""
    return {"audio_camera": None, "preview": False, "mocap_selected": []}


def default_session(name: str) -> dict:
    return {
        "version": SESSION_VERSION,
        "name": name,
        "settings": copy.deepcopy(config.DEFAULT_SETTINGS),
        "files": [],
        "cameras": [],
        "takes": [],
        "sync": {},
        "export_options": default_export_options(),
        "mocap": default_mocap(),
        "ui": default_ui(),
        "state": {"scanned_at": None, "processed_at": None, "synced_at": None, "exported_at": None},
    }


def _normalize(data: dict, name: str) -> dict:
    base = default_session(name)
    for key, value in base.items():
        if key not in data or data[key] is None:
            data[key] = value
    for key in ("settings", "export_options", "state", "ui"):
        merged = copy.deepcopy(base[key])
        merged.update(data.get(key) or {})
        data[key] = merged
    mocap = data["mocap"]
    mocap["options"] = {**default_mocap()["options"], **(mocap.get("options") or {})}
    mocap.setdefault("takes", {})
    _migrate_mocap(mocap)
    data["name"] = name
    for f in data["files"]:
        f.setdefault("assets", {})
        for k in ("thumb", "features", "audio", "wave", "proxy"):
            f["assets"].setdefault(k, False)
        f.setdefault("camera", None)
        f.setdefault("offset", None)
        f.setdefault("sync", {"method": "none"})
        f.setdefault("probe", None)
    return data


def write_json_atomic(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=1, ensure_ascii=False)
        fh.flush()
        os.fsync(fh.fileno())
    replace_with_retry(tmp, path)


class SessionStore:
    def __init__(self):
        self._locks: dict[str, threading.RLock] = {}
        self._guard = threading.Lock()

    def lock(self, name: str) -> threading.RLock:
        with self._guard:
            return self._locks.setdefault(name, threading.RLock())

    def paths(self, name: str) -> SessionPaths:
        return SessionPaths(name)

    def exists(self, name: str) -> bool:
        return self.paths(name).root.is_dir()

    def create(self, name: str) -> SessionPaths:
        p = self.paths(name)
        p.source.mkdir(parents=True, exist_ok=True)
        p.output.mkdir(parents=True, exist_ok=True)
        return p

    def load(self, name: str) -> dict:
        p = self.paths(name)
        if not p.root.is_dir():
            raise SessionNotFound(name)
        with self.lock(p.name):
            if p.json.exists():
                with open(p.json, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
            else:
                data = default_session(p.name)
        return _normalize(data, p.name)

    def save(self, name: str, data: dict) -> None:
        p = self.paths(name)
        with self.lock(p.name):
            p.ensure()
            write_json_atomic(p.json, data)

    @contextmanager
    def edit(self, name: str):
        """Read-modify-write under the per-session lock."""
        p = self.paths(name)
        with self.lock(p.name):
            data = self.load(p.name)
            yield data
            self.save(p.name, data)

    def list_sessions(self) -> list[dict]:
        base = config.sessions_dir()
        out = []
        if not base.is_dir():
            return out
        for d in sorted(base.iterdir(), key=lambda x: x.name.lower()):
            if not d.is_dir() or d.name.startswith("."):
                continue
            try:
                validate_session_name(d.name)
            except InvalidName:
                continue
            n_files, size = 0, 0
            src = d / "source"
            if src.is_dir():
                for root, dirs, files in os.walk(src):
                    dirs[:] = [x for x in dirs if not x.startswith(".")]
                    for fn in files:
                        if os.path.splitext(fn)[1].lower() in config.VIDEO_EXTS:
                            n_files += 1
                            try:
                                size += os.path.getsize(os.path.join(root, fn))
                            except OSError:
                                pass
            state = {}
            sj = d / ".preprocess" / "session.json"
            if sj.exists():
                try:
                    with open(sj, "r", encoding="utf-8") as fh:
                        state = (json.load(fh).get("state") or {})
                except (OSError, ValueError):
                    state = {}
            out.append({
                "name": d.name,
                "n_source_files": n_files,
                "size": size,
                "processed": bool(state.get("processed_at")),
                "synced": bool(state.get("synced_at")),
                "exported": bool(state.get("exported_at")),
                "mtime": d.stat().st_mtime,
            })
        return out


def now() -> float:
    return time.time()
