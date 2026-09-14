# instaward-bot — personal Instagram curation & retirement bot

> **Personal-use only.** This bot reposts content you curate to **your own**
> archive/curation account. It performs **no auto-follow, no auto-like, no DM**.
> It does post a CTA comment (default off) **only on its own uploads**, and
> deletes **only its own reposts** (ownership verified per post, `owner==self`).
> Pacing is conservative (default ≥30 min between posts, ≤10/day).
> For business/creator analytics use the official Instagram Graph API instead.

## What it does

- **Upload** (`/upload`) — fetch candidates once, publish reels with credit +
  cover, store the repost identity, optional CTA comment. Single instant publish
  or background multi-upload jobs.
- **Quality gate** — every candidate video is resolution/size-checked before
  upload; low-res sources are skipped, high-quality only.
- **Comment engine** — posts a CTA comment (default `FOLLOW ME` variants) on
  each new repost and pins it. Off by default; opt in per call or via env.
- **Retire** (`/archive`) — delete stale/low-view **own** reposts
  (Instagram's API cannot archive reels, so retirement = `media_delete`;
  photos would use `media_archive`). Runs on demand or hourly via scheduler.
- **Jobs** — long upload/archive runs execute in the background and survive
  HTTP disconnects; poll progress via `/a_job`.

## Endpoints (all except `/health` need `?key=`)

Base: `https://instaward-bot.onrender.com`

| Endpoint | Description |
|---|---|
| `GET /health` | `{"status":"ok"}`. No key. Use for uptime pingers. |
| `GET /upload` | Publish reel(s). See params below. |
| `GET /live` | Last 5 `processed_media` rows. |
| `GET /archive` | Retire reposts. See params below. |
| `GET /a_job?id=` | Poll a background job (progress + result). 404 if unknown. |
| `GET /archive_one?code=` | Retire ONE shortcode immediately (bypasses age/views gating). |

### `GET /upload` params

| Param | Default | Notes |
|---|---|---|
| `hidelike` | `HIDELIKE` | `1` hides like/view counts on the reel |
| `botlog` | — | Reserved |
| `thumb` | — | Per-post cover override (direct `*.{jpg,jpeg,png,webp}` URL) |
| `attempts` | auto | Max candidates to try (`min(unprocessed, MAX_UPLOADS_PER_RUN*5)`) |
| `amount` | — | **Omit** = publish 1 reel synchronously (returns summary). **Set 1–50** = background job publishing N reels, returns `{"job_id","status":"running","target_count"}` instantly; poll `/a_job`. |
| `comment` | `COMMENT_ENABLED` | `1` = post+pin CTA comment on each upload, `0` = skip. Default off. |

Upload flow per candidate: download video → download/convert cover → **quality
gate** (reject → next candidate) → caption (`full original + blank line +
Credit=@author`, ≤2000 chars) → `clip_upload` → record source + repost
identity (`repost_code`/`repost_pk`) → **CTA comment + pin** (if enabled) →
publish. Per-candidate failures are logged, `/tmp` cleaned, next candidate
tried. Pacing: 30-min gaps are slept **inside** the job (same candidate
retried, not counted); daily cap fails the run with 429/`error`.

### `GET /archive` params

| Param | Default | Notes |
|---|---|---|
| `time` | `24h` | Max age: `{n}s/m/h/d` or bare seconds, e.g. `90m`, `2d`, `3600` |
| `views` | `1000` | Views threshold (int) |
| `all` | `0` | `1` = retire **every** tracked live repost, ignore age/views/scan flags |
| `wait` | `0` | `1` = run synchronously and return the summary (may time out on long runs). Default = background job, returns `job_id` instantly |

Response (both modes) echoes the effective values:
`{"ok":true,"time_sec":86400,"views":1000,"all":false,...}` plus either
`"summary"` (wait=1) or `"job_id","status":"running"` (job mode).

Retirement rule (`all=0`): `age > time` **AND** `views <= threshold` →
retire. Views = the **repost's own** `play_count` (clips), never the source's.
`all=1`: every row with `archived=0` is processed; live reposts retire,
already-gone ones are marked done, unmapped rows are skipped (source posts are
never touched — there is no code path that archives/deletes others' media).

Retire method: clips → `media_delete` (Instagram answers `cannot archive
Clips media` to every archive variant — verified across 5 request shapes);
photos → `media_archive` variants. Deletion runs **only** when
`media.owner == logged-in user`, else local-only DB mark. Nothing is ever
retried blindly; failures mark the row failed, never deleted.

### `GET /a_job?id=` polling

```json
{"ok":true,"job":{"id":"…","kind":"archive|upload","status":"running|done|error",
"checked":4,"archived":1,"kept":3,"attempted":2,"published":1,"failed":0,
"items":[{"media_code":"…","repost_code":"…","author":"…"}],
"errors":[],"result":{…},"error":null}}
```

Archive jobs update `checked/archived/kept`; upload jobs update
`attempted/published/failed/items`. A second `/archive` (`/upload`) call while
one runs returns the existing `job_id` with `"note":"already running"`.
`?wait=1` bypasses the queue. Note: jobs live in process memory — a
deploy/restart clears them (use Turso rows as the durable record).

### `GET /archive_one?code=`

Immediate single-post retirement, bypassing gating. Resolves the code → checks
ownership → retires (delete own clip / archive photo) → marks DB. Returns
`{"code","pk","archived":true,"method":"delete|archive:*|local"}`.

## Build / run

```bash
pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port $PORT
```

Render Blueprint (`render.yaml`): type web, name `instaward-bot`, runtime
python, plan free, build `pip install -r requirements.txt`, start
`uvicorn app.main:app --host 0.0.0.0 --port $PORT`. Auto-deploys from `main`.

Docker (optional, includes ffmpeg):

```bash
docker build -t instaward-bot .
docker run -p 8000:8000 --env-file .env instaward-bot
```

## Dependencies — rationale

| Package | Why |
|---|---|
| `fastapi==0.141.1` | Web API (`/health`, `/upload`, `/live`, `/archive`, `/a_job`, `/archive_one`) |
| `uvicorn[standard]==0.52.1` | ASGI server |
| `aiograpi==2.0.2` | Async Instagram client (async port of instagrapi). Delays `[1,3]`, one `asyncio.Lock` per session, `set_settings` before login, `challenge_code_handler` → Telegram + raise |
| `libsql==0.1.2` | Turso client (sync, wrapped via `asyncio.to_thread`) |
| `python-telegram-bot==22.8` | Log/error/checkpoint/publish notifications |
| `httpx==0.28.1` | Async streaming video/thumbnail downloads to `/tmp` |
| `APScheduler==3.11.3` | `AsyncIOScheduler` archive job every 60 min in FastAPI lifespan |
| `python-dotenv==1.1.1` | Local `.env` loading |
| `Pillow==12.3.0` | Thumbnail JPEG conversion + thumbnail dimension probe |

Python `3.12.7` via `PYTHON_VERSION`. ffprobe is used for resolution probing
when present (Docker image); otherwise a stdlib MP4 box parser (`moov>trak>
tkhd`) covers it — no extra deps.

## Env vars

| Var | Required | Default | Notes |
|---|---|---|---|
| `ENV_KEY` | yes | — | Auth for all routes except `/health` (403 otherwise). Alias: `UPLOAD_SECRET` |
| `TURSO_URL` | yes | — | e.g. `libsql://...turso.io`. Alias: `TURSO_DATABASE_URL` |
| `TURSO_AUTH_TOKEN` | yes | — | Turso token |
| `INSTAGRAM_USERNAME` | yes | — | Login |
| `INSTAGRAM_PASSWORD` | yes | — | Login (fallback when sessionid fails) |
| `INSTAGRAM_SESSION_STATE` | no | — | JSON session for `set_settings` before login; refreshed copy persisted in Turso `session_cache` |
| `INSTAGRAM_SESSIONID` | no* | — | Raw `sessionid` cookie; bypasses 2FA. Aliases: `SESSION_ID`, `INSTAGRAM_SESSION` |
| `INSTAGRAM_CSRFTOKEN` | no | — | Raw `csrftoken` cookie (optional). Alias: `CSRF_TOKEN` |
| `INSTAGRAM_DS_USER_ID` | no | — | Raw `ds_user_id` cookie (optional). Alias: `DS_USER_ID` |
| `INSTAGRAM_TOTP_SEED` | no | — | Reusable base32 authenticator secret for 2FA (never logged) |
| `INSTAGRAM_2FA_CODE` | no | — | One-shot 6-digit TOTP or 8-digit backup code (never logged) |
| `TELEGRAM_BOT_TOKEN` | no | — | Needed for notifications |
| `TELEGRAM_CHAT_ID` | no | — | Needed for notifications |
| `ARCHIVE_TIME` | no | `24h` | e.g. `90m`, `24h`, `2d` → seconds (also the hourly scheduler default) |
| `ARCHIVE_VIEWS` | no | `1000` | Views threshold (also scheduler default) |
| `ARCHIVE_BATCH` | no | `50` | Max DB rows scanned per archive run |
| `QUALITY_ENABLED` | no | `1` | Master switch for the quality gate |
| `QUALITY_MIN_BYTES` | no | `300000` | Min video file size; smaller → reject |
| `QUALITY_MIN_WIDTH` | no | `720` | Min video width px |
| `QUALITY_MIN_HEIGHT` | no | `960` | Min video height px |
| `QUALITY_STRICT` | no | `0` | `1` = reject when resolution can't be determined; `0` = allow with warning |
| `COMMENT_ENABLED` | no | `0` | Master switch for auto-comments (off by default; `&comment=1` per call) |
| `COMMENT_TEXT` | no | `FOLLOW ME / FOLLOW FOR MORE / FOLLOW ME` | `\|`-separated rotation, random pick per post (fire emoji variants) |
| `SESSION_REUSE_TTL_MIN` | no | `120` | In-process authenticated-client reuse TTL |
| `IG_CALL_JITTER_SEC` | no | `1,3` | `min,max` human-like jitter before writes / between reads |
| `HIDELIKE` | no | `1` | `extra_data={"like_and_view_counts_disabled":1}`. Alias: `HIDE_LIKE_VIEW_COUNTS` |
| `BOTLOG` | no | `1` | `1` = Telegram notifications on |
| `THUMBNAIL_URL` | no | — | Global cover fallback; then `COVER_FILE`, then `COVER_DIR` |
| `COVER_MODE` | no | `static` | `random` or `static` |
| `COVER_DIR` | no | — | Thumbnail dir fallback |
| `COVER_FILE` | no | — | Thumbnail file fallback (beats `COVER_DIR`) |
| `FETCH_COUNT` | no | `30` | Reel candidates per run. Alias: `REEL_FETCH_COUNT` |
| `MIN_POST_INTERVAL_MIN` | no | `30` | Pacing floor (slept inside jobs, same candidate retried) |
| `MAX_PER_DAY` | no | `10` | Daily cap (fails the run when hit) |
| `MAX_UPLOADS_PER_RUN` | no | `=MAX_PER_DAY` | Legacy per-run cap, clamped to `MAX_PER_DAY` |
| `LOG_LEVEL` | no | `INFO` | Logger level |
| `PORT` | no | `8000` | Runtime port |
| `PYTHON_VERSION` | render | `3.12.7` | Pinned runtime |

> **Session note:** `INSTAGRAM_SESSION_STATE` is aiograpi settings JSON.
> Raw browser cookies (`INSTAGRAM_SESSIONID` + `csrftoken` / `ds_user_id`)
> log in via `login_by_sessionid` or cookie injection, **bypassing 2FA**.
> Cookies expire — refresh when auth fails; fallback is username/password
> (2FA/challenge notify unchanged). `DESTINATION_USERNAME` is unused.
>
> **2FA note:** exactly ONE verification attempt via
> `login(..., verification_code=code)` from `INSTAGRAM_TOTP_SEED` (fresh per
> 30 s window) or one-shot `INSTAGRAM_2FA_CODE`. No retry loops. SMS codes not
> supported — use an authenticator app or session cookies.

## Quality gate

Runs per candidate after download, before upload. Order: file-size floor →
real resolution (`ffprobe`, else stdlib MP4 `tkhd` parse) vs
`QUALITY_MIN_WIDTH`×`QUALITY_MIN_HEIGHT` → reject logs
`quality reject: low-res WxH < min` and moves to the next candidate (counts as
an attempt). Unprobable files: allowed with a warning unless `QUALITY_STRICT=1`.
Thumbnails are advisory only and never block. Disable wall-to-wall with
`QUALITY_ENABLED=0`.

## Comment engine

After each successful upload (own repost only, never elsewhere): posts one
random `COMMENT_TEXT` variant via `media_comment` (retry once on pk-only id),
then best-effort `comment_pin`. States in summary/telegram:
`pinned` / `posted` / `skipped`. Comment failures never fail the publish.
Identical CTAs on every post can look spammy — rotate 3+ variants.

## Retirement model (read before `all=1`)

- DB tracks **source** codes; each row also stores **our repost**
  (`repost_code`/`repost_pk`, captured at upload or backfilled by matching
  `Credit=@author` + post time in our own feed).
- All reads/views target the repost, never the source. Views = repost
  `play_count` (clips).
- Instagram cannot archive reels (`cannot archive Clips media`, proven across
  5 request shapes), so clips retire via `media_delete` **iff**
  `owner == logged-in account**; anything else (or unknown ownership) → local
  mark only. Photos use `media_archive` variants.
- `GET /archive?all=1` processes every `archived=0` row: live reposts retire,
  already-deleted ones are marked done, unmapped rows are kept+scanned.

## Stealth & free-plan (0.1 CPU / 512 MB, single instance, idles after 15 min)

- **Session reuse:** cached `SESSION_REUSE_TTL_MIN` (120); sessionid first,
  password fallback; auth errors invalidate.
- **Call-volume discipline:** timeline stops at `FETCH_COUNT` (~5 pages max);
  jittered sleeps; archive capped at `ARCHIVE_BATCH`; `insights_media`
  skipped when `media_info` has views; single-attempt writes.
- **Backoff:** reads retry ≤3× on 429 only; auth raises immediately.
- **Jobs vs spin-down:** background jobs survive client disconnects but **not**
  instance restarts (in-memory registry; Turso rows are the durable record).
  Keep an external pinger on `/health` (~10 min) so the free instance stays
  warm; long multi-hour upload jobs should be re-polled after any deploy.
- **Why no auto-follow/like/DM:** ban-risk multipliers; writes are limited to
  publishing own reels, own-post comments, and retiring own reposts.

## MCP setup (brief)

- **Render MCP**: point at this repo; deploy the `render.yaml` Blueprint; set
  secrets in the dashboard (all `sync:false`).
- **GitHub MCP**: push this workspace, open PRs, wire Render auto-deploys.
- **Turso MCP**: create DB, copy `TURSO_URL` + token, run `schema.sql`
  (app also runs it via `init_db` on startup; `repost_code`/`repost_pk`
  backfilled by `ALTER TABLE` on boot).

## Thumbnail URL usage

Priority: per-post `?thumb=<url>` → `THUMBNAIL_URL` → `COVER_FILE` →
`COVER_DIR` → `media.thumbnail_url`. Must be direct
`*.{jpg,jpeg,png,webp}`. Cached to `/tmp/thumbs/{code}.jpg`, Pillow→JPEG,
passed as local `Path` to `clip_upload`.

## Test plan (no pytest; manual curl + DB checks)

1. `curl .../health` → `{"status":"ok"}` (no key).
2. `curl ".../upload?key=WRONG"` → 403.
3. `curl ".../upload?key=...&amount=1"` → `job_id` instantly; poll
   `.../a_job?key=...&id=...` → `done`, `published_count:1`,
   `repost_code` set, `comment:{posted,pinned}`.
4. Turso: new row has `repost_code`; `/live` shows it.
5. `curl ".../archive?key=...&all=1"` → job; poll → `done`; repost deleted
   on IG (`delete-own-clip → True` in logs), row `archived=1`.
6. `curl ".../archive?key=...&time=1s&views=1000000&wait=1"` → sync summary
   (watch the 120 s client timeout on big sweeps — prefer job mode).
7. Second `/upload` within 30 min → job waits on pacing (same candidate,
   `sleep 60` loop); over daily cap → `error` with `RateLimited`.
8. Wrong key on `/live`, `/archive`, `/a_job` → 403; unknown job id → 404.
