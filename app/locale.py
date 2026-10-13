"""Works out the home country, which the watermark builder leaves off a place
("Quepos, Puntarenas, Costa Rica" -> "Quepos, Puntarenas" at home). Home is
where this container lives: its time zone (TZ, /etc/timezone or /etc/localtime)
first, then the region of its locale (LANG=en_US.UTF-8 -> United States). The
names come from the zone tables that ship with the system's time zone data.
Returns "" when nothing can be worked out, so callers just skip."""
from __future__ import annotations

import os
import re

_DIRS = ("/usr/share/zoneinfo", "/usr/share/lib/zoneinfo", "/usr/lib/zoneinfo")
_ZONE = re.compile(r"^[A-Za-z0-9_+\-]+(/[A-Za-z0-9_+\-]+){0,2}$")
_REGION = re.compile(r"^[a-z]{2,3}_([A-Z]{2})(?:[.@].*)?$")
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


def country_for_region(code: str) -> str:
    """The English country name for a two-letter region code ("US")."""
    return _tables()["countries"].get(code, "") if isinstance(code, str) else ""


def timezone_name(env=None, readlink=os.readlink) -> str:
    """This machine's IANA time zone name, or ""."""
    env = os.environ if env is None else env
    zone = (env.get("TZ") or "").lstrip(":")
    if zone:
        return zone
    try:
        with open("/etc/timezone", encoding="utf-8") as f:
            zone = f.read().strip()
        if zone:
            return zone
    except OSError:
        pass
    try:
        target = readlink("/etc/localtime")
    except OSError:
        return ""
    return target.split("zoneinfo/", 1)[1] if "zoneinfo/" in target else ""


def container_country(env=None, zone: str | None = None) -> str:
    """The country this container is in: its time zone's, else its locale's."""
    env = os.environ if env is None else env
    found = country_for_timezone(timezone_name(env) if zone is None else zone)
    if found:
        return found
    for key in ("LC_ALL", "LC_MESSAGES", "LANG"):
        m = _REGION.match(env.get(key) or "")
        if m:
            return country_for_region(m.group(1))
    return ""
