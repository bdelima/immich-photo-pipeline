"""Finds new originals in the two entry queues and records them in the library.

This only looks and records; nothing here downloads an image or calls Claude.
Each new photo is created with status "processing", which is what the worker
(app/worker.py) picks up. Because that status is saved in the library, work
that was queued or half done when the container stopped simply continues.

  * Wallpaper Maker: every new photo or video becomes a single photo.
  * Collage Maker: portraits are grouped two or three at a time. A lone
    portrait waits (nothing is recorded) until a partner arrives. Videos do
    not belong in a collage: each one is recorded as failed, with the reason,
    so it is not looked at again.

An original is "known" once it is a source of any library photo, so an
original that could not be removed from its entry queue is never processed
twice.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Literal

from .immich_client import Asset, ImmichClient, ImmichError
from .library import (
    INBOX, STATUS_FAILED, STATUS_PROCESSING, Library, LibraryStore, Photo, Source,
)

log = logging.getLogger(__name__)

QUEUE_WALLPAPER = "wallpaper"
QUEUE_COLLAGE = "collage"

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".3gp", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg",
}


# ---- pure decision helpers (unit-testable without Immich) ----------------


def is_portrait(asset: Asset) -> bool:
    orientation = (asset.exif_orientation or "").strip()
    # EXIF orientation 6/8 mean the stored dims are landscape but the
    # displayed image is rotated to portrait; 1 is upright. Treat unknown as
    # not-portrait (conservative: don't block solo wallpaper processing on a
    # guess).
    return orientation in {"6", "8", "-90", "90"}


def is_video(asset: Asset) -> bool:
    return os.path.splitext(asset.original_file_name or "")[1].lower() in VIDEO_EXTENSIONS


@dataclass
class CollagePlan:
    action: Literal["wait", "group"]
    asset_ids: list[str]


def plan_collage_maker(assets: list[Asset]) -> CollagePlan:
    """Collage Maker always holds a lone portrait; two or more get grouped.
    3-up preferred per the recipe's own guidance, 2-up otherwise."""
    if len(assets) >= 2:
        group = assets[:3] if len(assets) >= 3 else assets
        return CollagePlan(action="group", asset_ids=[a.id for a in group])
    return CollagePlan(action="wait", asset_ids=[a.id for a in assets])


def known_source_ids(library: Library) -> set[str]:
    return {s.asset_id for p in library.photos.values() for s in p.sources}


def _source(asset: Asset) -> Source:
    return Source(asset_id=asset.id, name=asset.original_file_name or "", owner_id=asset.owner_id or "")


# ---- the scan --------------------------------------------------------------


class Intake:
    def __init__(self, immich: ImmichClient, store: LibraryStore, wallpaper_album_id: str, collage_album_id: str):
        self.immich = immich
        self.store = store
        self.wallpaper_album_id = wallpaper_album_id
        self.collage_album_id = collage_album_id
        # A lone portrait is reported once, not every cycle.
        self._waiting_logged: set[str] = set()

    def scan(self) -> list[str]:
        """Records every new original in the entry queues. Returns the ids of
        the photos created. A queue that cannot be listed is skipped until
        the next cycle."""
        created: list[str] = []
        created += self._scan_wallpaper()
        created += self._scan_collage()
        return created

    def _list(self, album_id: str) -> list[Asset] | None:
        try:
            return self.immich.list_album_assets(album_id)
        except ImmichError:
            log.exception("could not list entry queue %s; will try again next cycle", album_id)
            return None

    def _scan_wallpaper(self) -> list[str]:
        assets = self._list(self.wallpaper_album_id)
        if assets is None:
            return []

        def add(library: Library) -> list[str]:
            known = known_source_ids(library)
            made = []
            for asset in assets:
                if asset.id in known or asset.id in library.photos:
                    continue
                video = is_video(asset)
                library.photos[asset.id] = Photo(
                    id=asset.id, kind="single", media_type="video" if video else "image",
                    sources=[_source(asset)], home=INBOX, status=STATUS_PROCESSING, queue=QUEUE_WALLPAPER,
                )
                made.append(asset.id)
            return made

        made = self.store.update(add)
        for photo_id in made:
            log.info("wallpaper: new original %s queued", photo_id)
        return made

    def _scan_collage(self) -> list[str]:
        assets = self._list(self.collage_album_id)
        if assets is None:
            return []

        def add(library: Library) -> list[str]:
            known = known_source_ids(library)
            fresh = [a for a in assets if a.id not in known and a.id not in library.photos]
            made = []
            usable = []
            for asset in fresh:
                if is_video(asset):
                    library.photos[asset.id] = Photo(
                        id=asset.id, kind="single", media_type="video", sources=[_source(asset)],
                        home=INBOX, status=STATUS_FAILED, queue=QUEUE_COLLAGE,
                        error="Videos can't be used in a collage. Put it in Wallpaper Maker instead.",
                    )
                    made.append(asset.id)
                else:
                    usable.append(asset)
            while True:
                plan = plan_collage_maker(usable)
                if plan.action != "group":
                    break
                by_id = {a.id: a for a in usable}
                group = [by_id[i] for i in plan.asset_ids]
                library.photos[group[0].id] = Photo(
                    id=group[0].id, kind="collage", sources=[_source(a) for a in group],
                    home=INBOX, status=STATUS_PROCESSING, queue=QUEUE_COLLAGE,
                )
                made.append(group[0].id)
                usable = [a for a in usable if a.id not in plan.asset_ids]
            if plan.asset_ids and plan.asset_ids[0] not in self._waiting_logged:
                self._waiting_logged.add(plan.asset_ids[0])
                log.info("collage: %s is waiting for a second portrait", plan.asset_ids[0])
            return made

        made = self.store.update(add)
        for photo_id in made:
            log.info("collage: new photo %s recorded", photo_id)
        return made
