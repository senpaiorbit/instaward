# instaward-bot — personal Instagram curation & archival bot

> **Personal-use only.** This bot reposts content you curate to **your own**
> archive/curation account. It performs **no auto-follow, no auto-like, no DM,
> no spam**. Pacing is conservative (default ≥30 min between posts, ≤10/day).
> For business/creator analytics use the official Instagram Graph API instead.

## What it does

- `GET /upload?key=...` — fetch up to `FETCH_COUNT` candidates once, then
  retry in random order until one reel publishes (download → `clip_upload` →
  record in Turso). Per-candidate failures are logged, `/tmp` files cleaned,
  and the next candidate tried; only after all candidates are exhausted does
  it return 500. Optional `&attempts=N` caps retries
  (default `min(unprocessed, MAX_UPLOADS_PER_RUN*5 or 10)`).
- `GET /live?key=...` — last 5 processed rows.
- `GET /archive?key=...` — archive stale/low-view reposts.
- Background APScheduler job every 60 min runs the archive scan.

## Build / run

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Render Blueprint (`render.yaml`): type web, name `instaward-bot`, runtime
python, plan free, build `pip install -r requirements.txt`, start
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`.

Docker (optional, includes ffmpeg):

```bash
docker build -t instaward-bot .
docker run -p 8000:8000 --env-file .env instaward-bot
```

## Dependencies — rationale

| Package | Why |
|---|---|
| `fastapi==0.141.1` | Web API (`/health`, `/upload`, `/live`, `/archive`) |
| `uvicorn[standard]==0.52.1` | ASGI server |
| `aiograpi==2.0.2` | Async Instagram client (async port of instagrapi, native async; sync instagrapi rejected). Delays `[1,3]`, one `asyncio.Lock` per session, `set_settings` before login, `challenge_code_handler` → Telegram + raise |
| `libsql==0.1.2` | Turso client (sync, wrapped via `asyncio.to_thread`) |
| `python-telegram-bot==22.8` | Log/error/checkpoint notifications |
| `httpx==0.28.1` | Async streaming video/thumbnail downloads to `/tmp` |
| `APScheduler==3.11.3` | `AsyncIOScheduler` archive job every 60 min in FastAPI lifespan |
| `python-dotenv==1.1.1` | Local `.env` loading |
| `Pillow==12.3.0` | Convert thumbnails to JPEG for `clip_upload` |

Python `3.12.7` via `PYTHON_VERSION` (satisfies 3.11+). Render native runtime
provides ffmpeg (Dockerfile installs it for local/parity).

## Env vars

| Var | Required | Default | Notes |
|---|---|---|---|
| `ENV_KEY` | yes | — | Auth for all routes except `/health` (403 otherwise). Alias: `UPLOAD_SECRET` (fallback when `ENV_KEY` empty) |
| `TURSO_URL` | yes | — | e.g. `libsql://...turso.io`. Alias: `TURSO_DATABASE_URL` |
| `TURSO_AUTH_TOKEN` | yes | — | Turso token |
| `INSTAGRAM_USERNAME` | yes | — | Login |
| `INSTAGRAM_PASSWORD` | yes | — | Login |
| `INSTAGRAM_SESSION_STATE` | no | — | JSON session to `set_settings` before login; refreshed copy persisted in Turso `session_cache` |
| `TELEGRAM_BOT_TOKEN` | no | — | Needed for notifications |
| `TELEGRAM_CHAT_ID` | no | — | Needed for notifications |
| `ARCHIVE_TIME` | no | `12h` | e.g. `90m`, `12h`, `2d` → seconds |
| `ARCHIVE_VIEWS` | no | `1000` | Views threshold |
| `HIDELIKE` | no | `1` | `extra_data={"like_and_view_counts_disabled":1}` on upload. Alias: `HIDE_LIKE_VIEW_COUNTS=true/false/1/0` (fallback when `HIDELIKE` empty) |
| `BOTLOG` | no | `1` | `1` = Telegram notifications on |
| `THUMBNAIL_URL` | no | — | Global cover fallback. When empty, `COVER_FILE` then `COVER_DIR` are used |
| `COVER_MODE` | no | `static` | `random` or `static`; how `COVER_DIR` is picked |
| `COVER_DIR` | no | — | Thumbnail dir fallback when `THUMBNAIL_URL` empty |
| `COVER_FILE` | no | — | Thumbnail file fallback when `THUMBNAIL_URL` empty (takes precedence over `COVER_DIR`) |
| `FETCH_COUNT` | no | `30` | Reel candidates per run. Alias: `REEL_FETCH_COUNT` |
| `MIN_POST_INTERVAL_MIN` | no | `30` | Pacing floor |
| `MAX_PER_DAY` | no | `10` | Daily cap |
| `MAX_UPLOADS_PER_RUN` | no | `=MAX_PER_DAY` | Legacy per-run cap, clamped to `MAX_PER_DAY` |
| `LOG_LEVEL` | no | `INFO` | Sets `instaward-bot` logger level |
| `PORT` | no | `8000` | Runtime port |
| `PYTHON_VERSION` | render | `3.12.7` | Pinned runtime |

> **Session note:** raw session cookies (`INSTAGRAM_SESSION`, `SESSION_ID`,
> `CSRF_TOKEN`, `DS_USER_ID`) cannot substitute aiograpi settings JSON.
> `INSTAGRAM_SESSION`/`SESSION_ID` are ignored; `CSRF_TOKEN`, `DS_USER_ID`,
> and `DESTINATION_USERNAME` are unused. Log in once with
> `INSTAGRAM_USERNAME`/`INSTAGRAM_PASSWORD` so the app can generate proper
> settings and populate Turso `session_cache`.

## MCP setup (brief)

- **Render MCP**: point at this repo; deploy the `render.yaml` Blueprint; set
  the secret env vars above in the dashboard (all `sync:false`).
- **GitHub MCP**: push this workspace, open PRs, wire Render auto-deploys.
- **Turso MCP**: create DB, copy `TURSO_URL` + token, run `schema.sql`
  (app also runs it via `init_db` on startup).

## Thumbnail URL usage

Priority: per-post `?thumb=<url>` override → `THUMBNAIL_URL` env →
`media.thumbnail_url`. Must be a direct `*.{jpg,jpeg,png,webp}` URL. Downloaded
to `/tmp/thumbs/{code}.jpg`, converted via Pillow to JPEG, passed as `Path` to
`clip_upload(..., thumbnail=Path(cover_jpg))` (aiograpi requires local paths,
not bytes/URLs).

## Archive behavior

Real `media_archive(f"{pk}_{user_id}")` + DB flags (`archived=1`,
`archive_scanned=1`). Only DB-tracked rows
(`WHERE archived=0 AND archive_scanned=0`) are ever scanned — never a full
account sweep. If `media_archive` raises, fallback is `media_delete(pk)`; if
that also raises, a local-only DB mark is applied (row flagged archived without
an API call). Archive condition: age > threshold **OR** views < threshold.

## Test plan (no pytest; manual curl + DB checks)

1. `curl localhost:8000/health` → `{"status":"ok"}` (no key).
2. `curl ".../upload?key=WRONG"` → 403; with real key → publishes one reel.
3. `curl ".../live?key=..."` → 5 rows; confirm new `media_code` in Turso
   `processed_media`.
4. `curl ".../archive?key=...&time=1m&views=999999"` → forces archival path;
   verify `archived=1, archive_scanned=1`.
5. Immediate second `/upload` → 429 pacing; wrong key on `/live`, `/archive` → 403.
