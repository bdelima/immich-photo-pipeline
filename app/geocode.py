"""Names the place at a pair of coordinates, for photos whose place Immich could
not work out (it only fills in a place from its own offline list of towns).

This asks OpenStreetMap's public Nominatim service, so the photo's coordinates
leave the container for that one request. Its usage rules are followed: one
request at a time, no faster than one a second, an identifying User-Agent, and
every answer is kept so a place is only asked for once."""
from __future__ import annotations

import logging
import threading
import time

import requests

from . import version

log = logging.getLogger(__name__)

URL = "https://nominatim.openstreetmap.org/reverse"
USER_AGENT = f"immich-photo-pipeline/{version.current()} (+https://github.com/bdelima/immich-photo-pipeline)"
MIN_GAP_SECONDS = 1.0
TIMEOUT_SECONDS = 8

# The nearest named settlement, from the most to the least specific.
_CITY_KEYS = ("city", "town", "village", "hamlet", "suburb", "municipality", "county")

_lock = threading.Lock()
_last_request = 0.0
_cache: dict[tuple[float, float], str] = {}


def place_from_address(address: dict) -> str:
    """"City, State, Country" (the shape Immich uses), leaving out what is missing."""
    city = next((address[k] for k in _CITY_KEYS if isinstance(address.get(k), str) and address[k].strip()), "")
    parts: list[str] = []
    for value in (city, address.get("state"), address.get("country")):
        value = value.strip() if isinstance(value, str) else ""
        if value and value not in parts:
            parts.append(value)
    return ", ".join(parts)


def reverse(lat, lon, get=None, sleep=time.sleep, clock=time.monotonic) -> str:
    """The place at these coordinates, or "" if there is none or the service
    could not be reached (nothing is cached then, so a later try can work)."""
    global _last_request
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in (lat, lon)):
        return ""
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return ""
    key = (round(lat, 3), round(lon, 3))
    with _lock:
        if key in _cache:
            return _cache[key]
        wait = MIN_GAP_SECONDS - (clock() - _last_request)
        if _last_request and wait > 0:
            sleep(wait)
        _last_request = clock()
        try:
            response = (get or requests.get)(
                URL, timeout=TIMEOUT_SECONDS, headers={"User-Agent": USER_AGENT},
                params={"format": "jsonv2", "lat": lat, "lon": lon, "zoom": 10, "addressdetails": 1, "accept-language": "en"},
            )
            response.raise_for_status()
            address = response.json().get("address")
            place = place_from_address(address) if isinstance(address, dict) else ""
        except (requests.RequestException, ValueError, AttributeError) as exc:
            log.warning("place lookup failed: %s", exc)
            return ""
        if place:
            _cache[key] = place
        return place
