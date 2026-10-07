"""What a person can do to photos from the web UI: promote to Live or take
out of it, move to another place, trash, restore, and revert to an earlier
step.

Every action takes a list of photo ids (the UI's multi-select) and returns a
Result saying which were done and which were skipped, with the reason, so one
photo that can't be acted on never blocks the rest.

Actions change the library and nothing else. Immich is brought into line
afterwards by the poll cycle (see projection.py and publish.py), so an action
is quick, can't fail half-way on a network error, and a failure to reach
Immich is simply retried.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from .library import (
    INBOX, REVIEW, STATUS_PROCESSING, STATUS_READY, Library, LibraryStore, Photo, now_iso,
)
from .revert import RevertConflict, RevertError, revert_to
from .revisions import RevisionStore

MAX_BATCH = 500
MAX_ALBUM_NAME = 60

# Names a managed album can't have, because the web UI or Immich already uses
# them for something else (compared ignoring case). The configured names of
# the entry queues, Review and Live are added by the caller.
RESERVED_NAMES = frozenset({"inbox", "review", "live", "queue", "trash", "albums"})


class ActionError(Exception):
    """The whole request is wrong (as opposed to one photo being skipped)."""


@dataclass
class Result:
    done: list[str] = field(default_factory=list)
    skipped: list[dict] = field(default_factory=list)

    def skip(self, photo_id: str, reason: str) -> None:
        self.skipped.append({"id": photo_id, "reason": reason})

    def as_dict(self) -> dict:
        return {"done": self.done, "skipped": self.skipped}


def clean_ids(ids) -> list[str]:
    if not isinstance(ids, list) or not ids or not all(isinstance(i, str) and i for i in ids):
        raise ActionError("ids must be a non-empty list of photo ids")
    if len(ids) > MAX_BATCH:
        raise ActionError(f"at most {MAX_BATCH} photos at a time")
    return list(dict.fromkeys(ids))


def album_name(library: Library, raw, reserved: set[str] | frozenset[str] = frozenset()) -> str:
    """Validates a destination: "review", an existing album, or a new album
    name. Returns the home to use (an existing album's own spelling if the
    name matches one ignoring case)."""
    if not isinstance(raw, str):
        raise ActionError("home must be text")
    name = " ".join(raw.split())
    if not name:
        raise ActionError("name the album")
    if name.casefold() == REVIEW:
        return REVIEW
    if len(name) > MAX_ALBUM_NAME or re.search(r"[\x00-\x1f/\\]", name):
        raise ActionError(f"album names are at most {MAX_ALBUM_NAME} characters, with no slashes")
    known = set(library.albums) | {p.home for p in library.photos.values()}
    for existing in sorted(known - {INBOX, REVIEW}):
        if existing.casefold() == name.casefold():
            return existing
    blocked = {r.casefold() for r in RESERVED_NAMES | set(reserved)}
    if name.casefold() in blocked:
        raise ActionError(f"{name!r} is already used for something else")
    return name


def _each(store: LibraryStore, ids: list[str], fn) -> Result:
    """Runs fn(library, photo, result) on each photo in one short update."""
    result = Result()

    def apply(library: Library) -> None:
        for pid in ids:
            photo = library.photos.get(pid)
            if photo is None:
                result.skip(pid, "no such photo")
            else:
                fn(library, photo, result)

    store.update(apply)
    return result


def _movable(photo: Photo) -> str | None:
    """Why a photo can't be promoted or moved, or None if it can."""
    if photo.trashed:
        return "it is in the trash"
    if photo.status != STATUS_READY:
        return "it isn't finished yet"
    return None


def promote(store: LibraryStore, ids, live: bool = True) -> Result:
    ids = clean_ids(ids)

    def one(library, photo, result):
        why = _movable(photo)
        if why:
            return result.skip(photo.id, why)
        if photo.live == live:
            return result.skip(photo.id, "already in Live" if live else "not in Live")
        photo.live = live
        result.done.append(photo.id)

    return _each(store, ids, one)


def move(store: LibraryStore, ids, home, reserved: set[str] = frozenset()) -> Result:
    ids = clean_ids(ids)
    # Validate against the current library; the name is checked again against
    # the albums that exist when the move is applied.
    target = album_name(store.load(), home, reserved)

    def one(library, photo, result):
        why = _movable(photo)
        if why:
            return result.skip(photo.id, why)
        if photo.home == target:
            return result.skip(photo.id, "already there")
        photo.home = target
        result.done.append(photo.id)

    return _each(store, ids, one)


def trash(store: LibraryStore, ids) -> Result:
    """Puts photos in the web trash. Their Immich copy is moved to Immich's
    trash by the next poll cycle. Revisions and every other piece of state
    are kept, so a restore brings the photo back as it was."""
    ids = clean_ids(ids)

    def one(library, photo, result):
        if photo.trashed:
            return result.skip(photo.id, "already in the trash")
        if photo.status == STATUS_PROCESSING:
            return result.skip(photo.id, "it is being processed")
        photo.trashed = True
        photo.trashed_at = now_iso()
        photo.trashed_from = photo.home
        if photo.immich_asset_id:
            photo.stale_asset_ids.append(photo.immich_asset_id)
        photo.immich_asset_id = None
        photo.published_revision = None
        result.done.append(photo.id)

    return _each(store, ids, one)


def restore(store: LibraryStore, ids) -> Result:
    """Takes photos out of the trash, back to where they were (Review if that
    album no longer exists). The next poll cycle uploads a fresh copy, since
    the old one went to Immich's trash."""
    ids = clean_ids(ids)

    def one(library, photo, result):
        if not photo.trashed:
            return result.skip(photo.id, "not in the trash")
        back = photo.trashed_from
        if back not in (REVIEW, INBOX) and back not in library.albums:
            back = REVIEW
        photo.trashed = False
        photo.trashed_at = None
        photo.home = back
        photo.trashed_from = None
        result.done.append(photo.id)

    return _each(store, ids, one)


def revert(store: LibraryStore, revisions: RevisionStore, photo_id: str, step) -> dict:
    """Reverts a photo to an earlier step by stacking a copy of it on top
    (see revert.py). Returns {"step": new step} or {"unchanged": True} if it
    already shows that image."""
    if isinstance(step, bool) or not isinstance(step, int):
        raise ActionError("step must be a number")
    photo = store.load().photos.get(photo_id)
    if photo is None:
        raise ActionError("no such photo")
    why = _movable(photo)
    if why:
        raise ActionError(f"can't revert: {why}")
    if photo.busy:
        raise ActionError("can't revert: it is being revised")
    target = photo.revision_at_step(step)
    if target is None:
        raise ActionError(f"there is no step {step}")
    try:
        new = revert_to(store, revisions, photo_id, target.n)
    except RevertConflict as exc:
        raise ActionError("the photo changed while reverting; try again") from exc
    except RevertError as exc:
        raise ActionError(str(exc)) from exc
    if new is None:
        return {"unchanged": True}
    return {"step": store.load().photos[photo_id].step_of(new.n)}
