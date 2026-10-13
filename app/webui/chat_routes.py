"""The route behind a photo's chat box: POST a message, which is queued for
the worker (see app/chat.py). The conversation itself is read from the
photo's detail (GET /api/photos/<id>), which the page polls while the photo
is busy."""
from __future__ import annotations

from typing import Callable

from flask import Flask, jsonify, request

from .. import chat, geocode, locale, watermark
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
        """Names what is in the photo, or suggests three short poetic titles for
        it (a Claude run, so it can take a few seconds), for the watermark
        builder to use. `avoid` lists earlier answers, so a retry differs.
        Changes nothing."""
        data = request.get_json(silent=True)
        hint = data.get("hint") if isinstance(data, dict) else None
        style = (data.get("style") if isinstance(data, dict) else None) or "subject"
        avoid = data.get("avoid") if isinstance(data, dict) else None
        try:
            return jsonify({"names": watermark.identify(store, revisions, recipe, photo_id, hint, style, avoid)})
        except chat.ChatError as exc:
            return jsonify({"error": str(exc)}), exc.status

    @app.get("/api/home-country")
    def api_home_country():
        """The country for the browser's time zone (`?tz=America/Chicago`), so
        the builder can leave out a place's country when it is the home one.
        "" when it cannot be worked out."""
        return jsonify({"country": locale.country_for_timezone(request.args.get("tz", ""))})

    @app.get("/api/photos/<photo_id>/place")
    def api_place(photo_id: str):
        """The place at the photo's coordinates, for when Immich did not name
        one ("" when there are no coordinates or no answer). Changes nothing."""
        photo = store.load().photos.get(photo_id)
        if photo is None:
            return jsonify({"error": "no such photo"}), 404
        coordinates = watermark.parts(photo)["coordinates"]
        return jsonify({"place": geocode.reverse(*coordinates) if coordinates else ""})

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
