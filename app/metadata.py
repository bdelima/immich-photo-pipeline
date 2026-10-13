"""What Immich knows about an original -- when it was taken, where, and whose
account it came from -- copied onto the photo when the original is copied.

The original belongs to a contributor and may be deleted from Immich at any
time, so the details are kept with the photo (on its `Source`) rather than
looked up when needed. That also means a later step, such as a watermark, can
use them without Immich being reachable.

Nothing here is pipeline data; it is read from Immich's record of the asset.
Photos that were copied before this existed are filled in by `backfill`, for
as long as their originals are still there to ask about.
"""
from __future__ import annotations

import logging
import re
from typing import Sequence

from .immich_client import ImmichClient
from .library import LibraryStore, Source

log = logging.getLogger(__name__)

UNAVAILABLE = {"unavailable": True}
# How many originals one cycle looks up when filling in older photos.
BACKFILL_PER_CYCLE = 10

_CLOCK = re.compile(r"^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})")


def _clock(raw) -> str:
    """"2024-03-02T14:05:09.000Z" -> "2024-03-02 14:05". Immich's local time
    is the clock on the wall where the photo was taken, so it is used as it
    is, without converting from a time zone."""
    m = _CLOCK.match(raw) if isinstance(raw, str) else None
    return f"{m.group(1)} {m.group(2)}" if m else ""


def _number(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def meta_from_asset(item: dict) -> dict:
    """Date, place and owner from an asset as Immich describes it (GET
    /assets/{id}). Keys with nothing to say are left out."""
    exif = item.get("exifInfo") or {}
    out: dict = {}
    if exif.get("dateTimeOriginal"):
        taken, kind = _clock(item.get("localDateTime") or exif["dateTimeOriginal"]), "captured"
    else:
        # No capture date (a screenshot, an edited file): the file's own date,
        # which is not necessarily when the photo was taken.
        taken, kind = _clock(item.get("fileCreatedAt")), "file"
    if taken:
        out.update(taken=taken, taken_kind=kind)
    parts: list[str] = []
    for key in ("city", "state", "country"):
        value = (exif.get(key) or "").strip()
        if value and value not in parts:
            parts.append(value)
    if parts:
        out["place"] = ", ".join(parts)
    lat, lon = _number(exif.get("latitude")), _number(exif.get("longitude"))
    if lat is not None and lon is not None:
        out.update(latitude=lat, longitude=lon)
    owner = (item.get("owner") or {}).get("name")
    if owner:
        out["owner_name"] = owner
    return out


def fetch(clients: Sequence[ImmichClient], asset_id: str, owner_id: str = "") -> dict | None:
    """The metadata for one asset, asking each account in turn (the pipeline's
    own account can only see an original while it is in an album shared with
    it, but its contributor always can). Returns UNAVAILABLE if no account can
    see it any more, and None if that could not be settled this time (Immich
    unreachable), so the caller tries again later."""
    answered = False
    for client in clients:
        try:
            item = client.get_asset(asset_id)
        except Exception as exc:
            log.warning("could not read the details of asset %s: %s", asset_id, exc)
            return None
        if item is None:
            answered = True
            continue
        meta = meta_from_asset(item)
        if "owner_name" not in meta:
            name = client.get_user_name(item.get("ownerId") or owner_id) if (item.get("ownerId") or owner_id) else ""
            if name:
                meta["owner_name"] = name
        return meta or dict(UNAVAILABLE)
    return dict(UNAVAILABLE) if answered else None


def record(store: LibraryStore, photo_id: str, index: int, meta: dict) -> None:
    """Saves `meta` on a source that has none yet."""
    def apply(photo) -> None:
        if index < len(photo.sources) and not photo.sources[index].meta:
            photo.sources[index].meta = dict(meta)
    try:
        store.update_photo(photo_id, apply)
    except KeyError:
        pass


def fill_source(clients: Sequence[ImmichClient], store: LibraryStore, photo_id: str, index: int, source: Source) -> bool:
    """Looks up one source's metadata and saves it. False if it could not be
    settled this time."""
    meta = fetch(clients, source.asset_id, source.owner_id)
    if meta is None:
        return False
    record(store, photo_id, index, meta)
    return True


def backfill(clients: Sequence[ImmichClient], store: LibraryStore, limit: int = BACKFILL_PER_CYCLE) -> int:
    """Fills in originals copied before metadata was kept, a few per cycle.
    Returns how many were looked up."""
    done = 0
    for photo in list(store.load().photos.values()):
        for index, source in enumerate(photo.sources):
            if source.meta or not source.asset_id:
                continue
            if done >= limit:
                return done
            fill_source(clients, store, photo.id, index, source)
            done += 1
    return done
