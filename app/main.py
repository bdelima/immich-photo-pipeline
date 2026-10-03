"""Entrypoint: runs the poll loop and the web UI side by side."""
from __future__ import annotations

import logging
import threading
import time

from .config import Config
from .immich_client import ImmichClient
from .pipeline import Pipeline
from .recipe_runner import RecipeRunner
from .state import StateStore
from .webui.server import create_app

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("immich-photo-pipeline")


def poll_forever(pipeline: Pipeline, interval_seconds: int) -> None:
    while True:
        try:
            pipeline.run_once()
        except Exception:
            log.exception("poll cycle failed; will retry next interval")
        time.sleep(interval_seconds)


def main() -> None:
    cfg = Config.from_env()
    immich = ImmichClient(cfg.immich_url, cfg.immich_api_key)
    recipe = RecipeRunner(cfg.claude_binary, cfg.recipe_skill_path)
    store = StateStore(cfg.state_path)
    pipeline = Pipeline(cfg, immich, recipe, store)

    poll_thread = threading.Thread(
        target=poll_forever, args=(pipeline, cfg.poll_interval_seconds), daemon=True,
    )
    poll_thread.start()
    log.info("poll loop started (interval=%ss)", cfg.poll_interval_seconds)

    app = create_app(cfg, immich, store)
    app.run(host=cfg.webui_host, port=cfg.webui_port)


if __name__ == "__main__":
    main()
