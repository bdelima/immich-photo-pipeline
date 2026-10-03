# immich-photo-pipeline

Automated pipeline that processes Immich staging-album photos (Collage Maker, Wallpaper Maker) through a matting/cropping recipe, a Review approval step, and syncs approved managed albums to a Live album, with a small web UI to pick the live album.

## Releasing

Releases are cut by the code session only: bump `VERSION` in a reviewed PR
and merge it; the release workflow does the rest. Changes reach `main` only
through pull requests.

## Claude Code CLI

The pipeline drives Anthropic's Claude Code CLI as a headless subprocess. The CLI is proprietary, so it is **not bundled in the published image**: `docker-entrypoint.sh` installs `@anthropic-ai/claude-code` from npm into the `/data` volume on first start (needs outbound network once) and reuses it afterwards. Keep `/data` on a persistent volume, as in `docker-compose.example.yml`, or it is re-installed each time the container is recreated.

- `CLAUDE_CODE_VERSION`: pin an npm version (default `latest`; changing a pinned value re-installs).
- `CLAUDE_CLI_PREFIX`: install location (default `/data/claude-cli`).
- Authentication is unchanged: see the auth notes in `app/recipe_runner.py` (a long-lived token from `claude setup-token`).
