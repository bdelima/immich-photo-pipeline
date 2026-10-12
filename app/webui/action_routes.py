"""The routes behind the action bar: promote, move, trash, restore and revert.

Each takes JSON, acts through app/actions.py, and answers
{"done": [...], "skipped": [{"id", "reason"}]}. A request that is wrong as a
whole (no ids, a bad album name) is a 400; a photo that can't be acted on is
only skipped. After anything changed, `on_change` is called so the poll
cycle brings Immich into line straight away.
"""
from __future__ import annotations

from typing import Callable

from flask import Flask, jsonify, request

from .. import actions
from ..config import Config
from ..library import LibraryStore
from ..revisions import RevisionStore


def register_action_routes(
    app: Flask, store: LibraryStore, revisions: RevisionStore, cfg: Config | None,
    on_change: Callable[[], None] | None = None,
) -> None:
    reserved = set()
    if cfg is not None:
        reserved = {cfg.collage_album_name, cfg.wallpaper_album_name, cfg.review_album_name, cfg.live_album_name}

    def changed() -> None:
        if on_change is not None:
            try:
                on_change()
            except Exception:  # never fail a request because the nudge failed
                app.logger.exception("on_change failed")

    def body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def respond(fn):
        try:
            result = fn()
        except actions.ActionError as exc:
            return jsonify({"error": str(exc)}), 400
        if result.done:
            changed()
        return jsonify(result.as_dict())

    @app.post("/api/photos/promote")
    def api_promote():
        data = body()
        return respond(lambda: actions.promote(store, data.get("ids"), live=data.get("live", True) is not False))

    @app.post("/api/photos/move")
    def api_move():
        data = body()
        return respond(lambda: actions.move(store, data.get("ids"), data.get("home"), reserved))

    @app.post("/api/photos/trash")
    def api_trash():
        return respond(lambda: actions.trash(store, body().get("ids")))

    @app.post("/api/photos/restore")
    def api_restore():
        return respond(lambda: actions.restore(store, body().get("ids")))

    @app.post("/api/photos/<photo_id>/revert")
    def api_revert(photo_id: str):
        try:
            out = actions.revert(store, revisions, photo_id, body().get("step"))
        except actions.ActionError as exc:
            return jsonify({"error": str(exc)}), 400
        if not out.get("unchanged"):
            changed()
        return jsonify(out)
