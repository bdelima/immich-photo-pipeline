# immich-photo-pipeline

Automated pipeline that processes Immich staging-album photos (Collage Maker, Wallpaper Maker) through a matting/cropping recipe into a Review step, keeps every photo and every revision of it in its own library, and mirrors the managed albums and a Live album into Immich. Review, moving, promoting and trashing are done in the pipeline's own web UI, which is being built up in stages (see step 7).

## Setup

This walks through getting a release image running against a real Immich instance, start to finish. It assumes Immich itself is already running and reachable, with an existing user account you'll run the pipeline as.

### 1. Decide on accounts

Pick one Immich account to be the pipeline's own ("master") account — this is the account that creates the four albums below and owns everything the pipeline uploads. It doesn't need to be an Immich *admin* account, just a regular one; it does need to be the account whose API key the pipeline runs as.

If other household members will drop photos into the entry-queue albums from their own Immich accounts (likely, since that's the point), each of those accounts needs its own API key too — see `IMMICH_EXTRA_API_KEY` below for why.

### 2. Get an Immich API key for the pipeline's own account

In the Immich web app, signed in as the pipeline's account: **Account Settings → API Keys → New API Key**. Give it a name like `photo-pipeline` and copy the key — Immich only shows it once.

Repeat for each additional household account you want configured as an `IMMICH_EXTRA_API_KEY` fallback. That's needed because Immich won't let the pipeline's own account remove a photo from an entry-queue album unless it's the account that added it (confirmed intentional in Immich, not a bug — see `app/worker.py`'s `_clear_from_queue`); the extra keys are tried in turn as a fallback for that one operation. They also tell the pipeline which accounts to share its albums with (step 6), so every household member who should take part needs one.

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

### 6. Albums are shared automatically

The other accounts can't drop photos into an entry queue they can't see. So on startup the pipeline looks up which account each `IMMICH_EXTRA_API_KEY` belongs to and shares **Collage Maker and Wallpaper Maker** with them as editors (they can add photos). Every managed album the pipeline creates is shared with them as **viewers**: they can browse it and like photos, but cannot add or remove anything, so nothing changes in a managed album except through the pipeline. Live is left private since it only feeds your displays, and Review is not shown in Immich at all: it lives in the pipeline's own library.

This is why each household member who should take part needs an `IMMICH_EXTRA_API_KEY` line (steps 2-3), even if they never remove an original from a queue. Sharing is best-effort and safe to repeat: it only adds accounts that are missing (an album already shared keeps the role it has), and a failure is logged and skipped. Set `SHARE_ALBUMS=false` to turn all of this off and manage sharing yourself.

### 7. Use it

- Drop any photo or video into **Wallpaper Maker**. A photo is processed solo on the next poll cycle; a video is kept as it is (videos are managed, not processed).
- Drop a **portrait** photo into **Collage Maker**; it waits there until a second portrait arrives, then groups 2–3 into one collage. Videos can't go in a collage.
- Each result lands in **Review**, which is held in the pipeline's library rather than in an Immich album. The original and every later version are kept in the revision store (`REVISIONS_PATH`); Immich only ever holds the current version.
- The originals are copied into the store and then removed from the entry queue (see "A photo sits in an entry queue" below if one stays).
- If the recipe has a question it can't answer on its own, the photo is marked as waiting for an answer. Answering is part of the per-photo panel, which is not built yet.
- A photo that fails is marked failed with the reason and is **not** retried by itself, since each attempt spends Claude plan credits.
- **The pipeline no longer reads Immich comments or likes.** Replying to a photo in Immich does nothing now.
- The web UI at `http://<host>:<port>` (`8096` in the example compose file) currently shows what the library holds (processing, failed, Review, Live, each album). Browsing with thumbnails, moving, promoting to Live, trashing, and revising a photo by chatting with Claude are coming; until they land, Review can't be acted on, so don't switch to this version yet if you rely on it.
- Want a fix to stick for every future photo? Add a rule on the **Recipe rules** page — see step 9.

### 8. Import photos you already processed (optional)

Photos that were finished before the pipeline existed (or outside it) can be brought into the library. There is no history to capture, so each imported photo simply becomes a photo with a single revision. Run it inside the container; it's a **dry run** unless you add `--apply`:

```bash
# See what would happen (changes nothing):
docker exec immich-photo-pipeline python -m app.import_cli --from "Screensaver" --into "Everyday"

# Do it:
docker exec immich-photo-pipeline python -m app.import_cli --from "Screensaver" --into "Everyday" --apply
```

- `--from` is an album name (or id) holding the finished photos. `--into` is a managed album name (created if it doesn't exist) or the word `review` to put them in Review.
- Each photo's current image is copied into the revision store and published again as a new asset owned by the pipeline account (the source album usually belongs to someone else, so the pipeline could not manage the original). Nothing is deleted and nothing is removed from the source album.
- It's safe to re-run: anything already in the library is skipped, and a photo that fails (reported at the end) is skipped and can be retried.
- Under Portainer, open the container's **Console** (connect as the default user, `/bin/sh`) and run the `python -m app.import_cli ...` part of either command there instead of using `docker exec`.
- **Imported photos can't be revised.** With no original to work from, a "make it brighter" request would run the recipe on an already-matted image.
- **Coming from the old comment-and-like version?** The few photos it processed can be carried over, with the instructions it recorded, by `docker exec immich-photo-pipeline python -m app.import_legacy` (dry run; add `--apply`). It reads the old `state.json` (`STATE_PATH`), and the photo in the album that was live is marked as live.

### 9. Teach the recipe (optional)

The recipe doesn't have to stay as it was written. Reviewers can teach it standing rules, which are added to the prompt on every photo the pipeline processes from then on. They are kept in a file on the `/data` volume (`rules.json`), not written into `photo-mat-recipe/SKILL.md` — the skill is baked into the image, so edits there would vanish on the next update, and nothing would review them.

**Teaching one while revising a photo (not available yet).** Teaching a rule by commenting on a photo in Immich has been removed with the comment flow; it comes back with the per-photo chat in the web UI. Until then, add rules on the Recipe rules page. The way it worked, and the way it will work again:

> For collages, always keep the items balanced by size.

The photo is revised as usual, and the reply says exactly what was saved, so a wrong paraphrase is obvious:

> Saved rule r7 (collages): "Keep collage items balanced by size." Reply "forget r7" to undo.

A comment without such a cue ("too pink") is only ever a revision of that one photo. If the classifier is unsure, it chooses revision.

**Rules can apply to everything, to single photos only, or to collages only.** The pipeline picks the scope from what you said ("for collages…"); you can also add or fix one on the rules page. A collage-only rule is never added to a single-photo run, and vice versa.

**The recipe can suggest rules too.** After revising a photo from your feedback, the recipe may notice that the feedback was really a general rule. It then asks, on the revised photo:

> Should I remember this for future single photos? "Use a thinner bevel on dark photos." Reply "yes" to save it as rule r8, or "no" to drop it.

Nothing is used until you reply `yes`; a `no`, or no reply, leaves it unused. Only a bare yes/no counts, so "yes but make it darker" is treated as a normal revision. There is at most one open suggestion per photo.

**Managing rules.** Reply `forget r7` on any photo, or open the web UI's **Recipe rules** page (`/rules`, linked from the main page) to add a rule, retire one, or save/dismiss a suggestion. At most 20 rules can be active at once, each up to 300 characters on one line (angle brackets are dropped); when the limit is reached, retire one first.

Things worth knowing:

- Rules apply to photos processed or revised *after* they're saved. To re-apply one to a photo that's already finished, comment on it.
- Imported photos (step 8) can't be revised, but a rule taught in a comment on one is still saved and used for future photos.
- Rules are plain data in the prompt: they're framed as preferences about the image only and can't change the reply format, file access, or the other instructions.
- To make a rule permanent, fold it into `photo-mat-recipe/SKILL.md` through a normal reviewed change and retire it here.

## Configuration reference

All settings are environment variables. Credentials can also come from the shared secrets file (see step 3), which wins over the matching variable when both are set.

| Variable | Default | What it does |
| --- | --- | --- |
| `IMMICH_URL` | *(required)* | Base URL of your Immich instance, reachable from the container. |
| `IMMICH_API_KEY` | | The pipeline account's API key. Prefer the `IMMICH_API_KEY=` line in the secrets file; this is only the fallback. |
| `IMMICH_EXTRA_API_KEY` | | Secrets-file only, repeatable: other household accounts' keys. Used to remove originals they added from an entry queue, and to work out which accounts the pipeline's albums are shared with (step 6). |
| `SHARE_ALBUMS` | `true` | Share the pipeline's albums with the accounts behind the extra keys. Set `false` to manage sharing by hand. |
| `CLAUDE_CODE_OAUTH_TOKEN` | | Claude Pro token from `claude setup-token`. Prefer the secrets-file line; it's re-read periodically, so rotating it needs no restart. Never set `ANTHROPIC_API_KEY` — it would switch billing to metered API usage. |
| `SECRETS_FILE` | `/run/secrets/immich_secrets.env` | Path of the shared secrets file inside the container. |
| `COLLAGE_ALBUM_NAME` / `WALLPAPER_ALBUM_NAME` / `REVIEW_ALBUM_NAME` / `LIVE_ALBUM_NAME` | `Collage Maker` / `Wallpaper Maker` / `Review` / `Live` | Albums are looked up by this name at startup and created if missing. |
| `COLLAGE_ALBUM_ID` / `WALLPAPER_ALBUM_ID` / `REVIEW_ALBUM_ID` / `LIVE_ALBUM_ID` | | Pin an existing album by id instead. An id always wins over the name lookup. |
| `POLL_INTERVAL_SECONDS` | `15` | How often the poll loop runs. |
| `CLAUDE_AUTH_CHECK_INTERVAL_SECONDS` | `300` | How often the Claude session is re-probed (each probe is a real, trivial invocation, so this is slower than polling). |
| `LIBRARY_PATH` | `/data/library.json` | The library: every photo, where it lives, and its revisions. Keep `/data` on a persistent volume. |
| `REVISIONS_PATH` | `/data/revisions` | Where every original and every revision of every photo is kept. It can grow, so it is its own setting: bind-mount a bigger disk onto it if needed. |
| `WORKER_COUNT` | `1` | How many photos are processed at the same time (each is a Claude run). |
| `STATE_PATH` | `/data/state.json` | The old version's state file; only read by `python -m app.import_legacy`. |
| `RULES_PATH` | `/data/rules.json` | Where reviewer-taught recipe rules are kept (step 9); keep it on the persistent `/data` volume. |
| `RECIPE_SKILL_PATH` | `/app/.claude/skills/photo-mat-recipe` | The recipe skill. Claude Code only discovers skills from a `.claude/skills/<name>/` folder, so any override must keep that shape. |
| `CLAUDE_BINARY` | `claude` | The Claude Code executable to run. |
| `WEBUI_HOST` / `WEBUI_PORT` | `0.0.0.0` / `8080` | Where the web UI and `/healthz` listen inside the container. |
| `CLAUDE_CODE_VERSION` / `CLAUDE_CLI_PREFIX` | `latest` / `/data/claude-cli` | See "Claude Code CLI" below. |

### Using an album you already have

Setting a `*_ALBUM_ID` pins that album instead of creating one by name. That's how to reuse, say, an existing "Screensaver" album that your display containers already point at: set `LIVE_ALBUM_ID` to its id (find it in the album's URL in the Immich UI) and leave `LIVE_ALBUM_NAME` alone — the name is only used when no id is pinned, so there's no need for the two to match.

One thing to know before you do: the pipeline makes **Live match the photos promoted to Live in the library exactly**, so photos in Live that the library doesn't know about are removed from it (from that album only; the photos themselves are not deleted). An empty library never empties Live, but once anything is promoted, everything else goes. So if Live already holds finished photos, import them first (step 8) and promote them.

## Troubleshooting

- **`/healthz` returns 503, or the logs say there's no working Claude session** — the token line is missing or stale; see step 5. Processing is skipped until it recovers (no restart needed).
- **`mkdir: cannot create directory '/data/claude-cli': Permission denied`** — the `/data` bind mount isn't writable by uid 1000; see the `chown` in step 3.
- **The first start looks stuck "installing Claude Code CLI"** — it installs from npm into `/data` once and needs outbound network for that; later starts reuse it. An `npm WARN EBADENGINE` about the Node version is harmless.
- **A photo sits in an entry queue after being processed** — Immich only lets the account that added a photo remove it from an album. If none of the configured keys is that account, the pipeline logs a warning and leaves the original there; it is safe to delete by hand, and it won't be reprocessed either way.
- **A photo shows as failed** — the reason is recorded on it. It isn't retried by itself; videos in Collage Maker always fail this way (use Wallpaper Maker).
- **A rule I taught isn't being applied** — check the **Recipe rules** page: it must be *active* (not a suggestion waiting for a "yes"), and its scope must match the run (a collage-only rule never reaches a single photo). Rules take effect on the next processing or revision of a photo, not retroactively.

## Releasing

Releases are cut by the code session only: bump `VERSION` in a reviewed PR
and merge it; the release workflow does the rest. Changes reach `main` only
through pull requests.

## Claude Code CLI

The pipeline drives Anthropic's Claude Code CLI as a headless subprocess. The CLI is proprietary, so it is **not bundled in the published image**: `docker-entrypoint.sh` installs `@anthropic-ai/claude-code` from npm into the `/data` volume on first start (needs outbound network once) and reuses it afterwards. Keep `/data` on a persistent volume, as in `docker-compose.example.yml`, or it is re-installed each time the container is recreated.

- `CLAUDE_CODE_VERSION`: pin an npm version (default `latest`; changing a pinned value re-installs).
- `CLAUDE_CLI_PREFIX`: install location (default `/data/claude-cli`).
- Authentication is unchanged: see the auth notes in `app/recipe_runner.py` (a long-lived token from `claude setup-token`).
