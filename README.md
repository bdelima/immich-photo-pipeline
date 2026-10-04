# immich-photo-pipeline

Automated pipeline that processes Immich staging-album photos (Collage Maker, Wallpaper Maker) through a matting/cropping recipe, a Review approval step, and syncs approved managed albums to a Live album, with a small web UI to pick the live album.

## Setup

This walks through getting a release image running against a real Immich instance, start to finish. It assumes Immich itself is already running and reachable, with an existing user account you'll run the pipeline as.

### 1. Decide on accounts

Pick one Immich account to be the pipeline's own ("master") account — this is the account that creates the four albums below and owns everything the pipeline uploads. It doesn't need to be an Immich *admin* account, just a regular one; it does need to be the account whose API key the pipeline runs as.

If other household members will drop photos into the entry-queue albums from their own Immich accounts (likely, since that's the point), each of those accounts needs its own API key too — see `IMMICH_EXTRA_API_KEY` below for why.

### 2. Get an Immich API key for the pipeline's own account

In the Immich web app, signed in as the pipeline's account: **Account Settings → API Keys → New API Key**. Give it a name like `photo-pipeline` and copy the key — Immich only shows it once.

Repeat for each additional household account you want configured as an `IMMICH_EXTRA_API_KEY` fallback. That's needed because Immich won't let the pipeline's own account remove a photo from an entry-queue album unless it's the account that added it (confirmed intentional in Immich, not a bug — see `app/pipeline.py`'s `_clear_from_entry_queue`); the extra keys are tried in turn as a fallback for that one operation, nothing else.

### 3. Create the shared secrets file

On the host, next to wherever you'll keep `docker-compose.yml`:

```bash
mkdir -p secrets pipeline-data
nano secrets/immich-secrets.env
```

Contents (plain `KEY=VALUE` lines; `#` comments and blank lines are fine):

```
IMMICH_API_KEY=<the pipeline account's key from step 2>
IMMICH_EXTRA_API_KEY=<household member 2's key, if any>
IMMICH_EXTRA_API_KEY=<household member 3's key, if any -- repeat the line per extra account>
```

Leave the `CLAUDE_CODE_OAUTH_TOKEN=` line out for now — step 5 below adds it after the container is up. Make the file readable by uid 1000 (the container's non-root user):

```bash
chmod 644 secrets/immich-secrets.env
```

The container also runs as uid 1000 and writes its state file and the Claude Code CLI install into `/data`, so the `pipeline-data` directory you bind-mount there must be writable by that uid. Docker (and Portainer, when it creates a missing bind-mount path) creates it owned by root, which fails with `mkdir: cannot create directory '/data/claude-cli': Permission denied` — fix it once on the host:

```bash
sudo chown -R 1000:1000 pipeline-data
```

If you're also running `immich-overflight-feed`/`immich-frame-mirror` (see [immich-display-integrations](https://github.com/bdelima/immich-display-integrations)), point their `SECRETS_FILE` at this exact same file — they only ever read the `IMMICH_API_KEY=` line out of it and leave everything else alone, so one file covers all three containers.

### 4. Bring the container up

```bash
cp docker-compose.example.yml docker-compose.yml
```

Edit `docker-compose.yml`: set `IMMICH_URL` to your Immich instance's URL, reachable from this container (the internal Docker network address if Immich runs in the same compose project, otherwise its public URL). Leave the `IMMICH_API_KEY` environment variable commented out/unset — the secrets file from step 3 covers it.

```bash
docker compose up -d
docker compose logs -f immich-photo-pipeline
```

Running it as a Portainer stack instead? Paste the same compose file into a new stack and set the variables (`IMMICH_URL`, and optionally the album pins below) in Portainer's **Environment variables** panel rather than a `.env` file; everything else is identical. An environment variable left empty or unset is treated as "not set", so the compose file's `IMMICH_API_KEY: ${IMMICH_API_KEY}` line is harmless alongside the secrets file — the file's value always wins when it's present.

The first start creates the four albums (Collage Maker, Wallpaper Maker, Review, Live) in Immich under the pipeline account, if they don't already exist — watch the logs for confirmation, or check the Immich UI.

### 5. Authenticate Claude Code

The container can't process any photos yet — it needs a Claude Pro OAuth token (see `app/recipe_runner.py` for why this isn't a metered API key). Generate one:

```bash
docker exec -it immich-photo-pipeline claude setup-token
```

This opens the same browser-approval flow as signing into claude.ai normally (Google, passkey, email — whatever you normally use). If the container can't open a browser directly, it prints an approval URL and a short code instead: open that URL on your phone or any other device, approve it there, then paste the code back into the terminal. The command prints the resulting token straight to the terminal; it is **not** saved anywhere automatically.

Append it to the secrets file from step 3:

```bash
echo 'CLAUDE_CODE_OAUTH_TOKEN=<paste the token here>' >> secrets/immich-secrets.env
```

No restart needed — this line is checked periodically and the pipeline recovers on its own once it appears. Confirm it took:

```bash
curl http://localhost:8096/healthz
```

(adjust the port to match whatever you set in `docker-compose.yml`). `200` means a working Claude session; `503` means check `docker compose logs` for what's still missing.

### 6. Share the albums with everyone who'll use them

The four albums the pipeline created are owned by its own account. Anyone else (your wife, say) who should be able to drop photos into Collage Maker/Wallpaper Maker, or like/comment on Review and the managed albums, needs to be invited to each album from the Immich UI (open the album → Share → add their account) — the pipeline doesn't automate this invite step.

### 7. Use it

- Drop any photo into **Wallpaper Maker** and it's processed solo on the next poll cycle.
- Drop a **portrait** photo into **Collage Maker**; it waits there (and comments asking if you want it processed solo anyway) until a second portrait arrives, then groups 2–3 into one collage.
- Either way, the result lands in **Review**. From there:
  - **Like** it to get asked which managed album to promote it to — reply with a name, either an existing one from the list or a new one, which gets created.
  - **Comment** with feedback (e.g. "too pink", "crop in tighter") to have it reprocessed in place with that note.
  - **Comment** "delete this" (or similar — a quick Claude call reads the intent, not an exact phrase) to remove it outright. This works even for a non-admin reviewer account, since the pipeline's own account owns everything it uploads to Review.
- Once something's in a managed album, **unliking** it pulls it back to Review; **commenting** on it reprocesses it in place, same as Review.
- Already have finished photos from before the pipeline? See step 8 to import them so they're tracked like everything else.
- The small web UI at `http://<host>:<port>` (`8096` in the example compose file) picks which managed album is currently mirrored into **Live** — the one album [immich-overflight-feed/immich-frame-mirror](https://github.com/bdelima/immich-display-integrations) should point their own `ALBUM_ID` at, so your TV/display config never has to change when you switch between, say, "Everyday" and "Holiday".

### 8. Import photos you already processed (optional)

Photos that were finished before the pipeline existed (or outside it) can be brought under the same tracking as everything else — liking/unliking moves them between Review and managed albums, the web UI can mirror their album into Live, and a comment can delete them. There is no history to capture, so each imported photo simply becomes its own record. Run it inside the container; it's a **dry run** unless you add `--apply`:

```bash
# See what would happen (changes nothing):
docker exec immich-photo-pipeline python -m app.import_cli --from "Screensaver" --into "Everyday"

# Do it:
docker exec immich-photo-pipeline python -m app.import_cli --from "Screensaver" --into "Everyday" --apply
```

- `--from` is an album name (or id) holding the finished photos. `--into` is a managed album name (created if it doesn't exist) or the word `review` to put them in Review for the normal like-to-promote flow.
- Nothing is deleted, and nothing is removed from the source album — photos are only *added* to the target album, and for a managed album they are also liked (otherwise the pipeline would treat them as "pulled back" and move them to Review on the next cycle).
- It's safe to re-run: anything already tracked is skipped, and a photo that fails (reported at the end) is skipped and can be retried.
- Under Portainer, open the container's **Console** (connect as the default user, `/bin/sh`) and run the `python -m app.import_cli ...` part of either command there instead of using `docker exec`.
- A dry run just reads. An `--apply` run takes the same lock the poll loop holds while it works, so it waits for any in-flight cycle to finish (minutes, if a recipe is running) and the poll loop briefly waits for it in turn — this keeps the two from overwriting each other's saved state.
- If a photo can't be liked — usually because a different Immich account owns it, and Immich only lets the owner edit an asset — it is reported and skipped rather than imported half-way. Either like those photos yourself from the owning account in the Immich UI and re-run (already-liked photos need no edit), or import them into `review` instead.
- **Imported photos can't be revised.** With no original to work from, a "make it brighter" comment would run the recipe on an already-matted image. The pipeline replies once explaining that, and leaves the photo alone. Like/unlike and "delete this" still work.
- If you plan to mirror an album into Live afterwards, import everything you want kept *first*: picking a managed album in the web UI makes Live match it exactly.

## Configuration reference

All settings are environment variables. Credentials can also come from the shared secrets file (see step 3), which wins over the matching variable when both are set.

| Variable | Default | What it does |
| --- | --- | --- |
| `IMMICH_URL` | *(required)* | Base URL of your Immich instance, reachable from the container. |
| `IMMICH_API_KEY` | | The pipeline account's API key. Prefer the `IMMICH_API_KEY=` line in the secrets file; this is only the fallback. |
| `IMMICH_EXTRA_API_KEY` | | Secrets-file only, repeatable: other household accounts' keys, tried in turn when removing an original they added from an entry queue. |
| `CLAUDE_CODE_OAUTH_TOKEN` | | Claude Pro token from `claude setup-token`. Prefer the secrets-file line; it's re-read periodically, so rotating it needs no restart. Never set `ANTHROPIC_API_KEY` — it would switch billing to metered API usage. |
| `SECRETS_FILE` | `/run/secrets/immich_secrets.env` | Path of the shared secrets file inside the container. |
| `COLLAGE_ALBUM_NAME` / `WALLPAPER_ALBUM_NAME` / `REVIEW_ALBUM_NAME` / `LIVE_ALBUM_NAME` | `Collage Maker` / `Wallpaper Maker` / `Review` / `Live` | Albums are looked up by this name at startup and created if missing. |
| `COLLAGE_ALBUM_ID` / `WALLPAPER_ALBUM_ID` / `REVIEW_ALBUM_ID` / `LIVE_ALBUM_ID` | | Pin an existing album by id instead. An id always wins over the name lookup. |
| `POLL_INTERVAL_SECONDS` | `15` | How often the poll loop runs. |
| `CLAUDE_AUTH_CHECK_INTERVAL_SECONDS` | `300` | How often the Claude session is re-probed (each probe is a real, trivial invocation, so this is slower than polling). |
| `STATE_PATH` | `/data/state.json` | Where tracking state is kept; keep `/data` on a persistent volume. |
| `RECIPE_SKILL_PATH` | `/app/.claude/skills/photo-mat-recipe` | The recipe skill. Claude Code only discovers skills from a `.claude/skills/<name>/` folder, so any override must keep that shape. |
| `CLAUDE_BINARY` | `claude` | The Claude Code executable to run. |
| `WEBUI_HOST` / `WEBUI_PORT` | `0.0.0.0` / `8080` | Where the web UI and `/healthz` listen inside the container. |
| `CLAUDE_CODE_VERSION` / `CLAUDE_CLI_PREFIX` | `latest` / `/data/claude-cli` | See "Claude Code CLI" below. |

### Using an album you already have

Setting a `*_ALBUM_ID` pins that album instead of creating one by name. That's how to reuse, say, an existing "Screensaver" album that your display containers already point at: set `LIVE_ALBUM_ID` to its id (find it in the album's URL in the Immich UI) and leave `LIVE_ALBUM_NAME` alone — the name is only used when no id is pinned, so there's no need for the two to match.

One thing to know before you do: choosing a managed album in the web UI makes **Live match it exactly** — photos in Live that aren't in the chosen album are removed from Live (removed from that album only; the photos themselves are not deleted). So if Live is an album that already holds finished photos, import them first (step 8) and choose the album they were imported into, or they'll drop out of Live.

## Troubleshooting

- **`/healthz` returns 503, or the logs say there's no working Claude session** — the token line is missing or stale; see step 5. Processing is skipped until it recovers (no restart needed).
- **`mkdir: cannot create directory '/data/claude-cli': Permission denied`** — the `/data` bind mount isn't writable by uid 1000; see the `chown` in step 3.
- **The first start looks stuck "installing Claude Code CLI"** — it installs from npm into `/data` once and needs outbound network for that; later starts reuse it. An `npm WARN EBADENGINE` about the Node version is harmless.
- **A photo sits in an entry queue after being processed** — Immich only lets the account that added a photo remove it from an album. If none of the configured keys is that account, the pipeline leaves a comment saying the original is safe to delete by hand; it won't be reprocessed either way.
- **A comment on an imported photo gets a reply saying it can't be revised** — expected; see step 8.

## Releasing

Releases are cut by the code session only: bump `VERSION` in a reviewed PR
and merge it; the release workflow does the rest. Changes reach `main` only
through pull requests.

## Claude Code CLI

The pipeline drives Anthropic's Claude Code CLI as a headless subprocess. The CLI is proprietary, so it is **not bundled in the published image**: `docker-entrypoint.sh` installs `@anthropic-ai/claude-code` from npm into the `/data` volume on first start (needs outbound network once) and reuses it afterwards. Keep `/data` on a persistent volume, as in `docker-compose.example.yml`, or it is re-installed each time the container is recreated.

- `CLAUDE_CODE_VERSION`: pin an npm version (default `latest`; changing a pinned value re-installs).
- `CLAUDE_CLI_PREFIX`: install location (default `/data/claude-cli`).
- Authentication is unchanged: see the auth notes in `app/recipe_runner.py` (a long-lived token from `claude setup-token`).
