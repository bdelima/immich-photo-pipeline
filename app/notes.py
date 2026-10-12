"""The note on each pipeline-managed Immich album: what it is, that it is
view-only there, and where to go to change it.

Immich shows an album's description as plain text, so the address is shown
rather than linked; it can be copied. The address opens the pipeline web UI
on the matching view (see the hash handling in index.html).

The notes are kept current: every cycle compares each album's description
with the wanted text and rewrites it only when they differ, so an address
that changes in the compose file reaches every album, and a note edited by
hand in Immich is put back.
"""
from __future__ import annotations

import logging
from urllib.parse import quote

from .immich_client import ImmichClient, ImmichError
from .library import Library

log = logging.getLogger(__name__)

_MANAGED = "This is a view-only album managed by the Immich Pipeline. To make changes to this album, go to {url}"
_REVIEW = (
    "This is a view-only album managed by the Immich Pipeline that holds items waiting for review. "
    "To review items, go to {url}"
)
_LIVE = (
    "This is a view-only album managed by the Immich Pipeline that determines wallpapers on connected "
    "display devices. To add or remove wallpapers, go to {url}"
)


def view_url(base_url: str, view: str) -> str:
    """The pipeline address that opens the given view ("review", "live" or
    "album:<name>")."""
    return f"{base_url.rstrip('/')}/#{quote(view, safe=':')}"


def desired_notes(library: Library, base_url: str, live_album_id: str, review_album_id: str) -> dict[str, str]:
    """Album id -> the description it should have."""
    notes: dict[str, str] = {}
    for name, album_id in sorted(library.albums.items()):
        notes[album_id] = _MANAGED.format(url=view_url(base_url, f"album:{name}"))
    if review_album_id:
        notes[review_album_id] = _REVIEW.format(url=view_url(base_url, "review"))
    if live_album_id:
        notes[live_album_id] = _LIVE.format(url=view_url(base_url, "live"))
    return notes


def sync_notes(
    immich: ImmichClient, library: Library, base_url: str, live_album_id: str, review_album_id: str,
) -> list[str]:
    """Brings every managed album's description into line. Returns the ids of
    the albums rewritten. A failure on one album is logged and left for the
    next cycle."""
    if not base_url:
        return []
    wanted = desired_notes(library, base_url, live_album_id, review_album_id)
    current = {a.get("id"): (a.get("description") or "") for a in immich.list_albums()}
    changed = []
    for album_id, text in wanted.items():
        if album_id not in current or current[album_id] == text:
            continue
        try:
            immich.update_album_description(album_id, text)
            changed.append(album_id)
        except ImmichError:
            log.warning("could not update the note on album %s; will try again next cycle", album_id, exc_info=True)
    return changed
