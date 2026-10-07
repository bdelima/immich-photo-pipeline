"""Read-only browsing of the library: the tree, the photos in each place,
one photo's detail and history, and the image files themselves.

Nothing here changes anything. Actions on photos are added separately.

Views (the places the tree offers):
    review          photos waiting in Review
    live            every photo promoted to Live, wherever it lives
    album:<name>    a managed album
    queue           photos not finished yet: processing, waiting for an
                    answer, or failed
    trash           trashed photos
"""
from __future__ import annotations

import mimetypes
import os

from flask import Flask, abort, jsonify, request, send_file

from .. import chat
from ..library import (
    INBOX, REVIEW, STATUS_AWAITING_ANSWER, STATUS_FAILED, STATUS_PROCESSING, STATUS_READY,
    Library, LibraryStore, Photo,
)
from ..revisions import RevisionStore, RevisionStoreError
from ..rules import RulesStore
from ..thumbs import ThumbCache

_QUEUE_STATUSES = (STATUS_PROCESSING, STATUS_AWAITING_ANSWER, STATUS_FAILED)


def album_names(library: Library) -> list[str]:
    """Every managed album: those that exist in Immich, and any a photo has
    just been moved to whose Immich album the poll cycle hasn't made yet."""
    homes = {p.home for p in library.photos.values() if not p.trashed} - {INBOX, REVIEW}
    return sorted(set(library.albums) | homes)


def photos_in_view(library: Library, view: str) -> list[Photo] | None:
    """The photos in a view, newest first. None if the view is not valid."""
    live = [p for p in library.photos.values() if not p.trashed]
    if view == "review":
        photos = [p for p in live if p.home == REVIEW and p.status == STATUS_READY]
    elif view == "live":
        photos = library.live_photos()
    elif view == "queue":
        photos = [p for p in live if p.status in _QUEUE_STATUSES]
    elif view == "trash":
        photos = library.trashed_photos()
    elif view.startswith("album:") and view[len("album:"):] in album_names(library):
        name = view[len("album:"):]
        photos = [p for p in live if p.home == name and p.status == STATUS_READY]
    else:
        return None
    return sorted(photos, key=lambda p: (p.created_at, p.id), reverse=True)


def tree(library: Library) -> dict:
    def count(view: str) -> int:
        return len(photos_in_view(library, view) or [])

    return {
        "queue": count("queue"),
        "review": count("review"),
        "live": count("live"),
        "albums": [{"name": n, "view": f"album:{n}", "count": count(f"album:{n}")} for n in album_names(library)],
        "trash": count("trash"),
    }


def summary(photo: Photo) -> dict:
    rev = photo.current_revision()
    return {
        "id": photo.id,
        "kind": photo.kind,
        "media_type": photo.media_type,
        "status": photo.status,
        "home": photo.home,
        "live": photo.live,
        "trashed": photo.trashed,
        "imported": photo.imported,
        "created_at": photo.created_at,
        "has_image": rev is not None,
        # Changes whenever a different revision becomes current, so a
        # browser never shows a stale thumbnail.
        "version": rev.n if rev else None,
        "question": photo.question,
        "error": photo.error,
        # A chat message of this photo is waiting for, or being handled by,
        # the worker.
        "busy": photo.busy,
    }


def detail(photo: Photo, rules: RulesStore | None = None) -> dict:
    out = summary(photo)
    proposal = None
    if rules is not None:
        try:
            found = rules.pending_proposal_for(photo.id)
        except Exception:
            found = None
        if found is not None:
            proposal = {"id": found.id, "text": found.text, "scope": found.scope}
    out.update({
        "sources": [{"name": s.name or s.asset_id, "has_copy": bool(s.file)} for s in photo.sources],
        "history": [
            {"n": r.n, "step": step, "instruction": r.instruction, "rules": r.rules,
             "created_at": r.created_at, "origin": r.origin, "current": r.n == photo.current,
             "reverts_to_step": photo.step_of(r.reverts_to) if r.reverts_to is not None else None}
            for step, r in enumerate(photo.chain())
        ],
        "legacy_notes": photo.legacy_notes,
        "trashed_from": photo.trashed_from,
        "chat": [{"id": m.id, "role": m.role, "text": m.text, "state": m.state, "at": m.at} for m in photo.chat],
        # Why chat is off for this photo (None when it is on).
        "chat_blocked": chat.blocked_reason(photo),
        "proposal": proposal,
    })
    return out


def register_browse_routes(
    app: Flask, store: LibraryStore, revisions: RevisionStore, thumbs: ThumbCache,
    rules: RulesStore | None = None,
) -> None:
    def photo_or_404(photo_id: str) -> Photo:
        photo = store.load().photos.get(photo_id)
        if photo is None:
            abort(404)
        return photo

    def send_stored(relpath: str | None, *, cache: str):
        if not relpath:
            abort(404)
        try:
            path = revisions.path(relpath)
        except RevisionStoreError:
            abort(404)
        if not os.path.isfile(path):
            abort(404)
        response = send_file(path, mimetype=mimetypes.guess_type(path)[0] or "application/octet-stream",
                             conditional=True)
        response.headers["Cache-Control"] = cache
        return response

    @app.get("/api/tree")
    def api_tree():
        return jsonify(tree(store.load()))

    @app.get("/api/photos")
    def api_photos():
        photos = photos_in_view(store.load(), request.args.get("view", "review"))
        if photos is None:
            return jsonify({"error": "unknown view"}), 400
        return jsonify({"photos": [summary(p) for p in photos]})

    @app.get("/api/photos/<photo_id>")
    def api_photo(photo_id: str):
        return jsonify(detail(photo_or_404(photo_id), rules))

    @app.get("/media/<photo_id>/thumb")
    def media_thumb(photo_id: str):
        photo = photo_or_404(photo_id)
        rev = photo.current_revision()
        if photo.media_type != "image" or rev is None:
            abort(404)
        path = thumbs.get(revisions, rev.file)
        if path is None:
            abort(404)
        response = send_file(path, mimetype="image/jpeg", conditional=True)
        response.headers["Cache-Control"] = "private, max-age=86400"
        return response

    @app.get("/media/<photo_id>/full")
    def media_full(photo_id: str):
        rev = photo_or_404(photo_id).current_revision()
        return send_stored(rev.file if rev else None, cache="no-cache")

    @app.get("/media/<photo_id>/rev/<int:n>")
    def media_revision(photo_id: str, n: int):
        rev = photo_or_404(photo_id).revision(n)
        # A revision file is never overwritten, so this can be cached for good.
        return send_stored(rev.file if rev else None, cache="private, max-age=31536000, immutable")
