"""Makes Immich albums match the library.

The library decides which photos are in which managed album and which are
promoted to Live; the Immich albums are only a view of that, kept so the
household can browse them in Immich and so the displays (overflight-feed,
frame-mirror) can read Live. So nothing here ever reads an album to learn what
a photo's state is: it only brings the album into line.

Each function adds missing photos before it removes extra ones, so a failure
half-way leaves an album too full rather than too empty.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from .immich_client import ImmichClient, ImmichError
from .library import Library

log = logging.getLogger(__name__)


@dataclass
class ReconcileResult:
    added: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    # True when nothing was removed because the album was about to be
    # emptied (see reconcile_album).
    held_back: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.removed)


def desired_live_ids(library: Library) -> set[str]:
    """The Immich assets that should be in Live: every promoted photo that
    is not trashed and has a published asset."""
    return {p.immich_asset_id for p in library.live_photos() if p.immich_asset_id}


def desired_album_ids(library: Library, album_name: str) -> set[str]:
    """The Immich assets that should be in a managed album."""
    return {p.immich_asset_id for p in library.in_home(album_name) if p.immich_asset_id}


def reconcile_album(
    immich: ImmichClient, album_id: str, desired: set[str], *, allow_empty: bool = False,
) -> ReconcileResult:
    """Adds and removes assets so the album holds exactly `desired`.

    Never empties an album that has photos in it unless `allow_empty` is
    set. An empty `desired` is what a lost or blank library looks like, and
    wiping Live (or every album) because of that would be far worse than
    leaving them alone, so the caller has to say it means it."""
    current = {a.id for a in immich.list_album_assets(album_id)}
    result = ReconcileResult()
    result.added = sorted(desired - current)
    extra = sorted(current - desired)
    immich.add_assets_to_album(album_id, result.added)
    if extra and not desired and not allow_empty:
        result.held_back = True
        log.warning(
            "not emptying album %s (%d photo(s)): the library says it should be empty; "
            "pass allow_empty if that is really intended", album_id, len(extra),
        )
        return result
    immich.remove_assets_from_album(album_id, extra)
    result.removed = extra
    return result


def _may_empty(library: Library, members) -> bool:
    """Whether an empty desired set can be believed. It can when the library
    is not blank (a blank or lost one is the case the guard exists for) and
    every photo that belongs in the album already has its asset, so nothing
    is empty only because an upload has not happened yet. This is what lets
    the last photo be taken out of Live or an album."""
    return bool(library.photos) and all(p.immich_asset_id for p in members)


def sync_live(
    immich: ImmichClient, library: Library, live_album_id: str, *, allow_empty: bool | None = None,
) -> ReconcileResult:
    """`allow_empty` None means decide from the library (see _may_empty)."""
    if allow_empty is None:
        allow_empty = _may_empty(library, library.live_photos())
    return reconcile_album(immich, live_album_id, desired_live_ids(library), allow_empty=allow_empty)


def sync_albums(
    immich: ImmichClient, library: Library, *, allow_empty: bool | None = None,
) -> dict[str, ReconcileResult]:
    """Reconciles every managed album. A failure on one album is logged and
    does not stop the others; that album is simply left for the next pass."""
    results: dict[str, ReconcileResult] = {}
    for name, album_id in sorted(library.albums.items()):
        empty_ok = allow_empty if allow_empty is not None else _may_empty(library, library.in_home(name))
        try:
            results[name] = reconcile_album(
                immich, album_id, desired_album_ids(library, name), allow_empty=empty_ok,
            )
        except ImmichError:
            log.exception("could not bring album %r (%s) in line with the library", name, album_id)
    return results
