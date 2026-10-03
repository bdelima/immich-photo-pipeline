"""Entrypoint: runs the poll loop, the background Claude-auth probe, and
the web UI side by side."""
from __future__ import annotations

import logging
import threading
import time

from .config import Config
from .health import HealthStore
from .immich_client import ImmichClient
from .pipeline import Pipeline
from .recipe_runner import RecipeRunner, format_auth_instructions
from .state import StateStore
from .webui.server import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("immich-photo-pipeline")


def auth_probe_forever(recipe: RecipeRunner, health: HealthStore, token_file: str, interval_seconds: int) -> None:
    """Checks whether a Claude session can actually be established, on its
    own slower cadence (a probe spends a real, if trivial, invocation).
    Logs the setup instructions once per failure, not on every retry, so
    an unattended container doesn't spam its own logs."""
    was_ok: bool | None = None
    while True:
        ok, err = recipe.check_auth()
        if ok:
            health.set_auth_ok()
            if was_ok is not True:
                log.info("Claude Code session OK; pipeline will resume processing")
        else:
            health.set_auth_failed(err or "unknown error")
            if was_ok is not False:
                log.error("Claude auth check failed: %s", err)
                log.error(format_auth_instructions(token_file))
        was_ok = ok
        time.sleep(interval_seconds)


def poll_forever(pipeline: Pipeline, health: HealthStore, interval_seconds: int) -> None:
    while True:
        if health.snapshot().claude_auth_ok:
            try:
                pipeline.run_once()
            except Exception:
                log.exception("poll cycle failed; will retry next interval")
        else:
            log.debug("skipping poll cycle: no working Claude session yet")
        time.sleep(interval_seconds)


def main() -> None:
    cfg = Config.from_env()
    immich = ImmichClient(cfg.immich_url, cfg.immich_api_key)
    recipe = RecipeRunner(cfg.claude_binary, cfg.recipe_skill_path, cfg.claude_oauth_token_file)
    store = StateStore(cfg.state_path)
    health = HealthStore()
    pipeline = Pipeline(cfg, immich, recipe, store)

    auth_thread = threading.Thread(
        target=auth_probe_forever,
        args=(recipe, health, cfg.claude_oauth_token_file, cfg.claude_auth_check_interval_seconds),
        daemon=True,
    )
    auth_thread.start()

    poll_thread = threading.Thread(
        target=poll_forever, args=(pipeline, health, cfg.poll_interval_seconds), daemon=True,
    )
    poll_thread.start()
    log.info("poll loop started (interval=%ss)", cfg.poll_interval_seconds)

    app = create_app(cfg, immich, store, health)
    app.run(host=cfg.webui_host, port=cfg.webui_port)


if __name__ == "__main__":
    main()
