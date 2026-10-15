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
import time
from typing import Sequence

from .immich_client import ImmichClient, ImmichError
from .library import LibraryStore, Source

log = logging.getLogger(__name__)

UNAVAILABLE = {"unavailable": True}
# Bumped when more is read from an asset, so older records are topped up (see `backfill`).
DETAILS_VERSION = 2
# How many originals one cycle looks up when filling in older photos.
BACKFILL_PER_CYCLE = 10

# When each asset was last asked about (this run), so one that keeps failing
# does not use up every cycle's allowance and starve the rest.
_TRIED: dict[str, float] = {}

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
    """Date, place, owner, picture size, camera and exposure from an asset as
    Immich describes it (GET /assets/{id}). Keys with nothing to say are left out."""
    exif = item.get("exifInfo") or {}
    out: dict = {"details": DETAILS_VERSION}
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
    width, height = _number(exif.get("exifImageWidth")), _number(exif.get("exifImageHeight"))
    if width and height:
        out.update(width=int(width), height=int(height))
    size = _number(exif.get("fileSizeInByte"))
    if size:
        out["size_bytes"] = int(size)
    camera = (exif.get("model") or exif.get("make") or "").strip()
    if camera:
        out["camera"] = camera
    exposure = exif.get("exposureTime")
    if isinstance(exposure, (int, float)) and not isinstance(exposure, bool):
        exposure = (f"1/{round(1 / exposure)}" if 0 < exposure < 1 else f"{exposure:g}")
    if isinstance(exposure, str) and exposure.strip():
        out["exposure_time"] = exposure.strip()
    aperture, iso = _number(exif.get("fNumber")), _number(exif.get("iso"))
    if aperture:
        out["f_number"] = round(aperture, 1)
    if iso:
        out["iso"] = int(iso)
    return out


def fetch(clients: Sequence[ImmichClient], asset_id: str, owner_id: str = "") -> dict | None:
    """The metadata for one asset, asking each account in turn (the pipeline's
    own account can only see an original while it is in an album shared with
    it, but its contributor always can). An account that cannot see the asset
    (not found, or access refused) or that fails (Immich unreachable) never
    stops the next account being asked. Returns UNAVAILABLE if every account
    answered that it cannot see it, and None if that could not be settled this
    time (some account failed), so the caller tries again later."""
    answered = unsettled = False
    for client in clients:
        try:
            item = client.get_asset(asset_id)
        except ImmichError as exc:
            if "-> 403" in str(exc):
                answered = True                     # access refused: this account cannot see it
            else:
                unsettled = True
                log.warning("could not read the details of asset %s: %s", asset_id, exc)
            continue
        except Exception as exc:
            unsettled = True
            log.warning("could not read the details of asset %s: %s", asset_id, exc)
            continue
        if item is None:
            answered = True
            continue
        meta = meta_from_asset(item)
        if "owner_name" not in meta:
            name = client.get_user_name(item.get("ownerId") or owner_id) if (item.get("ownerId") or owner_id) else ""
            if name:
                meta["owner_name"] = name
        return meta or dict(UNAVAILABLE)
    return dict(UNAVAILABLE) if answered and not unsettled else None


def record(store: LibraryStore, photo_id: str, index: int, meta: dict) -> None:
    """Saves `meta` on a source that has none yet. On one that has some (kept from
    before more was read), the new details are added to it and win where both have
    a value; if Immich no longer has the asset the old details are kept, marked so
    it is not asked again."""
    def apply(photo) -> None:
        if index >= len(photo.sources):
            return
        source = photo.sources[index]
        if not source.meta or source.meta.get("unavailable"):
            source.meta = dict(meta)
        elif meta.get("unavailable"):
            source.meta = {**source.meta, "details": DETAILS_VERSION}
        else:
            source.meta = {**source.meta, **meta}
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


def backfill(
    clients: Sequence[ImmichClient], store: LibraryStore, limit: int = BACKFILL_PER_CYCLE,
    tried: dict[str, float] | None = None,
) -> int:
    """Fills in originals copied before metadata was kept, and tops up those
    kept before more of it was read, a few per cycle, those not asked about
    recently first. Returns how many were looked up."""
    tried = _TRIED if tried is None else tried
    wanted = [
        (photo.id, index, source)
        for photo in store.load().photos.values()
        for index, source in enumerate(photo.sources)
        if source.asset_id and (not source.meta or (
            not source.meta.get("unavailable") and source.meta.get("details") != DETAILS_VERSION))
    ]
    wanted.sort(key=lambda w: tried.get(w[2].asset_id, 0.0))
    for photo_id, index, source in wanted[:limit]:
        tried[source.asset_id] = time.monotonic()
        fill_source(clients, store, photo_id, index, source)
    return min(len(wanted), limit)
