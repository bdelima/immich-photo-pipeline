"""Entrypoint: runs the poll loop, the worker, the background Claude-auth
probe, and the web UI side by side."""
from __future__ import annotations

import logging
import threading
import time

from .albums import ensure_core_albums
from .config import Config
from .cycle import Cycle
from .health import HealthStore
from .immich_client import ImmichClient, ImmichError
from .intake import Intake
from .library import LibraryStore
from .recipe_runner import RecipeRunner, format_auth_instructions
from .revisions import RevisionStore
from .rules import RulesStore
from .sharing import ensure_all_shared, resolve_user_ids
from .secrets import resolve_secret, resolve_secret_list
from .webui.server import create_app
from .worker import Worker

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("immich-photo-pipeline")


def wait_for_immich_ready(immich: ImmichClient, cfg: Config) -> Config:
    """Resolves/bootstraps the four core albums by name (see albums.py).
    Retries rather than crashing if Immich isn't reachable yet -- a real
    possibility at container startup, e.g. under compose without a
    healthcheck-gated depends_on."""
    while True:
        try:
            return ensure_core_albums(immich, cfg)
        except ImmichError:
            log.exception("Immich not ready yet; retrying in 10s")
            time.sleep(10)


def auth_probe_forever(recipe: RecipeRunner, health: HealthStore, secrets_file: str, interval_seconds: int) -> None:
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
                log.error(format_auth_instructions(secrets_file))
        was_ok = ok
        time.sleep(interval_seconds)


def poll_forever(cycle: Cycle, interval_seconds: int) -> None:
    while True:
        try:
            cycle.run_once()
        except Exception:
            log.exception("poll cycle failed; will retry next interval")
        time.sleep(interval_seconds)


def main() -> None:
    cfg = Config.from_env()
    immich_api_key = resolve_secret(cfg.secrets_file, "IMMICH_API_KEY", cfg.immich_api_key)
    if not immich_api_key:
        raise RuntimeError(
            f"no Immich API key found: set IMMICH_API_KEY or add an "
            f"IMMICH_API_KEY=... line to {cfg.secrets_file} (SECRETS_FILE)"
        )
    immich = ImmichClient(cfg.immich_url, immich_api_key)
    cfg = wait_for_immich_ready(immich, cfg)
    extra_keys = resolve_secret_list(cfg.secrets_file, "IMMICH_EXTRA_API_KEY")
    extra_clients = [ImmichClient(cfg.immich_url, key) for key in extra_keys]
    log.info("resolved core albums; %d extra Immich account(s) configured", len(extra_clients))
    recipe = RecipeRunner(cfg.claude_binary, cfg.recipe_skill_path, cfg.secrets_file)
    store = LibraryStore(cfg.library_path)
    revisions = RevisionStore(cfg.revisions_path)
    rules = RulesStore(cfg.rules_path)
    health = HealthStore()
    share_user_ids: list[str] = []
    if cfg.share_albums and extra_clients:
        share_user_ids = resolve_user_ids(immich, extra_clients)
        # The entry queues are shared as editors so the household can drop
        # photos into them. Every managed album is shared as viewers by the
        # poll cycle (see cycle.py). Live is only mirrored to displays, so it
        # is left private.
        ensure_all_shared(immich, [cfg.collage_album_id, cfg.wallpaper_album_id], share_user_ids)
        log.info("entry queues shared with %d extra account(s)", len(share_user_ids))

    worker = Worker(
        store, revisions, immich, recipe,
        wallpaper_album_id=cfg.wallpaper_album_id, collage_album_id=cfg.collage_album_id,
        extra_clients=extra_clients, rules=rules, count=cfg.worker_count,
        can_run_recipe=lambda: health.snapshot().claude_auth_ok,
    )
    intake = Intake(immich, store, cfg.wallpaper_album_id, cfg.collage_album_id)
    cycle = Cycle(immich, store, intake, worker, live_album_id=cfg.live_album_id, share_user_ids=share_user_ids)

    auth_thread = threading.Thread(
        target=auth_probe_forever,
        args=(recipe, health, cfg.secrets_file, cfg.claude_auth_check_interval_seconds),
        daemon=True,
    )
    auth_thread.start()

    worker.start()
    poll_thread = threading.Thread(
        target=poll_forever, args=(cycle, cfg.poll_interval_seconds), daemon=True,
    )
    poll_thread.start()
    log.info("poll loop started (interval=%ss)", cfg.poll_interval_seconds)

    app = create_app(cfg, immich, store, health, rules=rules)
    app.run(host=cfg.webui_host, port=cfg.webui_port)


if __name__ == "__main__":
    main()
