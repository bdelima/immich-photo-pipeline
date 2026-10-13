"""Which release is running.

The answer comes from the VERSION file the image carries (the same file the
release workflow tags from), not from an environment variable: a container
that is recreated from its old configuration keeps the old ENV values even
with a new image, so APP_VERSION can name an earlier release than the code
actually running. The file is part of the image and cannot go stale that way.
APP_VERSION is only the fallback, and "dev" is what a plain checkout without
either reports.
"""
from __future__ import annotations

import os

VERSION_FILE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "VERSION")
MAX_LEN = 40


def _clean(text: str) -> str:
    lines = text.strip().splitlines()
    first = lines[0].strip() if lines else ""
    return first if 0 < len(first) <= MAX_LEN else ""


def current(path: str | None = None, env: dict | None = None) -> str:
    try:
        with open(path or VERSION_FILE, encoding="utf-8") as fh:
            found = _clean(fh.read(1024))
    except OSError:
        found = ""
    if found:
        return found
    return _clean((os.environ if env is None else env).get("APP_VERSION", "")) or "dev"
