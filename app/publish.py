"""Keeping Immich's copy of each photo in line with the library.

A photo's image lives in the revision store. Immich holds a published copy
(an asset owned by the pipeline account), which is what the albums, Live and
the displays show. The copy has to be replaced when a different revision
becomes current (a revert, later a revision), made again when a photo is
restored from the web trash, and the one it replaces has to be thrown away.

Nothing here is triggered directly by an action. The actions only change the
library; each poll cycle calls these, which work out what is out of line and
fix it, so a failure is simply tried again next cycle:

  publish_pending   uploads the current revision of every photo that has no
                    asset, or whose asset shows an older revision
  tidy_stale        moves assets nobody uses any more to Immich's trash
                    (force=False, so they can still be recovered there)
"""
from __future__ import annotations

import logging
import os

from .immich_client import ImmichClient, ImmichError
from .library import STATUS_READY, Library, LibraryStore, Photo
from .revisions import RevisionStore, RevisionStoreError

log = logging.getLogger(__name__)


def needs_publish(photo: Photo) -> bool:
    if photo.trashed or photo.status != STATUS_READY or photo.media_type not in ("image", "video"):
        return False
    rev = photo.current_revision()
    if rev is None or not rev.file:
        return False
    if photo.immich_asset_id is None:
        return True
    # An asset with no recorded revision predates this bookkeeping; trust it.
    return photo.published_revision is not None and photo.published_revision != photo.current


def photos_needing_publish(library: Library) -> list[str]:
    return sorted(p.id for p in library.photos.values() if needs_publish(p))


def publish_photo(immich: ImmichClient, store: LibraryStore, revisions: RevisionStore, photo_id: str) -> bool:
    """Uploads the photo's current revision and records it. The asset it
    replaces, if any, is queued for the trash. Returns False if there was
    nothing to do."""
    photo = store.load().photos.get(photo_id)
    if photo is None or not needs_publish(photo):
        return False
    rev = photo.current_revision()
    try:
        path = revisions.path(rev.file)
    except RevisionStoreError:
        log.warning("%s: revision file path is not valid; not publishing", photo_id)
        return False
    if not os.path.isfile(path):
        log.warning("%s: revision file is missing from the store; not publishing", photo_id)
        return False
    ext = os.path.splitext(path)[1] or ".jpg"
    new_id = immich.upload_asset(path, f"{photo_id}-{rev.n}{ext}")

    def record(p: Photo) -> None:
        old = p.immich_asset_id
        if p.trashed:
            # Trashed while the upload ran: nothing should show it.
            p.stale_asset_ids.append(new_id)
            return
        if old and old != new_id:
            p.stale_asset_ids.append(old)
        p.immich_asset_id = new_id
        p.published_revision = rev.n

    store.update_photo(photo_id, record)
    log.info("%s: published revision %d", photo_id, rev.n)
    return True


def publish_pending(immich: ImmichClient, store: LibraryStore, revisions: RevisionStore) -> list[str]:
    """Publishes every photo that needs it. One failing photo does not stop
    the rest."""
    done = []
    for photo_id in photos_needing_publish(store.load()):
        try:
            if publish_photo(immich, store, revisions, photo_id):
                done.append(photo_id)
        except ImmichError:
            log.warning("could not publish %s; will try again next cycle", photo_id, exc_info=True)
    return done


def tidy_stale(immich: ImmichClient, store: LibraryStore) -> int:
    """Moves assets that no photo uses to Immich's trash and forgets them.
    Returns how many were handled."""
    pending = {p.id: list(p.stale_asset_ids) for p in store.load().photos.values() if p.stale_asset_ids}
    count = 0
    for photo_id, asset_ids in sorted(pending.items()):
        try:
            immich.delete_assets(asset_ids, force=False)
        except ImmichError:
            log.warning("could not move %d old asset(s) of %s to the Immich trash; will retry",
                        len(asset_ids), photo_id, exc_info=True)
            continue

        def forget(p: Photo, gone=frozenset(asset_ids)) -> None:
            p.stale_asset_ids = [a for a in p.stale_asset_ids if a not in gone]

        try:
            store.update_photo(photo_id, forget)
        except KeyError:
            continue
        count += len(asset_ids)
    return count
