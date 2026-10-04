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
    # This account's own key -- the "master"/admin key the container runs
    # as for everything except entry-queue removals someone else's account
    # added (see secrets_file below). An IMMICH_API_KEY= line in the
    # shared secrets file wins over this raw env value when both are set
    # -- same resolve_secret() rule as the Claude token, and the same
    # motivation: it's what lets this key be read from the exact same
    # shared file as the sibling overflight-feed/frame-mirror containers
    # (bdelima/immich-display-integrations) instead of a separately-
    # copied value per project. Resolved once at startup in main.py, not
    # re-read per request like the Claude token, since unlike that token
    # this key isn't expected to rotate while the container is running.
    immich_api_key: str

    # Each *_album_id is optional: when unset, main.py's startup bootstrap
    # (see albums.py) looks up an album with the matching *_album_name,
    # creating it if none exists yet. Setting the id explicitly always
    # wins over the name lookup, for anyone who already has albums set up.
    collage_album_id: str
    collage_album_name: str
    wallpaper_album_id: str
    wallpaper_album_name: str
    review_album_id: str
    review_album_name: str
    live_album_id: str
    live_album_name: str

    poll_interval_seconds: int
    state_path: str

    recipe_skill_path: str
    claude_binary: str

    # The one shared secrets file for the whole Immich pipeline/display-
    # integrations ecosystem -- simple KEY=VALUE lines (see
    # app/secrets.py), one bind-mounted file instead of one file per
    # credential. This component pulls out exactly three keys and leaves
    # anything else in the file alone:
    #   CLAUDE_CODE_OAUTH_TOKEN -- the Claude Pro OAuth token. Checked
    #     periodically (see claude_auth_check_interval_seconds below) and
    #     recovers on its own when the line appears/changes, no restart
    #     needed.
    #   IMMICH_API_KEY -- this account's own/admin key, shared with the
    #     sibling overflight-feed/frame-mirror containers. Only an env
    #     var fallback if this key is unset in the file (see
    #     immich_api_key above); read once at startup.
    #   IMMICH_EXTRA_API_KEY -- repeatable, one line per extra household
    #     account, used only as a fallback when removing an entry-queue
    #     original someone else's account added (Immich restricts
    #     removal to whoever added the asset). Read once at startup.
    secrets_file: str
    # How often the background auth probe re-checks the Claude token,
    # independent of the main poll interval -- a probe spends a real (if
    # trivial) Claude invocation, so this defaults much slower than
    # poll_interval.
    claude_auth_check_interval_seconds: int

    webui_host: str
    webui_port: int

    # Reviewer-taught recipe rules (see app/rules.py). Defaulted so code
    # that builds a Config by hand doesn't have to know about it.
    rules_path: str = "/data/rules.json"

    @staticmethod
    def from_env() -> "Config":
        return Config(
            immich_url=_env("IMMICH_URL", required=True).rstrip("/"),
            # Not required=True here: resolve_secret() in main.py accepts
            # this as the fallback when the secrets file has no
            # IMMICH_API_KEY= line either, and raises its own clearer
            # error if both are empty -- "set IMMICH_API_KEY or add an
            # IMMICH_API_KEY=... line to the secrets file" beats a
            # generic "required environment variable" message.
            immich_api_key=_env("IMMICH_API_KEY"),
            collage_album_id=_env("COLLAGE_ALBUM_ID"),
            collage_album_name=_env("COLLAGE_ALBUM_NAME", "Collage Maker"),
            wallpaper_album_id=_env("WALLPAPER_ALBUM_ID"),
            wallpaper_album_name=_env("WALLPAPER_ALBUM_NAME", "Wallpaper Maker"),
            review_album_id=_env("REVIEW_ALBUM_ID"),
            review_album_name=_env("REVIEW_ALBUM_NAME", "Review"),
            live_album_id=_env("LIVE_ALBUM_ID"),
            live_album_name=_env("LIVE_ALBUM_NAME", "Live"),
            poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 15),
            state_path=_env("STATE_PATH", "/data/state.json"),
            recipe_skill_path=_env("RECIPE_SKILL_PATH", "/app/.claude/skills/photo-mat-recipe"),
            claude_binary=_env("CLAUDE_BINARY", "claude"),
            secrets_file=_env("SECRETS_FILE", "/run/secrets/immich_secrets.env"),
            claude_auth_check_interval_seconds=_env_int("CLAUDE_AUTH_CHECK_INTERVAL_SECONDS", 300),
            webui_host=_env("WEBUI_HOST", "0.0.0.0"),
            webui_port=_env_int("WEBUI_PORT", 8080),
            rules_path=_env("RULES_PATH", "/data/rules.json"),
        )
