"""The route behind a photo's chat box: POST a message, which is queued for
the worker (see app/chat.py). The conversation itself is read from the
photo's detail (GET /api/photos/<id>), which the page polls while the photo
is busy."""
from __future__ import annotations

from typing import Callable

from flask import Flask, jsonify, request

from .. import chat, watermark
from ..library import LibraryStore


def register_chat_routes(
    app: Flask, store: LibraryStore, on_job: Callable[[], None] | None = None,
    recipe=None, revisions=None,
) -> None:
    @app.post("/api/photos/<photo_id>/chat")
    def api_chat(photo_id: str):
        data = request.get_json(silent=True)
        text = data.get("text") if isinstance(data, dict) else None
        try:
            message_id = chat.enqueue(store, photo_id, text)
        except chat.ChatError as exc:
            return jsonify({"error": str(exc)}), exc.status
        if on_job is not None:
            try:
                on_job()
            except Exception:  # the worker also looks on its own every few seconds
                app.logger.exception("on_job failed")
        return jsonify({"id": message_id})

    @app.post("/api/photos/<photo_id>/identify")
    def api_identify(photo_id: str):
        """Names what is in the photo, or gives it a short poetic title (a
        Claude run, so it can take a few seconds), for the watermark builder to
        use. Changes nothing."""
        data = request.get_json(silent=True)
        hint = data.get("hint") if isinstance(data, dict) else None
        style = (data.get("style") if isinstance(data, dict) else None) or "subject"
        try:
            return jsonify({"name": watermark.identify(store, revisions, recipe, photo_id, hint, style)})
        except chat.ChatError as exc:
            return jsonify({"error": str(exc)}), exc.status

    @app.post("/api/photos/<photo_id>/watermark")
    def api_watermark(photo_id: str):
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        try:
            message_id = watermark.add(store, photo_id, data.get("title"), data.get("subtitle"), data.get("third"))
        except chat.ChatError as exc:
            return jsonify({"error": str(exc)}), exc.status
        if on_job is not None:
            try:
                on_job()
            except Exception:
                app.logger.exception("on_job failed")
        return jsonify({"id": message_id})
