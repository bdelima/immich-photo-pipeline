"""The small purpose-built web UI: browsing the library (see browse.py), and
(at /rules) the recipe rules the reviewers have taught -- add one, retire
one, or confirm a proposed one. Actions on photos are built on top of this.
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, request

from ..config import Config
from ..health import HealthStore
from ..immich_client import ImmichClient
from ..library import REVIEW, STATUS_FAILED, STATUS_PROCESSING, Library, LibraryStore
from ..revisions import RevisionStore
from ..rules import MAX_ACTIVE_RULES, SCOPE_LABELS, SCOPES, RulesFull, RulesStore
from ..thumbs import ThumbCache
from .browse import register_browse_routes

log = logging.getLogger(__name__)


def create_app(
    cfg: Config, immich: ImmichClient, store: LibraryStore, health: HealthStore,
    rules: RulesStore | None = None, revisions: RevisionStore | None = None,
    thumbs: ThumbCache | None = None,
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
        return render_template("index.html", overview=_overview(store.load()))

    @app.get("/api/albums")
    def api_albums():
        return jsonify(_overview(store.load()))

    if store is not None and revisions is not None and thumbs is not None:
        register_browse_routes(app, store, revisions, thumbs)
    if rules is not None:
        _register_rules_routes(app, rules)

    return app


def _overview(library: Library) -> dict:
    """Counts only: what each album holds, Review, Live, and the queue."""
    photos = [p for p in library.photos.values() if not p.trashed]
    return {
        "albums": {name: len(library.in_home(name)) for name in sorted(library.album_names())},
        "review": len([p for p in photos if p.home == REVIEW and p.status == "ready"]),
        "live": len(library.live_photos()),
        "processing": len([p for p in photos if p.status == STATUS_PROCESSING]),
        "failed": len([p for p in photos if p.status == STATUS_FAILED]),
        "trashed": len(library.trashed_photos()),
    }


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
