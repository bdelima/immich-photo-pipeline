"""The household Photos view: every household member's photos on one timeline.

The pipeline's account sees a member's whole library on its own timeline once that
member has added it as a partner (in Immich: Account settings, Sharing, Partners),
so a month of the timeline is one call. This module turns Immich's answer into what
the page draws, leaving out what the page should not show:

  - the pipeline account's own photos (its processed results and the Review and Live
    copies), which the library views already show and which would appear twice here;
  - trashed photos, and archived or locked ones (anything not on a normal timeline);
  - photos already in the pipeline (an original that is a source of a photo in the library),
    unless the page asks for them; those it gets a "pipeline" mark of "in" or "trash" (the
    processed photo is in the web trash).

Nothing here changes anything in Immich.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .immich_client import ImmichClient

_UUID = re.compile(r"^[0-9a-fA-F-]{32,36}$")
_BUCKET = re.compile(r"^\d{4}-\d{2}-\d{2}(T[\d:.]+Z?)?$")
DEFAULT_RATIO = 1.5

IN_PIPELINE = "in"          # a photo made from this original is in the pipeline
IN_PIPELINE_TRASH = "trash"  # only photos in the web trash were made from it (a restore brings it back)


def pipeline_states(library) -> dict[str, str]:
    """{original's asset id: "in" or "trash"} for every original the library knows.

    Worked out from the library each time, never stored, so it follows the library: it is
    gone when the last photo made from the original is deleted for good. Trashing a photo
    keeps its originals "known" (a restore brings it back), so they show as "trash"."""
    out: dict[str, str] = {}
    for photo in library.photos.values():
        for source in photo.sources:
            if not source.asset_id:
                continue
            if not photo.trashed:
                out[source.asset_id] = IN_PIPELINE
            else:
                out.setdefault(source.asset_id, IN_PIPELINE_TRASH)
    return out


def photos_made_from(library, asset_ids) -> list:
    """The library photos, trashed or not, that have any of these assets as a source."""
    wanted = set(asset_ids)
    return [p for p in library.photos.values() if any(s.asset_id in wanted for s in p.sources)]


def valid_asset_id(value: str) -> bool:
    """Asset ids are UUIDs; anything else is refused before it can reach an Immich URL."""
    return bool(_UUID.match(value or ""))


def valid_bucket(value: str) -> bool:
    return bool(_BUCKET.match(value or ""))


def months(immich: ImmichClient) -> list[dict]:
    """[{"bucket": "2026-10-01T00:00:00.000Z", "month": "2026-10", "count": 412}, ...], newest first."""
    out = []
    for b in immich.timeline_buckets():
        stamp = str(b["timeBucket"])
        out.append({"bucket": stamp, "month": stamp[:7], "count": int(b.get("count") or 0)})
    out.sort(key=lambda m: m["bucket"], reverse=True)
    return out


def _local(created, offset_hours) -> tuple[str, str]:
    """(day, clock) -- "2026-10-17", "16:41" -- on the wall clock where the photo was taken.
    Immich gives the time in UTC and the local offset in hours."""
    try:
        when = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
    except ValueError:
        return "", ""
    if when.tzinfo is None:
        when = when.replace(tzinfo=timezone.utc)
    if isinstance(offset_hours, (int, float)) and not isinstance(offset_hours, bool):
        when = when + timedelta(hours=offset_hours)
    return when.strftime("%Y-%m-%d"), when.strftime("%H:%M")


def _seconds(duration) -> int:
    """A video's length in whole seconds. Immich 1.133 and later give milliseconds as a
    number (null for a photo); older versions wrote "h:mm:ss.fff", where "0:00:00.00000"
    means none."""
    if isinstance(duration, bool):
        return 0
    if isinstance(duration, (int, float)):
        return max(0, int(duration // 1000))
    if not isinstance(duration, str):
        return 0
    try:
        h, m, s = duration.split(":")
        return int(float(h) * 3600 + float(m) * 60 + float(s))
    except ValueError:
        return 0


def month_items(
    immich: ImmichClient, bucket: str, own_id: str, states: dict[str, str] | None = None, include_sent: bool = False,
) -> list[dict]:
    """One month's photos and videos for the page, newest first. `states` is pipeline_states();
    photos already in the pipeline are left out unless `include_sent`."""
    states = states or {}
    items = []
    for a in immich.timeline_bucket(bucket):
        if not a.get("id") or a.get("isTrashed") or a.get("ownerId") == own_id:
            continue
        if a.get("visibility") not in (None, "timeline"):
            continue
        mark = states.get(a["id"], "")
        if mark and not include_sent:
            continue
        day, clock = _local(a.get("fileCreatedAt"), a.get("localOffsetHours"))
        ratio = a.get("ratio")
        items.append({
            "id": a["id"], "owner_id": a.get("ownerId") or "",
            "ratio": round(float(ratio), 4) if isinstance(ratio, (int, float)) and ratio > 0 else DEFAULT_RATIO,
            "video": bool(a.get("isVideo")), "seconds": _seconds(a.get("duration")) if a.get("isVideo") else 0,
            "day": day, "time": clock, "pipeline": mark,
        })
    items.sort(key=lambda i: (i["day"], i["time"]), reverse=True)
    return items


def name_owners(immich: ImmichClient, items: list[dict]) -> None:
    """Adds each item's owner's display name (looked up once per account)."""
    for item in items:
        item["owner"] = immich.get_user_name(item["owner_id"]) if item["owner_id"] else ""


# ---- search ---------------------------------------------------------------------------

# How the words typed in the search box are read: (Immich search, field the words go in).
SEARCH_MODES = {
    "context": ("smart", "query"),            # what the photo looks like
    "file_name": ("metadata", "originalFileName"),
    "description": ("metadata", "description"),
    "path": ("metadata", "originalPath"),
    "ocr": ("metadata", "ocr"),                # text in the picture
}
PAGE_SIZE = 200
_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SUGGESTION_KINDS = ("country", "state", "city", "camera-make", "camera-model", "camera-lens-model")


def search_body(text: str, mode: str, filters: dict, page: int) -> tuple[str, dict]:
    """(kind, request body) for Immich's search. `filters` may hold: from, to (YYYY-MM-DD),
    country, state, city, make, model, lens, type ("photo" or "video"), people (asset ids of
    people), favorites, archive, not_in_album (booleans). Anything else is ignored; a bad
    date is ignored too. Raises ValueError for an unknown mode."""
    if mode not in SEARCH_MODES:
        raise ValueError(f"unknown search type {mode!r}")
    kind, field = SEARCH_MODES[mode]
    body: dict = {"page": max(1, int(page)), "size": PAGE_SIZE, "withExif": True}
    text = (text or "").strip()
    if text:
        body[field] = text
    elif kind == "smart":
        kind = "metadata"          # nothing to look for by meaning: just the filters
    if _DAY.match(filters.get("from") or ""):
        body["takenAfter"] = filters["from"] + "T00:00:00.000Z"
    if _DAY.match(filters.get("to") or ""):
        body["takenBefore"] = filters["to"] + "T23:59:59.999Z"
    for key, name in (("country", "country"), ("state", "state"), ("city", "city"),
                      ("make", "make"), ("model", "model"), ("lens", "lensModel")):
        if (filters.get(key) or "").strip():
            body[name] = filters[key].strip()
    if filters.get("type") in ("photo", "video"):
        body["type"] = "IMAGE" if filters["type"] == "photo" else "VIDEO"
    people = [p for p in (filters.get("people") or []) if valid_asset_id(p)]
    if people:
        body["personIds"] = people
    if filters.get("favorites"):
        body["isFavorite"] = True
    if filters.get("archive"):
        body["visibility"] = "archive"
    if filters.get("not_in_album"):
        body["isNotInAlbum"] = True
    return kind, body


def _ratio(a: dict) -> float:
    exif = a.get("exifInfo") or {}
    w = a.get("width") or exif.get("exifImageWidth")
    h = a.get("height") or exif.get("exifImageHeight")
    if not isinstance(w, (int, float)) or not isinstance(h, (int, float)) or w <= 0 or h <= 0:
        return DEFAULT_RATIO
    if str(exif.get("orientation") or "") in ("5", "6", "7", "8"):       # stored sideways
        w, h = h, w
    return round(float(w) / float(h), 4)


def _search_day(a: dict) -> tuple[str, str]:
    local = str(a.get("localDateTime") or "")
    if re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", local):          # the wall clock where it was taken
        return local[:10], local[11:16]
    return _local(a.get("fileCreatedAt"), None)


def search_items(
    immich: ImmichClient, own_id: str, text: str, mode: str, filters: dict, page: int = 1,
    states: dict[str, str] | None = None, include_sent: bool = False, owner_id: str = "",
) -> tuple[list[dict], int | None]:
    """One page of search results in the shape of month_items (newest first within the page),
    and the next page number or None. Leaves out what the timeline leaves out: the pipeline's
    own photos, the trashed, and (unless asked for) photos already in the pipeline. `owner_id`
    keeps only that account's photos."""
    states = states or {}
    kind, body = search_body(text, mode, filters, page)
    found = immich.search_assets(kind, body)
    items = []
    for a in found["items"]:
        if not a.get("id") or a.get("isTrashed") or a.get("ownerId") == own_id:
            continue
        if owner_id and a.get("ownerId") != owner_id:
            continue
        mark = states.get(a["id"], "")
        if mark and not include_sent:
            continue
        video = a.get("type") == "VIDEO"
        day, clock = _search_day(a)
        items.append({
            "id": a["id"], "owner_id": a.get("ownerId") or "", "ratio": _ratio(a), "video": video,
            "seconds": _seconds(a.get("duration")) if video else 0, "day": day, "time": clock, "pipeline": mark,
        })
    items.sort(key=lambda i: (i["day"], i["time"]), reverse=True)
    return items, found["next"]
