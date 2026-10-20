"""Process-wide singletons shared by the API modules (server.py and the routes_* routers)."""
from __future__ import annotations

from .jobs import JobManager
from .session_store import SessionNotFound, SessionStore, validate_session_name

store = SessionStore()
jobs = JobManager()


def require_session(s: str) -> str:
    """Validated name of an existing session (raises InvalidName / SessionNotFound)."""
    name = validate_session_name(s)
    if not store.exists(name):
        raise SessionNotFound(name)
    return name
