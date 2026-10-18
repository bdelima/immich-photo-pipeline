"""What the Photos view reads: the months of everyone's timeline, one month's photos,
thumbnails and previews, video playback, and the details of one photo.

Pictures are passed through from Immich with the pipeline's own key (the household members
have partnered it, see app/household.py), so the browser never talks to Immich itself.
Thumbnails are kept on disk (app/immich_thumbs.py) and every picture carries an ETag, so the
browser can ask "has it changed?" and be told no without the bytes again.

What a person can do with the photos they see (when the library and the entry queues are
known): search, send them to an entry queue, download them, and move them to Immich's trash.
Everything that changes a photo is done with the key of the account that owns it (the
IMMICH_EXTRA_API_KEY lines): Immich only lets an owner add their photo to an album or trash
it. A photo whose owner has no key here is skipped, with the reason.
"""
from __future__ import annotations

import hashlib

from typing import Callable

from flask import Flask, Response, abort, jsonify, request, send_file, stream_with_context

from .. import actions, household, metadata
from ..immich_client import ImmichClient, ImmichError
from ..immich_thumbs import ImmichThumbCache

_PASS_HEADERS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges")


# How long a browser may reuse a picture without asking. After that it asks with its ETag.
PICTURE_MAX_AGE = 3600


MAX_IDS = 200                 # photos handled by one request
SEND_DESTINATIONS = ("wallpaper", "collage", "import")


def register_household_routes(
    app: Flask, immich: ImmichClient, thumb_cache: ImmichThumbCache | None = None, *,
    store=None, revisions=None, cfg=None, extra_immich=(), on_change: Callable[[], None] | None = None,
) -> None:
    state: dict = {}

    def own_id() -> str:
        if "own" not in state:
            state["own"] = immich.get_my_user_id()
        return state["own"]

    def owner_clients() -> dict:
        """{account id: client} for every account whose key the pipeline holds, its own included."""
        if "owners" not in state:
            found = {own_id(): immich}
            for client in extra_immich:
                try:
                    found[client.get_my_user_id()] = client
                except ImmichError:
                    app.logger.warning("household photos: an extra Immich key could not be read")
            state["owners"] = found
        return state["owners"]

    def pipeline_states() -> dict:
        return household.pipeline_states(store.load()) if store is not None else {}

    def changed() -> None:
        if on_change is not None:
            try:
                on_change()
            except Exception:
                app.logger.exception("on_change failed")

    def body() -> dict:
        data = request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def ids_from(data) -> list[str] | None:
        """The asset ids of a request, in order and without repeats; None if the request is wrong as a whole."""
        raw = data.get("ids") if isinstance(data, dict) else None
        if not isinstance(raw, list) or not raw or len(raw) > MAX_IDS:
            return None
        out: list[str] = []
        for value in raw:
            if not isinstance(value, str) or not household.valid_asset_id(value):
                return None
            if value not in out:
                out.append(value)
        return out

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
            items = household.month_items(
                immich, bucket, own_id(), pipeline_states(), include_sent=request.args.get("sent") == "1")
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


    # ---- search -----------------------------------------------------------------------

    @app.get("/api/household/search")
    def api_household_search():
        """One page of results. Query: q, mode (context, file_name, description, path, ocr), from, to,
        country, state, city, make, model, lens, type (photo or video), owner (an account id),
        people (comma-separated ids), favorites, archive, not_in_album, sent (1 = include photos already
        in the pipeline), page."""
        args = request.args
        filters = {k: args.get(k, "") for k in ("from", "to", "country", "state", "city", "make", "model", "lens", "type")}
        filters["people"] = [p for p in args.get("people", "").split(",") if p]
        for key in ("favorites", "archive", "not_in_album"):
            filters[key] = args.get(key) == "1"
        owner = args.get("owner", "")
        if len(owner) > 64:
            return jsonify({"error": "bad owner"}), 400
        try:
            page = int(args.get("page", "1"))
        except ValueError:
            return jsonify({"error": "bad page"}), 400
        try:
            items, nxt = household.search_items(
                immich, own_id(), args.get("q", ""), args.get("mode", "context"), filters, page,
                pipeline_states(), include_sent=args.get("sent") == "1", owner_id=owner)
            household.name_owners(immich, items)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except ImmichError as exc:
            return failed(exc)
        return jsonify({"items": items, "next": nxt})

    @app.get("/api/household/suggest")
    def api_household_suggest():
        """Values Immich knows for a filter box (countries, camera makes and so on)."""
        kind = request.args.get("type", "")
        if kind not in household.SUGGESTION_KINDS:
            return jsonify({"error": "bad type"}), 400
        extra = {k: request.args[k] for k in ("country", "state", "make", "model") if request.args.get(k)}
        try:
            return jsonify({"values": immich.suggestions(kind, **extra)})
        except ImmichError as exc:
            return failed(exc)

    @app.get("/api/household/filters")
    def api_household_filters():
        """What the search panel offers besides text: whose photos (the accounts the pipeline holds a
        key for) and the people Immich has named."""
        owners = []
        try:
            me = own_id()
            for uid, client in owner_clients().items():
                if uid != me:
                    owners.append({"id": uid, "name": immich.get_user_name(uid) or client.get_user_name(uid)})
            people = immich.list_people()
        except ImmichError as exc:
            return failed(exc)
        owners.sort(key=lambda o: o["name"].lower())
        return jsonify({"owners": owners, "people": people})

    # ---- what a person can do with the photos ----------------------------------------------

    def per_photo(ids, one):
        """Runs `one(asset, owner_client)` for each id and gathers {"done": [...], "skipped": [{"id", "reason"}]}.
        A photo Immich does not know, the pipeline's own, or one whose owner has no key is skipped."""
        done, skipped = [], []
        me = own_id()
        for asset_id in ids:
            try:
                asset = immich.get_asset(asset_id)
            except ImmichError as exc:
                skipped.append({"id": asset_id, "reason": str(exc)})
                continue
            if asset is None:
                skipped.append({"id": asset_id, "reason": "Immich no longer has this photo"})
                continue
            owner = asset.get("ownerId") or ""
            if owner == me:
                skipped.append({"id": asset_id, "reason": "it is the pipeline's own copy"})
                continue
            client = owner_clients().get(owner)
            if client is None:
                who = immich.get_user_name(owner) or "its owner"
                skipped.append({"id": asset_id, "reason": f"the pipeline has no key for {who}"})
                continue
            try:
                reason = one(asset, client)
            except ImmichError as exc:
                reason = str(exc)
            if reason:
                skipped.append({"id": asset_id, "reason": reason})
            else:
                done.append(asset_id)
        return {"done": done, "skipped": skipped}

    def album_for(dest: str) -> str:
        return {"wallpaper": cfg.wallpaper_album_id, "collage": cfg.collage_album_id, "import": cfg.import_album_id}[dest]

    @app.post("/api/household/send")
    def api_household_send():
        """Sends photos to an entry queue ({"ids": [...], "dest": "wallpaper" | "collage" | "import"}).
        A photo already in the pipeline is skipped, and so is a video bound for a collage. A photo whose
        processed copy is only in the web trash is skipped too, unless the request says "purge": true:
        then, once the photo has been added to the queue, the processed photos made from it are deleted
        for good (a whole collage, if it was part of one), so the original is not "known" any more. The
        purge happens only for a photo that was sent, so a refused send never costs anyone a processed photo."""
        if store is None or cfg is None:
            return jsonify({"error": "sending is not available"}), 404
        data = body()
        ids, dest, purge = ids_from(data), data.get("dest"), data.get("purge") is True
        if ids is None or dest not in SEND_DESTINATIONS:
            return jsonify({"error": "choose photos and a destination"}), 400
        if purge and revisions is None:
            return jsonify({"error": "purging is not available"}), 404
        album = album_for(dest)
        if not album:
            return jsonify({"error": "that entry queue is not set up"}), 400
        states = pipeline_states()
        purged: list[str] = []

        def one(asset, client):
            state = states.get(asset["id"])
            if state == household.IN_PIPELINE:
                return "already in the pipeline"
            if state == household.IN_PIPELINE_TRASH and not purge:
                return "in the pipeline's trash"
            if dest == "collage" and asset.get("type") == "VIDEO":
                return "a video can't go in a collage"
            duplicate = False
            for result in client.add_assets_to_album(album, [asset["id"]]):
                if result.get("success") is False:
                    if result.get("error") != "duplicate":
                        return f"Immich refused: {result.get('error')}"
                    duplicate = True
            if state == household.IN_PIPELINE_TRASH:
                # Sent (or already waiting in the queue from an earlier try): now the processed copy can go.
                trashed = [p.id for p in household.photos_made_from(store.load(), [asset["id"]]) if p.trashed]
                if trashed:
                    purged.extend(actions.delete(store, revisions, trashed).done)
                return ""
            return "already in that queue" if duplicate else ""

        out = per_photo(ids, one)
        out["purged"] = purged
        if out["done"] or purged:
            changed()
        return jsonify(out)

    @app.post("/api/household/purge")
    def api_household_purge():
        """Deletes for good the processed photos in the pipeline's web trash that were made from these
        originals, so the originals are forgotten and can be sent again. Nothing outside the trash is touched,
        and the originals stay in their owners' Immich libraries."""
        if store is None or revisions is None:
            return jsonify({"error": "purging is not available"}), 404
        ids = ids_from(body())
        if ids is None:
            return jsonify({"error": "choose photos"}), 400
        trashed = [p.id for p in household.photos_made_from(store.load(), ids) if p.trashed]
        if not trashed:
            return jsonify({"purged": [], "skipped": []})
        result = actions.delete(store, revisions, trashed)
        if result.done:
            changed()
        return jsonify({"purged": result.done, "skipped": result.as_dict().get("skipped", [])})

    @app.post("/api/household/trash")
    def api_household_trash():
        """Moves photos to the trash of the account that owns them (Immich keeps them there for its own
        retention period). The pipeline's copies, if any, are not touched."""
        ids = ids_from(body())
        if ids is None:
            return jsonify({"error": "choose photos"}), 400
        return jsonify(per_photo(ids, lambda asset, client: client.delete_assets([asset["id"]], force=False) or ""))

    def attachment(upstream, name: str, fallback_type: str):
        def chunks():
            try:
                yield from upstream.iter_content(chunk_size=1 << 16)
            finally:
                upstream.close()

        resp = Response(stream_with_context(chunks()), mimetype=upstream.headers.get("Content-Type", fallback_type))
        resp.headers.set("Content-Disposition", "attachment", filename=name)
        if "Content-Length" in upstream.headers:
            resp.headers["Content-Length"] = upstream.headers["Content-Length"]
        return resp

    @app.get("/api/household/<asset_id>/download")
    def api_household_download(asset_id: str):
        asset_id = asset_id_or_404(asset_id)
        try:
            asset = immich.get_asset(asset_id)
            if asset is None or asset.get("ownerId") == own_id():
                abort(404)
            upstream = immich.open_original(asset_id)
        except ImmichError as exc:
            return failed(exc)
        return attachment(upstream, asset.get("originalFileName") or asset_id, "application/octet-stream")

    @app.post("/api/household/download")
    def api_household_download_many():
        """Several originals as one zip. Sent as a form (ids = comma-separated), so the browser saves the answer."""
        ids = [i for i in request.form.get("ids", "").split(",") if i]
        if not ids or len(ids) > MAX_IDS or not all(household.valid_asset_id(i) for i in ids):
            return jsonify({"error": "choose photos"}), 400
        try:
            me = own_id()
            for asset_id in ids:
                asset = immich.get_asset(asset_id)
                if asset is None or asset.get("ownerId") == me:
                    return jsonify({"error": "that selection includes a photo that can't be downloaded"}), 400
            upstream = immich.open_archive(ids)
        except ImmichError as exc:
            return failed(exc)
        return attachment(upstream, "photos.zip", "application/zip")
