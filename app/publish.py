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
  purge_assets      deletes the assets of photos deleted for good from Immich
                    for good (force=True, Immich's trash included)

Immich de-duplicates uploads by checksum, so uploading bytes it has seen
before (a revert stacks a copy of an earlier revision) hands back the
existing asset, even one that is in Immich's trash. publish_photo therefore
checks what it got back: a trashed asset is restored through the API, and if
it is gone (the trash was emptied) the upload is simply made again, which
then creates a fresh asset.
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


def _upload_live_asset(immich: ImmichClient, path: str, name: str) -> str:
    """Uploads the file and returns an asset that is not in Immich's trash."""
    for attempt in range(2):
        asset_id = immich.upload_asset(path, name)
        info = immich.get_asset(asset_id)
        if info is not None and not info.get("isTrashed"):
            return asset_id
        if info is not None:
            try:
                immich.restore_assets([asset_id])
                log.info("restored %s from the Immich trash (the upload matched it)", asset_id)
                return asset_id
            except ImmichError:
                if immich.get_asset(asset_id) is not None:
                    raise
        # Gone from Immich: upload again, which now makes a new asset.
        log.info("%s is no longer in Immich; uploading again", asset_id)
    raise ImmichError(f"could not get a usable asset for {name}")


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
    new_id = _upload_live_asset(immich, path, f"{photo_id}-{rev.n}{ext}")

    def record(p: Photo) -> None:
        # The asset is in use now, so it must not stay queued for the trash.
        p.stale_asset_ids = [a for a in p.stale_asset_ids if a != new_id]
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
    library = store.load()
    # Photos with identical bytes share one asset, and a revert can bring an
    # old asset back into use: never trash one that any photo shows now.
    in_use = {p.immich_asset_id for p in library.photos.values() if p.immich_asset_id}
    pending = {p.id: list(p.stale_asset_ids) for p in library.photos.values() if p.stale_asset_ids}
    count = 0
    for photo_id, asset_ids in sorted(pending.items()):
        keep = [a for a in asset_ids if a in in_use]
        asset_ids = [a for a in asset_ids if a not in in_use]
        if keep:
            store.update_photo(photo_id, lambda p, k=frozenset(keep): setattr(
                p, "stale_asset_ids", [a for a in p.stale_asset_ids if a not in k]))
        if not asset_ids:
            continue
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


def purge_assets(immich: ImmichClient, store: LibraryStore) -> int:
    """Deletes from Immich, for good, the assets of photos that were deleted
    from the pipeline. Returns how many were deleted.

    An asset that a remaining photo still shows is left alone (photos with
    identical bytes share one asset) and just dropped from the list. A
    failure keeps the list for the next cycle."""
    library = store.load()
    pending = list(dict.fromkeys(library.purge_asset_ids))
    if not pending:
        return 0
    in_use = {p.immich_asset_id for p in library.photos.values() if p.immich_asset_id}
    doomed = [a for a in pending if a not in in_use]
    if doomed:
        try:
            immich.delete_assets(doomed, force=True)
        except ImmichError:
            log.warning("could not delete %d asset(s) from Immich; will retry", len(doomed), exc_info=True)
            return 0
    done = frozenset(pending)

    def forget(lib: Library) -> None:
        lib.purge_asset_ids = [a for a in lib.purge_asset_ids if a not in done]
        # An older copy of another photo queued for the trash is gone now too.
        for p in lib.photos.values():
            if p.stale_asset_ids:
                p.stale_asset_ids = [a for a in p.stale_asset_ids if a not in doomed]

    store.update(forget)
    return len(doomed)
