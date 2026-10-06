"""One-off import of photos that were processed *before* this pipeline
existed (or outside it), so they are in the library like anything else.

Each asset in the source album becomes a photo with a single revision: its
current image, copied into the revision store and published again as an
asset owned by the pipeline (the source album usually belongs to someone
else, so the pipeline could not manage the original). From then on it can be
browsed, moved, promoted to Live and trashed.

What is NOT captured, by design: lineage. There is no original to trace back
to, so the photo is flagged `imported=True` and the pipeline refuses to
revise it (feeding an already-matted image back through the recipe would
double-mat it).

Safety properties:
  * Dry-run by default; nothing is touched unless `apply=True`.
  * Never removes anything from the source album and never deletes an
    asset.
  * Idempotent: an asset already in the library (as a source) is skipped,
    so re-running after a partial failure is safe.
  * Per-photo failures are reported and skipped, not fatal.
"""
from __future__ import annotations

import logging
import os
import shutil
import tempfile
from dataclasses import dataclass, field

from .albums import ensure_album
from .immich_client import Asset, ImmichClient, ImmichError
from .library import REVIEW, STATUS_READY, LibraryStore, Photo, Revision, Source
from .revisions import RevisionStore

log = logging.getLogger(__name__)


@dataclass
class ImportReport:
    source_album: str
    target: str
    dry_run: bool
    # Asset ids imported -- or, in a dry run, that WOULD be imported.
    imported_ids: list[str] = field(default_factory=list)
    already_tracked: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (asset id, reason)


def _tracked_asset_ids(store: LibraryStore) -> set[str]:
    library = store.load()
    ids: set[str] = set(library.photos)
    for photo in library.photos.values():
        ids.update(s.asset_id for s in photo.sources)
        if photo.immich_asset_id:
            ids.add(photo.immich_asset_id)
    return ids


def import_existing(
    immich: ImmichClient,
    store: LibraryStore,
    revisions: RevisionStore,
    *,
    source_album_id: str,
    source_album_name: str,
    target: str,
    apply: bool = False,
) -> ImportReport:
    """Registers every untracked asset in `source_album_id` as an imported,
    already-finished photo homed in `target` -- either "review" or the name
    of a managed album (created if it doesn't exist yet)."""
    report = ImportReport(source_album=source_album_name, target=target, dry_run=not apply)
    tracked = _tracked_asset_ids(store)
    candidates: list[Asset] = []
    for asset in immich.list_album_assets(source_album_id):
        if asset.id in tracked:
            report.already_tracked.append(asset.id)
        else:
            candidates.append(asset)

    if not apply:
        report.imported_ids = [a.id for a in candidates]
        return report

    if target != REVIEW:
        ensure_album(immich, store, target)

    for asset in candidates:
        try:
            photo = _import_one(immich, revisions, asset, target)
        except (ImmichError, OSError) as exc:
            log.warning("could not import %s: %s", asset.id, exc)
            revisions.purge(asset.id)
            report.failed.append((asset.id, str(exc)))
            continue
        store.update(lambda library, photo=photo: library.photos.setdefault(photo.id, photo))
        report.imported_ids.append(asset.id)
    return report


def _import_one(immich: ImmichClient, revisions: RevisionStore, asset: Asset, target: str) -> Photo:
    tmp = tempfile.mkdtemp(prefix="import-")
    try:
        path = immich.download_asset_original(asset.id, tmp)
        rel, digest = revisions.save_revision(asset.id, 0, path)
        ext = os.path.splitext(path)[1] or ".jpg"
        new_asset = immich.upload_asset(path, f"{asset.id}{ext}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return Photo(
        id=asset.id,
        sources=[Source(asset_id=asset.id, name=asset.original_file_name or "", owner_id=asset.owner_id or "")],
        revisions=[Revision(n=0, parent=None, file=rel, sha256=digest, origin="legacy")],
        current=0, home=target, status=STATUS_READY, immich_asset_id=new_asset, published_revision=0, imported=True,
    )
