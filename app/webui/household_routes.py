"""What the Photos view reads: the months of everyone's timeline, one month's photos,
thumbnails and previews, video playback, and the details of one photo.

All read-only. Pictures are passed through from Immich with the pipeline's own key (the
household members have partnered it, see app/household.py), so the browser never talks to
Immich itself. Thumbnails are kept on disk (app/immich_thumbs.py) and every picture carries
an ETag, so the browser can ask "has it changed?" and be told no without the bytes again.
"""
from __future__ import annotations

import hashlib

from flask import Flask, Response, abort, jsonify, request, send_file, stream_with_context

from .. import household, metadata
from ..immich_client import ImmichClient, ImmichError
from ..immich_thumbs import ImmichThumbCache

_PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")


# How long a browser may reuse a picture without asking. After that it asks with its ETag.
PICTURE_MAX_AGE = 3600


def register_household_routes(app: Flask, immich: ImmichClient, thumb_cache: ImmichThumbCache | None = None) -> None:
    state: dict = {}

    def own_id() -> str:
        if "own" not in state:
            state["own"] = immich.get_my_user_id()
        return state["own"]

    def failed(exc: Exception):
        app.logger.warning("household photos: %s", exc)
        return jsonify({"error": str(exc)}), 502

    def asset_id_or_404(asset_id: str) -> str:
        if not household.valid_asset_id(asset_id):
            abort(404)
        return asset_id

    @app.get("/api/household/months")
    def api_household_months():
        try:
            return jsonify({"months": household.months(immich)})
        except ImmichError as exc:
            return failed(exc)

    @app.get("/api/household/month")
    def api_household_month():
        bucket = request.args.get("bucket", "")
        if not household.valid_bucket(bucket):
            return jsonify({"error": "bad month"}), 400
        try:
            items = household.month_items(immich, bucket, own_id())
            household.name_owners(immich, items)
        except ImmichError as exc:
            return failed(exc)
        return jsonify({"items": items})

    def image(asset_id: str, size: str):
        asset_id = asset_id_or_404(asset_id)
        cache_control = f"private, max-age={PICTURE_MAX_AGE}"
        kept = thumb_cache.get(asset_id, size) if thumb_cache else None
        if kept is None:
            try:
                data, content_type = immich.fetch_image(asset_id, size)
            except ImmichError as exc:
                return failed(exc)
            if thumb_cache:
                kept = thumb_cache.put(asset_id, size, data, content_type)
            if kept is None:        # not kept (no cache, or it could not be written): serve what we have
                etag = '"' + hashlib.sha1(data).hexdigest() + '"'
                resp = Response(data, mimetype=content_type, headers={"Cache-Control": cache_control, "ETag": etag})
                return resp.make_conditional(request)
        path, mime = kept
        # send_file adds an ETag and Last-Modified from the file and answers a repeat request with 304.
        resp = send_file(path, mimetype=mime, conditional=True, max_age=PICTURE_MAX_AGE)
        resp.headers["Cache-Control"] = cache_control
        return resp

    @app.get("/api/household/<asset_id>/thumb")
    def api_household_thumb(asset_id: str):
        return image(asset_id, "thumbnail")

    @app.get("/api/household/<asset_id>/preview")
    def api_household_preview(asset_id: str):
        return image(asset_id, "preview")

    @app.get("/api/household/<asset_id>/video")
    def api_household_video(asset_id: str):
        try:
            upstream = immich.open_video(asset_id_or_404(asset_id), request.headers.get("Range"))
        except ImmichError as exc:
            return failed(exc)

        def chunks():
            try:
                yield from upstream.iter_content(chunk_size=1 << 16)
            finally:
                upstream.close()

        headers = {h: upstream.headers[h] for h in _PASS_HEADERS if h in upstream.headers}
        return Response(stream_with_context(chunks()), status=upstream.status_code, headers=headers)

    @app.get("/api/household/<asset_id>")
    def api_household_asset(asset_id: str):
        """What Immich knows about one photo, for the page's info panel."""
        try:
            item = immich.get_asset(asset_id_or_404(asset_id))
            if item is None:
                return jsonify({"error": "Immich no longer has this photo"}), 404
            meta = metadata.meta_from_asset(item)
            if "owner_name" not in meta and item.get("ownerId"):
                meta["owner_name"] = immich.get_user_name(item["ownerId"])
        except ImmichError as exc:
            return failed(exc)
        return jsonify({
            "id": item.get("id"), "file_name": item.get("originalFileName") or "",
            "video": item.get("type") == "VIDEO", "meta": meta,
        })
