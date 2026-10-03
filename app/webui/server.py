"""The small purpose-built web UI: v1 scope is exactly one job — pick
which managed album is mirrored into Live. See the design doc's "Web UI
v1 scope" open question for what else might land here later.
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

from ..config import Config
from ..health import HealthStore
from ..immich_client import ImmichClient
from ..state import StateStore

log = logging.getLogger(__name__)


def create_app(cfg: Config, immich: ImmichClient, store: StateStore, health: HealthStore) -> Flask:
    app = Flask(__name__)

    @app.get("/healthz")
    def healthz():
        snap = health.snapshot()
        if snap.claude_auth_ok:
            return jsonify({"status": "ok"}), 200
        return jsonify({"status": "unhealthy", "reason": snap.last_error}), 503

    @app.get("/")
    def index():
        state = store.load()
        return render_template(
            "index.html",
            albums=sorted(state.watched_albums.keys()),
            live_source=state.live_source_album,
        )

    @app.get("/api/albums")
    def api_albums():
        state = store.load()
        return jsonify({
            "watched_albums": state.watched_albums,
            "live_source_album": state.live_source_album,
        })

    @app.post("/api/live-album")
    def set_live_album():
        name = (request.get_json(silent=True) or {}).get("name")
        state = store.load()
        if name not in state.watched_albums:
            return jsonify({"error": f"unknown album {name!r}"}), 400
        target_album_id = state.watched_albums[name]
        current = {a.id for a in immich.list_album_assets(target_album_id)}
        live = {a.id for a in immich.list_album_assets(cfg.live_album_id)}
        immich.add_assets_to_album(cfg.live_album_id, list(current - live))
        immich.remove_assets_from_album(cfg.live_album_id, list(live - current))
        state.live_source_album = name
        store.save(state)
        return jsonify({"ok": True, "live_source_album": name})

    return app
