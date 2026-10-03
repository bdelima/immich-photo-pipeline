"""In-memory, process-local health signal for whether the container has a
working Claude Code session right now. Deliberately separate from
state.py: this is never persisted and never survives a restart -- it's
purely what /healthz and the background auth probe share.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass


@dataclass(frozen=True)
class HealthSnapshot:
    claude_auth_ok: bool = False
    last_error: str | None = None


class HealthStore:
    """Starts unhealthy on purpose: until the first auth probe completes,
    we don't know that a Claude session actually works, so the poll loop
    should not assume it does."""

    def __init__(self):
        self._lock = threading.Lock()
        self._snapshot = HealthSnapshot(claude_auth_ok=False, last_error="not checked yet")

    def set_auth_ok(self) -> None:
        with self._lock:
            self._snapshot = HealthSnapshot(claude_auth_ok=True, last_error=None)

    def set_auth_failed(self, error: str) -> None:
        with self._lock:
            self._snapshot = HealthSnapshot(claude_auth_ok=False, last_error=error)

    def snapshot(self) -> HealthSnapshot:
        with self._lock:
            return self._snapshot
