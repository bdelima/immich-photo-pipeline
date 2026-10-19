"""Finds new originals in the entry queues and records them in the library.

This only looks and records; nothing here downloads an image or calls Claude.
Each new photo is created with status "processing", which is what the worker
(app/worker.py) picks up. Because that status is saved in the library, work
that was queued or half done when the container stopped simply continues.

  * Wallpaper Maker: every new photo or video becomes a single photo.
  * Import Only: every new photo or video becomes a single photo that is kept
    exactly as it is: no recipe, no revision. It lands in Review, flagged
    `imported`, and can be browsed, moved, promoted and trashed.
  * Collage Maker: photos wait there (nothing is recorded) until a person
    chooses 2 to 12 of them in the Collage Maker place and presses Make
    collage (app/collage.py). Videos do not belong in a collage: each one is
    recorded as failed, with the reason, so it is not looked at again.

An original is "known" once it is a source of any library photo, so an
original that could not be removed from its entry queue is never processed
twice.
"""
from __future__ import annotations

import logging
import os

from .immich_client import Asset, ImmichClient, ImmichError
from .library import (
    INBOX, STATUS_FAILED, STATUS_PROCESSING, Library, LibraryStore, Photo, Source,
)

log = logging.getLogger(__name__)

QUEUE_WALLPAPER = "wallpaper"
QUEUE_COLLAGE = "collage"
QUEUE_IMPORT = "import"

VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".3gp", ".mts", ".m2ts", ".wmv", ".mpg", ".mpeg",
}


# ---- pure decision helpers (unit-testable without Immich) ----------------


def is_video(asset: Asset) -> bool:
    return os.path.splitext(asset.original_file_name or "")[1].lower() in VIDEO_EXTENSIONS


def known_source_ids(library: Library) -> set[str]:
    return {s.asset_id for p in library.photos.values() for s in p.sources}


def _source(asset: Asset) -> Source:
    return Source(asset_id=asset.id, name=asset.original_file_name or "", owner_id=asset.owner_id or "")


# ---- the scan --------------------------------------------------------------


class Intake:
    def __init__(
        self, immich: ImmichClient, store: LibraryStore, wallpaper_album_id: str, collage_album_id: str,
        import_album_id: str = "",
    ):
        self.immich = immich
        self.store = store
        self.wallpaper_album_id = wallpaper_album_id
        self.collage_album_id = collage_album_id
        self.import_album_id = import_album_id

    def scan(self) -> list[str]:
        """Records every new original in the entry queues. Returns the ids of
        the photos created. A queue that cannot be listed is skipped until
        the next cycle."""
        created: list[str] = []
        created += self._scan_wallpaper()
        created += self._scan_collage()
        created += self._scan_import()
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
            for asset in fresh:
                if is_video(asset):
                    library.photos[asset.id] = Photo(
                        id=asset.id, kind="single", media_type="video", sources=[_source(asset)],
                        home=INBOX, status=STATUS_FAILED, queue=QUEUE_COLLAGE,
                        error="Videos can't be used in a collage. Put it in Wallpaper Maker instead.",
                    )
                    made.append(asset.id)
            return made

        made = self.store.update(add)
        for photo_id in made:
            log.info("collage: video %s recorded as failed", photo_id)
        return made

    def _scan_import(self) -> list[str]:
        if not self.import_album_id:
            return []
        assets = self._list(self.import_album_id)
        if assets is None:
            return []

        def add(library: Library) -> list[str]:
            known = known_source_ids(library)
            made = []
            for asset in assets:
                if asset.id in known or asset.id in library.photos:
                    continue
                library.photos[asset.id] = Photo(
                    id=asset.id, kind="single", media_type="video" if is_video(asset) else "image",
                    sources=[_source(asset)], home=INBOX, status=STATUS_PROCESSING, queue=QUEUE_IMPORT,
                    imported=True,
                )
                made.append(asset.id)
            return made

        made = self.store.update(add)
        for photo_id in made:
            log.info("import: new original %s queued", photo_id)
        return made
