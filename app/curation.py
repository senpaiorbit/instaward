"""Curation: retry over candidates until target publishes. Import-safe."""
import asyncio
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("instaward-bot")


class RateLimited(Exception):
    def __init__(self, detail: str, kind: str = "daily") -> None:
        super().__init__(detail)
        self.detail = detail
        self.kind = kind


async def _check_pacing() -> None:
    from app import config, db as dbmod

    last = await dbmod.last_published_at()
    if last:
        try:
            # SQLite CURRENT_TIMESTAMP is "YYYY-MM-DD HH:MM:SS" (UTC, naive)
            dt = datetime.fromisoformat(str(last).replace("Z", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
            if age_min < config.MIN_POST_INTERVAL_MIN:
                raise RateLimited(
                    f"pacing: last post {age_min:.1f}min ago, minimum is "
                    f"{config.MIN_POST_INTERVAL_MIN}min",
                    kind="interval",
                )
        except RateLimited:
            raise
        except Exception:  # noqa: BLE001 - unparsable timestamp => skip pacing
            pass
    today_count = await dbmod.count_today()
    if today_count >= config.MAX_PER_DAY:
        raise RateLimited(f"pacing: {today_count} posts today, max is {config.MAX_PER_DAY}", kind="daily")


def _cleanup_tmp(*paths: Optional[Path]) -> None:
    for p in paths:
        if p is None:
            continue
        try:
            pp = Path(p)
            if str(pp).startswith("/tmp/") and pp.is_file():
                pp.unlink()
        except OSError:
            pass


def _build_caption(caption_text: str, author: str, max_len: int = 2000) -> str:
    """Full original caption + blank line + `Credit=@author` last line.

    Combined total capped at max_len (IG allows ~2200); the original part is
    cut first so the credit line is always preserved. Empty originals yield
    just the credit line.
    """
    credit = f"Credit=@{author}"
    body = (caption_text or "").strip()
    if not body:
        return credit[:max_len]
    full = f"{body}\n\n{credit}"
    if len(full) <= max_len:
        return full
    room = max_len - len(f"\n\n{credit}")
    if room <= 0:
        return credit[:max_len]
    return f"{body[:room].rstrip()}\n\n{credit}"


def _default_max_attempts(fresh_count: int) -> int:
    from app import config

    per_run = getattr(config, "MAX_UPLOADS_PER_RUN", 10) or 10
    try:
        cap = int(per_run) * 5 or 10
    except (TypeError, ValueError):
        cap = 10
    if cap <= 0:
        cap = 10
    return max(1, min(fresh_count, cap))


def _uploaded_identity(cl: Any, uploaded: Any) -> tuple[str, str]:
    """Extract (repost_code, repost_pk) from a clip_upload result.

    Defensive for both Media objects and dicts. Never raises.
    """
    try:
        if uploaded is None:
            return ("", "")
        pk = ""
        try:
            if isinstance(uploaded, dict):
                pk = str(uploaded.get("pk") or "")
                if not pk:
                    raw_id = uploaded.get("id")
                    if raw_id is not None:
                        pk = str(raw_id).split("_")[0]
            else:
                pk = str(getattr(uploaded, "pk", "") or "")
                if not pk:
                    raw_id = getattr(uploaded, "id", None)
                    if raw_id is not None:
                        pk = str(raw_id).split("_")[0]
        except Exception:  # noqa: BLE001
            pk = ""
        code = ""
        try:
            if isinstance(uploaded, dict):
                code = str(uploaded.get("code") or "")
            else:
                code = str(getattr(uploaded, "code", "") or "")
        except Exception:  # noqa: BLE001
            code = ""
        if not code and pk:
            try:
                fn = getattr(cl, "media_code_from_pk", None)
                if callable(fn):
                    resolved = fn(pk)
                    if resolved:
                        code = str(resolved)
            except Exception:  # noqa: BLE001
                pass
        return (code or "", pk or "")
    except Exception:  # noqa: BLE001
        return ("", "")


async def run_curation(
    hide_like: bool | None = None,
    thumbnail_override: str | None = None,
    max_attempts: int | None = None,
    target_count: int = 1,
    job: dict | None = None,
) -> dict[str, Any]:
    """Fetch candidates once, then retry in random order until target publishes.

    Only raises after every attempted candidate failed. Returns summary with
    attempts, failed_codes, and the published media_code(s).
    """
    from app import config, db as dbmod
    from app import ig as igmod

    def _sync_job() -> None:
        if job is None:
            return
        try:
            job["attempted"] = attempts
            job["published"] = len(published)
            job["failed"] = len(failed_codes)
            job["items"] = list(published)
            job["errors"] = [f["error"] for f in failed_codes][-10:]
        except Exception:  # noqa: BLE001
            pass

    if hide_like is None:
        hide_like = bool(config.HIDELIKE)

    try:
        want = max(1, int(target_count or 1))
    except (TypeError, ValueError):
        want = 1

    fetch_n = getattr(config, "FETCH_COUNT", 30) or 30
    cl = await igmod.ensure_login()
    candidates = await igmod.fetch_candidates(int(fetch_n))
    if not candidates:
        raise RuntimeError("no candidates found")

    processed = await dbmod.get_processed_codes()
    fresh = [m for m in candidates if str(getattr(m, "code", getattr(m, "pk", ""))) not in processed]
    if not fresh:
        raise RuntimeError("no unprocessed candidates (all already published)")
    random.shuffle(fresh)

    if max_attempts is None:
        limit = _default_max_attempts(len(fresh))
    else:
        try:
            limit = int(max_attempts)
        except (TypeError, ValueError):
            raise ValueError("max_attempts must be an integer")
        limit = max(1, min(limit, len(fresh)))
    limit = max(limit, min(len(fresh), want * 5))

    attempts = 0
    failed_codes: list[dict[str, str]] = []
    published: list[dict[str, str]] = []
    first: dict[str, Any] = {}

    for media in fresh[:limit]:
        # Pacing gate: interval waits retry the SAME candidate without
        # counting an attempt or recording a failure; daily cap fails the run.
        while True:
            try:
                await _check_pacing()
                break
            except RateLimited as rl:
                if rl.kind == "daily":
                    raise
                await asyncio.sleep(60)
        code = str(getattr(media, "code", getattr(media, "pk", "")))
        attempts += 1
        video_path: Optional[Path] = None
        thumb_path: Optional[Path] = None
        try:
            author = str(getattr(getattr(media, "user", None), "username", "unknown"))
            caption_text = str(getattr(media, "caption_text", "") or "")

            video_url = igmod.get_best_video_url(media)
            if not video_url:
                raise RuntimeError(f"no video url for {code}")
            video_path = await igmod.download_to_tmp(video_url)

            thumb_url = (
                thumbnail_override
                or os.getenv("THUMBNAIL_URL", "")
                or config.THUMBNAIL_URL
                or str(getattr(media, "thumbnail_url", "") or "")
            )
            thumb_path = await igmod.cache_thumbnail(thumb_url or None, code)

            caption = _build_caption(caption_text, author)

            extra_data = {"like_and_view_counts_disabled": 1} if hide_like else {}
            # Human-like pause before the write; single attempt per candidate
            # (writes are NEVER retried blindly — failures move to next).
            await igmod.jitter_delay()
            lock = igmod.get_lock()
            async with lock:
                if thumb_path is not None:
                    uploaded = await cl.clip_upload(
                        Path(video_path), caption, thumbnail=Path(thumb_path), extra_data=extra_data
                    )
                else:
                    uploaded = await cl.clip_upload(Path(video_path), caption, extra_data=extra_data)

            original_url = f"https://www.instagram.com/reel/{code}/"
            await dbmod.insert_processed(code, author, original_url, video_url)
            repost_code, repost_pk = _uploaded_identity(cl, uploaded)
            if repost_code or repost_pk:
                await dbmod.update_repost(code, repost_code, repost_pk)
                log.info("stored repost identity src=%s repost=%s pk=%s", code, repost_code, repost_pk)

            published.append({"media_code": code, "repost_code": repost_code, "author": author})
            if not first:
                first = {
                    "media_code": code,
                    "repost_code": repost_code,
                    "author": author,
                    "caption": caption[:300],
                    "video_path": str(video_path),
                    "thumbnail_path": str(thumb_path) if thumb_path else None,
                }
            _sync_job()
            if len(published) >= want:
                break
            continue
        except Exception as exc:  # noqa: BLE001 - per-candidate failure: continue
            log.warning("candidate %s failed (%d/%d): %s: %s", code, attempts, limit, type(exc).__name__, exc)
            try:
                if igmod.is_auth_error(exc):
                    igmod.mark_session_stale()
            except Exception:  # noqa: BLE001
                pass
            failed_codes.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
            _cleanup_tmp(video_path, thumb_path)
            _sync_job()
            continue

    if not published:
        raise RuntimeError(f"all {attempts} candidates failed: {[f['code'] for f in failed_codes]}")

    summary = {
        "media_code": first["media_code"],
        "repost_code": first["repost_code"],
        "author": first["author"],
        "caption": first["caption"],
        "hide_like": bool(hide_like),
        "video_path": first["video_path"],
        "thumbnail_path": first["thumbnail_path"],
        "attempts": attempts,
        "failed_codes": failed_codes,
        "failed_count": len(failed_codes),
        "published": published,
        "published_count": len(published),
    }
    try:
        from app import telegramlog as tg

        suffix = f" (+{len(published) - 1} more)" if len(published) > 1 else ""
        await tg.send_message(
            f"✅ published reel {first['media_code']} via @{first['author']} "
            f"(hide_like={bool(hide_like)}, attempts={attempts})"
            f" repost={first['repost_code'] or '-'}{suffix}"
        )
    except Exception:  # noqa: BLE001
        pass
    return summary
