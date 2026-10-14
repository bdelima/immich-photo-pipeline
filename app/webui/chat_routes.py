"""The route behind a photo's chat box: POST a message, which is queued for
the worker (see app/chat.py). The conversation itself is read from the
photo's detail (GET /api/photos/<id>), which the page polls while the photo
is busy."""
from __future__ import annotations

from typing import Callable

from flask import Flask, Response, jsonify, request

from .. import chat, locale, watermark
from ..library import LibraryStore


def register_chat_routes(
    app: Flask, store: LibraryStore, on_job: Callable[[], None] | None = None,
    recipe=None, revisions=None, on_change: Callable[[], None] | None = None, immich=None, extra_immich=(),
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
        """The country this container is in (from its own time zone, else its
        locale), so the builder can leave out a place's country when it is the
        home one. "" when it cannot be worked out."""
        return jsonify({"country": locale.container_country()})

    @app.post("/api/photos/<photo_id>/locate")
    def api_locate(photo_id: str):
        """Names the place at the photo's coordinates from one source
        ({"source": "osm"} or {"source": "claude"}), for the watermark builder
        to offer next to what Immich knew. Changes nothing."""
        data = request.get_json(silent=True)
        source = data.get("source") if isinstance(data, dict) else None
        try:
            return jsonify({"place": watermark.locate(store, recipe, photo_id, source)})
        except chat.ChatError as exc:
            return jsonify({"error": str(exc)}), exc.status

    @app.post("/api/photos/<photo_id>/watermark")
    def api_watermark(photo_id: str):
        """Draws the watermark on the photo as a new step (here, not by Claude)
        and answers {"step": n}."""
        data = request.get_json(silent=True)
        try:
            step = watermark.apply(store, revisions, photo_id, data if isinstance(data, dict) else {})
        except chat.ChatError as exc:
            return jsonify({"error": str(exc)}), exc.status
        if on_change is not None:
            try:
                on_change()
            except Exception:
                app.logger.exception("on_change failed")
        return jsonify({"step": step})

    @app.post("/api/photos/<photo_id>/watermark/preview")
    def api_watermark_preview(photo_id: str):
        """A PNG of the corner the watermark will sit on, drawn on the original (or a plain
        panel when there is none), with the text drawn as it will be. The corner, the colours
        used and what it was drawn on ("original" or "none") are in the X-Watermark-Corner,
        X-Watermark-Colors and X-Watermark-Source headers. Changes nothing."""
        data = request.get_json(silent=True)
        try:
            png, info = watermark.preview(store, revisions, photo_id, data if isinstance(data, dict) else {})
        except chat.ChatError as exc:
            return jsonify({"error": str(exc)}), exc.status
        return Response(png, mimetype="image/png", headers={
            "Cache-Control": "no-store", "X-Watermark-Corner": info["corner"], "X-Watermark-Colors": ",".join(info["colors"]),
            "X-Watermark-Source": info["source"]})

    @app.post("/api/photos/<photo_id>/watermark/whole")
    def api_watermark_whole(photo_id: str):
        """The whole original with the watermark drawn on it as the form stands (a JPEG for the
        screen), for a click on the preview. 404 when there is no original on file. Changes nothing."""
        data = request.get_json(silent=True)
        try:
            jpeg, info = watermark.whole(store, revisions, photo_id, data if isinstance(data, dict) else {})
        except chat.ChatError as exc:
            return jsonify({"error": str(exc)}), exc.status
        return Response(jpeg, mimetype="image/jpeg", headers={"Cache-Control": "no-store", "X-Watermark-Corner": info["corner"]})

    @app.get("/api/photos/<photo_id>/watermark/options")
    def api_watermark_options(photo_id: str):
        """What the pulldowns under the watermark's fields offer (see watermark.options)."""
        try:
            return jsonify(watermark.options(store, immich, photo_id, extra_immich))
        except chat.ChatError as exc:
            return jsonify({"error": str(exc)}), exc.status

    @app.post("/api/photos/<photo_id>/font")
    def api_font(photo_id: str):
        """Asks Claude for a font and colour that suit the photo and its watermark text
        ({"texts": [...], "hint": "..."}); a Claude run. Changes nothing but the list of
        fonts the form offers."""
        data = request.get_json(silent=True)
        data = data if isinstance(data, dict) else {}
        try:
            return jsonify(watermark.suggest_font(store, revisions, recipe, photo_id, data.get("texts"), data.get("hint")))
        except chat.ChatError as exc:
            return jsonify({"error": str(exc)}), exc.status
