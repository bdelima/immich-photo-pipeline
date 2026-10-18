"""The household Photos view: every household member's photos on one timeline.

The pipeline's account sees a member's whole library on its own timeline once that
member has added it as a partner (in Immich: Account settings, Sharing, Partners),
so a month of the timeline is one call. This module turns Immich's answer into what
the page draws, leaving out what the page should not show:

  - the pipeline account's own photos (its processed results and the Review and Live
    copies), which the library views already show and which would appear twice here;
  - trashed photos, and archived or locked ones (anything not on a normal timeline).

Nothing here changes anything in Immich.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from .immich_client import ImmichClient

_UUID = re.compile(r"^[0-9a-fA-F-]{32,36}$")
_BUCKET = re.compile(r"^\d{4}-\d{2}-\d{2}(T[\d:.]+Z?)?$")
DEFAULT_RATIO = 1.5


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
    """"0:00:12.345" -> 12. Immich writes a video's length as h:mm:ss.fff; "0:00:00.00000" means none."""
    if not isinstance(duration, str):
        return 0
    try:
        h, m, s = duration.split(":")
        return int(float(h) * 3600 + float(m) * 60 + float(s))
    except ValueError:
        return 0


def month_items(immich: ImmichClient, bucket: str, own_id: str) -> list[dict]:
    """One month's photos and videos for the page, newest first."""
    items = []
    for a in immich.timeline_bucket(bucket):
        if not a.get("id") or a.get("isTrashed") or a.get("ownerId") == own_id:
            continue
        if a.get("visibility") not in (None, "timeline"):
            continue
        day, clock = _local(a.get("fileCreatedAt"), a.get("localOffsetHours"))
        ratio = a.get("ratio")
        items.append({
            "id": a["id"], "owner_id": a.get("ownerId") or "",
            "ratio": round(float(ratio), 4) if isinstance(ratio, (int, float)) and ratio > 0 else DEFAULT_RATIO,
            "video": bool(a.get("isVideo")), "seconds": _seconds(a.get("duration")) if a.get("isVideo") else 0,
            "day": day, "time": clock,
        })
    items.sort(key=lambda i: (i["day"], i["time"]), reverse=True)
    return items


def name_owners(immich: ImmichClient, items: list[dict]) -> None:
    """Adds each item's owner's display name (looked up once per account)."""
    for item in items:
        item["owner"] = immich.get_user_name(item["owner_id"]) if item["owner_id"] else ""
