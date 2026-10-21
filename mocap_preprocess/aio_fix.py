"""Windows asyncio fix for connections the browser has already dropped.

The proactor event loop closes a connection with ``socket.shutdown()`` first. When the
browser already reset it (an aborted video request: a seek, a tile switching source,
which Cut Takes does constantly), that raises WinError 10054 out of
``_call_connection_lost``: a traceback on the console for every one, and the socket's
``close()`` right after it never runs. ``install()`` swaps in the same method with that one
error caught.
"""
from __future__ import annotations

import socket
import sys


def _call_connection_lost(self, exc):
    if self._called_connection_lost:
        return
    try:
        self._protocol.connection_lost(exc)
    finally:
        if hasattr(self._sock, "shutdown") and self._sock.fileno() != -1:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:          # the other end already reset it: just close
                pass
        self._sock.close()
        self._sock = None
        server = self._server
        if server is not None:
            server._detach(self)
            self._server = None
        self._called_connection_lost = True


def install() -> bool:
    """Patch the proactor transport (Windows only). True when it's in place."""
    if sys.platform != "win32":
        return False
    from asyncio import proactor_events

    proactor_events._ProactorBasePipeTransport._call_connection_lost = _call_connection_lost
    return True
