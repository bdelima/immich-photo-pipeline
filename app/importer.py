"""One-off import of photos that were processed *before* this pipeline
existed (or outside it), so they're tracked like anything else.

What "tracked" means here: an ImageState entry per photo, with
`current_asset_id` pointing at the existing, already-finished asset. From
then on the normal flows apply -- unliking pulls it to Review, liking in
Review asks which managed album, the web UI can mirror its album into
Live, and a comment can delete it.

What is NOT captured, by design: lineage. There is no original to trace
back to, so each imported photo is its own "source" and is flagged
`imported=True`. The pipeline refuses to *revise* such a photo (see
Pipeline._reprocess): feeding an already-matted image back through the
recipe would double-mat it and then delete the good copy.

Safety properties:
  * Dry-run by default; nothing is touched unless `apply=True`.
  * Never removes anything from the source album and never deletes an
    asset -- it only adds album membership, (for a managed target) sets
    the like flag, and records state.
  * Idempotent: anything already tracked is skipped, so re-running after a
    partial failure is safe.
  * Per-photo failures are reported and skipped, not fatal.
"""
from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field

from .albums import find_album_id_by_name
from .immich_client import Asset, ImmichClient, ImmichError
from .state import ImageState, PipelineState, StateStore

log = logging.getLogger(__name__)

REVIEW = "review"


@dataclass
class ImportReport:
    source_album: str
    target: str
    dry_run: bool
    # Asset ids imported -- or, in a dry run, that WOULD be imported.
    imported_ids: list[str] = field(default_factory=list)
    already_tracked: list[str] = field(default_factory=list)
    failed: list[tuple[str, str]] = field(default_factory=list)  # (asset id, reason)


def _tracked_asset_ids(state: PipelineState) -> set[str]:
    ids: set[str] = set()
    for lineage_id, img in state.images.items():
        ids.add(lineage_id)
        ids.update(img.source_asset_ids)
        if img.current_asset_id:
            ids.add(img.current_asset_id)
    return ids


def import_existing(
    immich: ImmichClient,
    store: StateStore,
    *,
    source_album_id: str,
    source_album_name: str,
    target: str,
    review_album_id: str,
    apply: bool = False,
) -> ImportReport:
    """Registers every untracked asset in `source_album_id` as an imported,
    already-finished photo homed in `target` -- either "review" or the
    name of a managed album (created and registered as a watched album if
    it doesn't exist yet).

    A managed target needs each photo to be liked (the managed-album flow
    treats an un-liked photo as "pulled back to Review" and moves it on the
    next cycle), so the like flag is set first. If that fails -- typically
    because the asset belongs to a different Immich account and Immich only
    lets the owner edit it -- the photo is skipped and reported, rather than
    being added and then immediately bounced to Review.
    """
    # Hold the cross-process state lock for the whole load -> save when
    # actually writing, so a running poll cycle can't overwrite the result
    # (and vice versa). A dry run only reads, so it doesn't wait.
    lock = store.exclusive() if apply else contextlib.nullcontext()
    with lock:
        report = ImportReport(source_album=source_album_name, target=target, dry_run=not apply)
        state = store.load()
        tracked = _tracked_asset_ids(state)

        assets: list[Asset] = immich.list_album_assets(source_album_id)
        candidates: list[Asset] = []
        for asset in assets:
            if asset.id in tracked:
                report.already_tracked.append(asset.id)
            else:
                candidates.append(asset)

        if not apply:
            report.imported_ids = [a.id for a in candidates]
            return report

        if target == REVIEW:
            target_album_id = review_album_id
        else:
            target_album_id = state.watched_albums.get(target)
            if not target_album_id:
                target_album_id = _resolve_or_create(immich, target)
                state.watched_albums[target] = target_album_id

        for asset in candidates:
            try:
                if target != REVIEW and not asset.is_favorite:
                    immich.set_favorite(asset.id, True)
                immich.add_assets_to_album(target_album_id, [asset.id])
            except ImmichError as exc:
                log.warning("could not import %s: %s", asset.id, exc)
                report.failed.append((asset.id, str(exc)))
                continue
            state.images[asset.id] = ImageState(
                source_asset_ids=[asset.id],
                current_asset_id=asset.id,
                home=target,
                imported=True,
            )
            report.imported_ids.append(asset.id)

        store.save(state)
        return report


def _resolve_or_create(immich: ImmichClient, name: str) -> str:
    existing = find_album_id_by_name(immich.list_albums(), name)
    return existing or immich.create_album(name)
