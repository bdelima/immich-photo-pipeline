"""The Collage Maker place: the photos waiting in the Collage Maker album, making a collage of some of them,
and taking some out of the album (app/collage.py has the rules).

Pictures come through the household routes (/api/household/<id>/thumb and so on), which work for any photo
the pipeline account can see; the photos in the Collage Maker album are among them.
"""
from __future__ import annotations

import time
from typing import Callable

from flask import Flask, jsonify, request

from .. import collage, household
from ..immich_client import ImmichClient, ImmichError

COUNT_TTL = 10.0              # seconds a count for the sidebar is reused


def register_collage_routes(
    app: Flask, immich: ImmichClient, store, cfg, *, extra_immich=(), on_change: Callable[[], None] | None = None,
) -> None:
    album = cfg.collage_album_id
    kept: dict = {}

    def changed() -> None:
        kept.clear()
        if on_change is not None:
            try:
                on_change()
            except Exception:
                app.logger.exception("on_change failed")

    def waiting() -> list[dict]:
        items = collage.waiting(immich, album, store.load())
        household.name_owners(immich, items)
        return items

    def failed(exc: Exception):
        app.logger.warning("collage maker: %s", exc)
        return jsonify({"error": str(exc)}), 502

    def chosen_from(items: list[dict]):
        """The waiting items for the ids of the request, in the order asked. None if the request is wrong."""
        data = request.get_json(silent=True)
        raw = data.get("ids") if isinstance(data, dict) else None
        if not isinstance(raw, list) or not raw or len(raw) > 200:
            return None
        by_id = {i["id"]: i for i in items}
        out, seen = [], set()
        for value in raw:
            if not isinstance(value, str) or not household.valid_asset_id(value):
                return None
            if value not in seen:
                seen.add(value)
                out.append(by_id.get(value) or {"id": value, "missing": True})
        return out

    @app.get("/api/collage/waiting")
    def api_collage_waiting():
        try:
            return jsonify({"items": waiting(), "min": collage.MIN_PHOTOS, "max": collage.MAX_PHOTOS})
        except ImmichError as exc:
            return failed(exc)

    @app.get("/api/collage/count")
    def api_collage_count():
        now = time.monotonic()
        if "count" in kept and now - kept["at"] < COUNT_TTL:
            return jsonify({"count": kept["count"]})
        try:
            n = len(collage.waiting(immich, album, store.load()))
        except ImmichError as exc:
            return failed(exc)
        kept.update(count=n, at=now)
        return jsonify({"count": n})

    @app.post("/api/collage/make")
    def api_collage_make():
        """Makes one collage of the photos in {"ids": [...]}, laid out in that order. 2 to 12 photos, all waiting."""
        try:
            chosen = chosen_from(collage.waiting(immich, album, store.load()))
        except ImmichError as exc:
            return failed(exc)
        if chosen is None:
            return jsonify({"error": "choose photos"}), 400
        if any(c.get("missing") for c in chosen):
            return jsonify({"error": "one of these photos is no longer waiting in Collage Maker"}), 409
        try:
            photo_id = collage.make(store, chosen)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        changed()
        return jsonify({"id": photo_id, "count": len(chosen)})

    @app.post("/api/collage/remove")
    def api_collage_remove():
        """Takes the photos in {"ids": [...]} out of the Collage Maker album. They stay in their owner's library.
        Immich lets only the account that added a photo to an album take it out, so each account's key is tried."""
        try:
            chosen = chosen_from(collage.waiting(immich, album, store.load()))
        except ImmichError as exc:
            return failed(exc)
        if chosen is None:
            return jsonify({"error": "choose photos"}), 400
        done, skipped = [], []
        for c in chosen:
            if c.get("missing"):
                skipped.append({"id": c["id"], "reason": "no longer waiting in Collage Maker"})
                continue
            for client in (immich, *extra_immich):
                try:
                    client.remove_assets_from_album(album, [c["id"]])
                    done.append(c["id"])
                    break
                except ImmichError:
                    continue
            else:
                skipped.append({"id": c["id"], "reason": "the account that added it has no key here"})
        if done:
            changed()
        return jsonify({"done": done, "skipped": skipped})
