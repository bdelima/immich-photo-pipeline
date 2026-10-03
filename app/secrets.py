"""Parses the single shared secrets file this whole Immich
pipeline/display-integrations ecosystem uses: simple KEY=VALUE lines, one
assignment per line, so one bind-mounted file can carry every credential
these containers need (immich-photo-pipeline, plus the sibling
immich-overflight-feed/immich-frame-mirror in
bdelima/immich-display-integrations), with each component pulling out
only the key(s) it actually uses and leaving the rest alone.

Keys this component (immich-photo-pipeline) reads from it:
    CLAUDE_CODE_OAUTH_TOKEN   (this component only)
    IMMICH_API_KEY            (shared with overflight-feed/frame-mirror)
    IMMICH_EXTRA_API_KEY      (this component only; repeatable, one line
                               per extra household account)

A component that doesn't recognize a key just never asks for it -- there's
no validation here that rejects unknown keys, since other components are
expected to have their own keys living in the same file.
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)


def parse_secrets_file(path: str) -> dict[str, list[str]]:
    """KEY=VALUE per line; '#' comments, blank lines, and lines with no
    '=' are skipped. A key may repeat (e.g. IMMICH_EXTRA_API_KEY once per
    household account); all of its values are collected in file order.
    Returns {} if the file is missing or unreadable -- every caller below
    falls back to its own env var in that case, so a missing shared file
    is never fatal here."""
    if not path or not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw_lines = fh.readlines()
    except OSError as exc:
        log.warning("could not read %s: %s", path, exc)
        return {}
    values: dict[str, list[str]] = {}
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            values.setdefault(key, []).append(value)
    return values


def resolve_secret(path: str, key: str, env_value: str | None) -> str | None:
    """The shared file's first value for `key` wins; otherwise falls back
    to env_value (whitespace-only counts as unset); None if neither is
    set -- the caller decides whether that's fatal. Rotating a credential
    is "edit the one shared file", never "edit compose and restart"."""
    values = parse_secrets_file(path).get(key)
    if values:
        return values[0]
    stripped = (env_value or "").strip()
    return stripped or None


def resolve_secret_list(path: str, key: str) -> list[str]:
    """Every value for a repeatable key (IMMICH_EXTRA_API_KEY), in file
    order. No env-var fallback -- a list has no single-value env
    equivalent, so nothing configured just means an empty list."""
    return parse_secrets_file(path).get(key, [])
