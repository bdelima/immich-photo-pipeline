"""Shared bind-mounted-file-wins-over-env-var resolution for every secret
this container needs: the Claude Pro OAuth token and the primary/"master"
Immich API key both follow this same rule, so rotating either credential
is "overwrite the mounted file," never "edit compose and restart" -- and
neither value needs to sit in `docker inspect` output.

Pulled out as its own module (rather than duplicated once per secret) so
the Immich API key can converge on exactly the same resolution behavior
the Claude token already uses, instead of drifting into two slightly
different implementations.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def resolve_secret(file_path: str, env_value: str | None) -> str | None:
    """A non-empty bind-mounted file always wins over the env var. Falls
    back to the env var when the file is missing, unreadable, or empty
    (whitespace-only counts as empty), and returns None when neither is
    set -- the caller decides whether that's fatal."""
    if file_path and os.path.isfile(file_path):
        try:
            with open(file_path, "r", encoding="utf-8") as fh:
                content = fh.read().strip()
        except OSError as exc:
            log.warning("could not read %s: %s", file_path, exc)
            content = ""
        if content:
            return content
    stripped = (env_value or "").strip()
    return stripped or None
