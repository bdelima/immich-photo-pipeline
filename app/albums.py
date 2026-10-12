"""Resolves the four core albums (Collage Maker, Wallpaper Maker, Review,
Live) by name at startup, creating any that don't exist yet. An explicit
*_ALBUM_ID env var always wins over the name lookup, for anyone who's
already set albums up by hand and would rather pin the id directly.

Sharing is separate: see sharing.py, which main.py runs after this to
share the albums with the accounts behind IMMICH_EXTRA_API_KEY.
"""
from __future__ import annotations

import dataclasses
import logging

from .config import Config
from .immich_client import ImmichClient, ImmichError
from .library import LibraryStore

log = logging.getLogger(__name__)


def find_album_id_by_name(albums: list[dict], name: str) -> str | None:
    matches = [a["id"] for a in albums if a.get("albumName") == name]
    if len(matches) > 1:
        log.warning("multiple albums named %r exist; using the first one (%s)", name, matches[0])
    return matches[0] if matches else None


def resolve_or_create_album(immich: ImmichClient, albums: list[dict], name: str) -> str:
    existing = find_album_id_by_name(albums, name)
    if existing:
        return existing
    log.info("creating album %r (no existing album with that name)", name)
    return immich.create_album(name)


def ensure_core_albums(immich: ImmichClient, cfg: Config) -> Config:
    """Returns a copy of cfg with all four *_album_id fields guaranteed
    non-empty, resolving/creating by name wherever an id wasn't already
    pinned. Fetches the album list once up front rather than once per
    album, to avoid four separate round-trips doing the same lookup."""
    albums = immich.list_albums()
    resolved = {
        "collage_album_id": cfg.collage_album_id or resolve_or_create_album(immich, albums, cfg.collage_album_name),
        "wallpaper_album_id": cfg.wallpaper_album_id or resolve_or_create_album(immich, albums, cfg.wallpaper_album_name),
        "review_album_id": cfg.review_album_id or resolve_or_create_album(immich, albums, cfg.review_album_name),
        "live_album_id": cfg.live_album_id or resolve_or_create_album(immich, albums, cfg.live_album_name),
    }
    return dataclasses.replace(cfg, **resolved)


def ensure_album(immich: ImmichClient, store: LibraryStore, name: str) -> str:
    """The Immich album id for a managed album, making one (owned by this
    account) if the library has none yet, and recording it in the library.

    An existing album of that name is reused only if this account owns it:
    an album owned by someone else cannot be changed by the pipeline."""
    known = store.load().albums.get(name)
    if known:
        return known
    album_id = ""
    try:
        me = immich.get_my_user_id()
        for album in immich.list_albums():
            if album.get("albumName") == name and album.get("ownerId") == me:
                album_id = album["id"]
                break
    except ImmichError:
        log.warning("could not look for an existing album named %r", name, exc_info=True)
    if not album_id:
        album_id = immich.create_album(name)

    def record(library):
        return library.albums.setdefault(name, album_id)

    return store.update(record)


def ensure_home_albums(immich: ImmichClient, store: LibraryStore) -> list[str]:
    """Makes sure every album a photo lives in exists in Immich. Returns the
    names of albums created or newly recorded. Failures are logged and left
    for the next cycle."""
    library = store.load()
    wanted = {p.home for p in library.photos.values()} - {"inbox", "review"}
    made = []
    for name in sorted(wanted - set(library.albums)):
        try:
            ensure_album(immich, store, name)
            made.append(name)
        except ImmichError:
            log.warning("could not create album %r; will try again next cycle", name, exc_info=True)
    return made
