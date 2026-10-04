"""The small purpose-built web UI: pick which managed album is mirrored
into Live, and (at /rules) review the recipe rules the reviewers have
taught -- add one, retire one, or confirm a proposed one. See the design
doc's "Web UI v1 scope" open question for what else might land here later.
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

from ..config import Config
from ..health import HealthStore
from ..immich_client import ImmichClient
from ..rules import MAX_ACTIVE_RULES, SCOPE_LABELS, SCOPES, RulesFull, RulesStore
from ..state import StateStore

log = logging.getLogger(__name__)


def create_app(
    cfg: Config, immich: ImmichClient, store: StateStore, health: HealthStore,
    rules: RulesStore | None = None,
) -> Flask:
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

    if rules is not None:
        _register_rules_routes(app, rules)

    return app


def _rule_json(rule) -> dict:
    return {
        "id": rule.id, "text": rule.text, "scope": rule.scope, "status": rule.status,
        "origin": rule.origin, "created_at": rule.created_at,
    }


def _register_rules_routes(app: Flask, rules: RulesStore) -> None:
    @app.get("/rules")
    def rules_page():
        all_rules = rules.all()
        return render_template(
            "rules.html",
            active=[r for r in all_rules if r.status == "active"],
            proposed=[r for r in all_rules if r.status == "proposed"],
            scopes=SCOPES, scope_labels=SCOPE_LABELS, limit=MAX_ACTIVE_RULES,
        )

    @app.get("/api/rules")
    def api_rules():
        return jsonify({
            "limit": MAX_ACTIVE_RULES,
            "rules": [_rule_json(r) for r in rules.all() if r.status != "retired"],
        })

    @app.post("/api/rules")
    def api_add_rule():
        body = request.get_json(silent=True) or {}
        try:
            rule = rules.add(body.get("text", ""), body.get("scope", "all"), origin="web")
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except RulesFull as exc:
            return jsonify({"error": str(exc)}), 409
        return jsonify({"ok": True, "rule": _rule_json(rule)}), 201

    @app.post("/api/rules/<rule_id>/retire")
    def api_retire_rule(rule_id: str):
        rule = rules.set_status(rule_id, "retired")
        if rule is None:
            return jsonify({"error": f"unknown rule {rule_id!r}"}), 404
        return jsonify({"ok": True, "rule": _rule_json(rule)})

    @app.post("/api/rules/<rule_id>/activate")
    def api_activate_rule(rule_id: str):
        try:
            rule = rules.set_status(rule_id, "active")
        except RulesFull as exc:
            return jsonify({"error": str(exc)}), 409
        if rule is None:
            return jsonify({"error": f"unknown rule {rule_id!r}"}), 404
        return jsonify({"ok": True, "rule": _rule_json(rule)})
