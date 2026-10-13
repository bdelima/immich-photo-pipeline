"""Works out a home country from a time zone name (such as "America/Chicago"),
using the zone tables that ship with the system's time zone data. Returns ""
when the tables are missing or the zone is unknown, so callers just skip."""
from __future__ import annotations

import os
import re

_DIRS = ("/usr/share/zoneinfo", "/usr/share/lib/zoneinfo", "/usr/lib/zoneinfo")
_ZONE = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")
_cache: dict[str, dict[str, str]] = {}


def _rows(name: str) -> list[list[str]]:
    for d in _DIRS:
        path = os.path.join(d, name)
        try:
            with open(path, encoding="utf-8") as f:
                return [line.rstrip("\n").split("\t") for line in f if line.strip() and not line.startswith("#")]
        except OSError:
            continue
    return []


def _tables() -> dict[str, dict[str, str]]:
    if not _cache:
        _cache["countries"] = {r[0]: r[1] for r in _rows("iso3166.tab") if len(r) >= 2}
        _cache["zones"] = {r[2]: r[0].split(",")[0] for r in _rows("zone.tab") if len(r) >= 3}
    return _cache


def country_for_timezone(zone: str) -> str:
    """The English country name for an IANA time zone ("" if unknown)."""
    if not isinstance(zone, str) or not _ZONE.match(zone):
        return ""
    tables = _tables()
    return tables["countries"].get(tables["zones"].get(zone, ""), "")
