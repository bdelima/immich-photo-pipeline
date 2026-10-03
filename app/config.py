"""Environment-driven configuration for the pipeline.

Every setting is a plain env var so this matches the conventions already
used by overflight-feed/frame-mirror (immich-display-integrations).
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field


def _env(name: str, default: str | None = None, required: bool = False) -> str:
    value = os.environ.get(name, default)
    if required and not value:
        raise RuntimeError(f"required environment variable {name} is not set")
    return value or ""


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    return int(raw) if raw else default


@dataclass(frozen=True)
class Config:
    immich_url: str
    immich_api_key: str

    collage_album_id: str
    wallpaper_album_id: str
    review_album_id: str
    live_album_id: str

    poll_interval_seconds: int
    state_path: str

    recipe_skill_path: str
    claude_binary: str

    webui_host: str
    webui_port: int

    @staticmethod
    def from_env() -> "Config":
        return Config(
            immich_url=_env("IMMICH_URL", required=True).rstrip("/"),
            immich_api_key=_env("IMMICH_API_KEY", required=True),
            collage_album_id=_env("COLLAGE_ALBUM_ID", required=True),
            wallpaper_album_id=_env("WALLPAPER_ALBUM_ID", required=True),
            review_album_id=_env("REVIEW_ALBUM_ID", required=True),
            live_album_id=_env("LIVE_ALBUM_ID", required=True),
            poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 15),
            state_path=_env("STATE_PATH", "/data/state.json"),
            recipe_skill_path=_env("RECIPE_SKILL_PATH", "/app/photo-mat-recipe"),
            claude_binary=_env("CLAUDE_BINARY", "claude"),
            webui_host=_env("WEBUI_HOST", "0.0.0.0"),
            webui_port=_env_int("WEBUI_PORT", 8080),
        )
