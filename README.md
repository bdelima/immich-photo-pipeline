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
- The small web UI at `http://<host>:<port>` (`8096` in the example compose file) picks which managed album is currently mirrored into **Live** — the one album [immich-overflight-feed/immich-frame-mirror](https://github.com/bdelima/immich-display-integrations) should point their own `ALBUM_ID` at, so your TV/display config never has to change when you switch between, say, "Everyday" and "Holiday".

## Releasing

Releases are cut by the code session only: bump `VERSION` in a reviewed PR
and merge it; the release workflow does the rest. Changes reach `main` only
through pull requests.

## Claude Code CLI

The pipeline drives Anthropic's Claude Code CLI as a headless subprocess. The CLI is proprietary, so it is **not bundled in the published image**: `docker-entrypoint.sh` installs `@anthropic-ai/claude-code` from npm into the `/data` volume on first start (needs outbound network once) and reuses it afterwards. Keep `/data` on a persistent volume, as in `docker-compose.example.yml`, or it is re-installed each time the container is recreated.

- `CLAUDE_CODE_VERSION`: pin an npm version (default `latest`; changing a pinned value re-installs).
- `CLAUDE_CLI_PREFIX`: install location (default `/data/claude-cli`).
- Authentication is unchanged: see the auth notes in `app/recipe_runner.py` (a long-lived token from `claude setup-token`).
