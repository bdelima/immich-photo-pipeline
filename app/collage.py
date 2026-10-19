"""The Collage Maker place: the photos waiting in the Collage Maker album, and making a collage of some of them.

Photos sent to Collage Maker (from the Photos view, or dropped into the Immich album) only wait there.
Nothing is recorded about them until a person chooses 2 to 12 and presses Make collage; the rest stay.
Landscape and portrait photos can be mixed. A collage is recorded like any other new photo (status
"processing", queue "collage"), so the worker picks it up as it does at every other entry. Its sources are
in the order the photos were chosen, which is the order the recipe lays them out in.
"""
from __future__ import annotations

from . import household, intake
from .immich_client import ImmichClient
from .library import INBOX, STATUS_PROCESSING, Library, LibraryStore, Photo, Source

MIN_PHOTOS = 2
MAX_PHOTOS = 12
QUEUE = intake.QUEUE_COLLAGE


def waiting(immich: ImmichClient, album_id: str, library: Library) -> list[dict]:
    """The photos waiting in the Collage Maker album, newest first, in the shape the Photos view draws.
    Videos and originals the pipeline already knows (a photo was made from them) are left out."""
    skip = intake.known_source_ids(library) | set(library.photos)
    return household.album_items(immich, album_id, skip)


def make(store: LibraryStore, chosen: list[dict]) -> str:
    """Records one collage from `chosen` (items from waiting(), in the order to lay them out) and returns its id.
    Raises ValueError if there are too few or too many, or one of them is no longer waiting."""
    if not MIN_PHOTOS <= len(chosen) <= MAX_PHOTOS:
        raise ValueError(f"a collage takes {MIN_PHOTOS} to {MAX_PHOTOS} photos")
    photo_id = chosen[0]["id"]

    def add(library: Library) -> None:
        known = intake.known_source_ids(library)
        if any(c["id"] in known for c in chosen) or photo_id in library.photos:
            raise ValueError("one of these photos is already in the pipeline")
        library.photos[photo_id] = Photo(
            id=photo_id, kind="collage", home=INBOX, status=STATUS_PROCESSING, queue=QUEUE,
            sources=[Source(asset_id=c["id"], name=c.get("name") or "", owner_id=c.get("owner_id") or "") for c in chosen],
        )

    store.update(add)
    return photo_id
