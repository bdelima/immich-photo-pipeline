"""Reverting a photo to an earlier step.

A revert never moves a pointer back. It stacks a copy of the chosen
revision's image on top of the history as a new step (see Revision), so the
history stays a straight line, nothing is hidden, and undoing a revert is
just another revert. It costs a file copy and no Claude call.

This only changes the library and the revision store. Putting the new
current image back on Immich is up to the caller.
"""
from __future__ import annotations

import os

from .library import LibraryStore, Revision
from .revisions import RevisionStore


class RevertError(Exception):
    """The revert can't be done (unknown photo or step, or no image)."""


class RevertConflict(RevertError):
    """The photo changed while the copy was being made; try again."""


def revert_to(store: LibraryStore, revisions: RevisionStore, photo_id: str, target_n: int) -> Revision | None:
    """Stacks a copy of revision `target_n` on top of the photo's history and
    returns the new revision. Returns None, changing nothing, if the photo is
    already showing that image."""
    photo = store.load().photos.get(photo_id)
    if photo is None:
        raise RevertError(f"unknown photo {photo_id!r}")
    target = photo.revision(target_n)
    if target is None or not target.file:
        raise RevertError(f"photo {photo_id} has no revision {target_n}")
    current = photo.current_revision()
    if current is not None and photo.content_source(current.n) == photo.content_source(target.n):
        return None

    n = photo.next_revision_number()
    rel, digest = revisions.save_revision(photo_id, n, revisions.path(target.file))

    def record(p) -> Revision:
        if p.next_revision_number() != n:
            raise RevertConflict(f"photo {photo_id} changed during the revert")
        return p.add_revert(target_n, rel, digest)

    try:
        return store.update_photo(photo_id, record)
    except BaseException:
        # Nothing was recorded, so don't leave the copy to block revision n.
        try:
            os.unlink(revisions.path(rel))
        except OSError:
            pass
        raise
