"""Shares the pipeline's albums with the household's other Immich accounts.

Immich only surfaces likes and comments on a *shared* album, and the other
accounts can't drop photos into an entry queue they can't see -- so a
freshly created album, visible only to the pipeline's own account, is
useless to everyone else until it's shared. The accounts to share with are
exactly the ones configured as IMMICH_EXTRA_API_KEY: each key identifies
its account via GET /users/me, so no user ids need configuring by hand.

An album that is already shared keeps the role it has: only accounts that
have no access yet are added.

Everything here is best-effort. Failing to share (an album the pipeline
doesn't own because its id was pinned, a key that's been revoked) is logged
and skipped; it never stops the pipeline from running.
"""
from __future__ import annotations

import logging

from .immich_client import ImmichClient, ImmichError

log = logging.getLogger(__name__)

# Entry queues are shared as editors, so the household can drop photos into
# them. Every other album is the pipeline's own output and is shared as
# viewers: they can look (and like), but cannot add or remove anything.
ROLE = "editor"
VIEWER = "viewer"


def resolve_user_ids(owner: ImmichClient, extra_clients: list[ImmichClient]) -> list[str]:
    """User ids of the accounts behind the extra API keys, de-duplicated and
    excluding the pipeline's own account. A key that can't be resolved is
    skipped with a warning."""
    try:
        owner_id = owner.get_my_user_id()
    except ImmichError:
        log.exception("could not look up the pipeline account's own user id; not sharing albums")
        return []
    ids: list[str] = []
    for client in extra_clients:
        try:
            uid = client.get_my_user_id()
        except ImmichError:
            log.warning("could not resolve the user behind an IMMICH_EXTRA_API_KEY; skipping it", exc_info=True)
            continue
        if uid != owner_id and uid not in ids:
            ids.append(uid)
    return ids


def ensure_shared(
    immich: ImmichClient, album_id: str, user_ids: list[str], role: str = ROLE, *, convert: bool = False,
) -> bool:
    """Makes sure every account in user_ids has access to the album, adding
    only the missing ones (Immich errors on a user who already has access).
    With `convert`, an account that already has access under a different
    role is changed to `role` (used to turn editor-shared output albums into
    viewer-shared ones). Returns True if the album is shared with everyone
    afterwards."""
    if not user_ids:
        return True
    try:
        album = immich.get_album(album_id)
        if album.get("isActivityEnabled") is False:
            immich.enable_album_activity(album_id)
            log.info("turned on likes and comments for album %s", album_id)
        roles = {au.get("user", {}).get("id"): au.get("role") for au in album.get("albumUsers", [])}
        have = set(roles)
        have.add(album.get("ownerId"))
        if convert:
            for uid in user_ids:
                if uid in roles and roles[uid] != role:
                    immich.update_album_user_role(album_id, uid, role)
                    log.info("changed account %s on album %s from %s to %s", uid, album_id, roles[uid], role)
        missing = [uid for uid in user_ids if uid not in have]
        if missing:
            immich.add_album_users(album_id, missing, role)
            log.info("shared album %s with %d account(s)", album_id, len(missing))
        return True
    except ImmichError:
        log.warning(
            "could not share album %s (the pipeline account may not own it); "
            "share it by hand in the Immich UI", album_id, exc_info=True,
        )
        return False


def ensure_all_shared(immich: ImmichClient, album_ids: list[str], user_ids: list[str], role: str = ROLE) -> None:
    for album_id in dict.fromkeys(album_ids):
        if album_id:
            ensure_shared(immich, album_id, user_ids, role)
