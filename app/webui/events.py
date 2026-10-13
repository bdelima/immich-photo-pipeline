"""Tells the open page when the library has changed, so it can update the parts
that changed (the counts, the thumbnails) without being reloaded.

The library is one JSON file that every writer, in this process or another,
replaces atomically, so a change shows up as a new modification time, size or
file. The page holds one Server-Sent Events connection open; this module
watches the file and sends a short token each time it changes (and once when
the connection opens, so a page that reconnects can tell it missed something).
"""
from __future__ import annotations

import os
import time

POLL_SECONDS = 1.0
HEARTBEAT_SECONDS = 15.0


def token(path: str) -> str:
    """Names the current state of the library file; it differs after any write."""
    try:
        st = os.stat(path)
    except OSError:
        return "none"
    return f"{st.st_ino}-{st.st_mtime_ns}-{st.st_size}"


def stream(path: str, poll=None, heartbeat=None, sleep=time.sleep, clock=time.monotonic, alive=lambda: True):
    """Yields Server-Sent Events text: the current token first, then a new one
    whenever the file changes, with a comment line now and then so a proxy
    does not close an idle connection. Stops when `alive()` is false."""
    poll = POLL_SECONDS if poll is None else poll
    heartbeat = HEARTBEAT_SECONDS if heartbeat is None else heartbeat
    seen = token(path)
    yield f"retry: 3000\ndata: {seen}\n\n"
    quiet_since = clock()
    while alive():
        sleep(poll)
        now = token(path)
        if now != seen:
            seen = now
            quiet_since = clock()
            yield f"data: {now}\n\n"
        elif clock() - quiet_since >= heartbeat:
            quiet_since = clock()
            yield ": keep-alive\n\n"
