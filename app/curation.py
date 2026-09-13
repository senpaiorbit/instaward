"""Curation: retry over candidates until one reel publishes. Import-safe."""
import logging
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("instaward-bot")


class RateLimited(Exception):
    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


async def _check_pacing() -> None:
    from app import config, db as dbmod

    last = await dbmod.last_published_at()
    if last:
        try:
            dt = datetime.fromisoformat(str(last).replace("Z", ""))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            age_min = (datetime.now(timezone.utc) - dt).total_seconds() / 60
            if age_min < config.MIN_POST_INTERVAL_MIN:
                raise RateLimited(
                    f"pacing: last post {age_min:.1f}min ago, minimum is "
                    f"{config.MIN_POST_INTERVAL_MIN}min"
                )
        except RateLimited:
            raise
        except Exception:  # noqa: BLE001 - unparsable timestamp => skip pacing
            pass
    today_count = await dbmod.count_today()
    if today_count >= config.MAX_PER_DAY:
        raise RateLimited(f"pacing: {today_count} posts today, max is {config.MAX_PER_DAY}")


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


async def run_curation(
    hide_like: bool | None = None,
    thumbnail_override: str | None = None,
    max_attempts: int | None = None,
) -> dict[str, Any]:
    """Fetch candidates once, then retry in random order until one publishes.

    Only raises after every attempted candidate failed. Returns summary with
    attempts, failed_codes, and the published media_code.
    """
    from app import config, db as dbmod
    from app import ig as igmod

    # Pacing guards run BEFORE any download/upload attempt.
    await _check_pacing()

    if hide_like is None:
        hide_like = bool(config.HIDELIKE)

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

    attempts = 0
    failed_codes: list[dict[str, str]] = []

    for media in fresh[:limit]:
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

            caption = f"via @{author}"
            if caption_text:
                caption += f"\n{caption_text[:1500]}"

            extra_data = {"like_and_view_counts_disabled": 1} if hide_like else {}
            lock = igmod.get_lock()
            async with lock:
                if thumb_path is not None:
                    await cl.clip_upload(
                        Path(video_path), caption, thumbnail=Path(thumb_path), extra_data=extra_data
                    )
                else:
                    await cl.clip_upload(Path(video_path), caption, extra_data=extra_data)

            original_url = f"https://www.instagram.com/reel/{code}/"
            await dbmod.insert_processed(code, author, original_url, video_url)

            summary = {
                "media_code": code,
                "author": author,
                "caption": caption[:300],
                "hide_like": bool(hide_like),
                "video_path": str(video_path),
                "thumbnail_path": str(thumb_path) if thumb_path else None,
                "attempts": attempts,
                "failed_codes": failed_codes,
                "failed_count": len(failed_codes),
            }
            try:
                from app import telegramlog as tg

                await tg.send_message(
                    f"published reel {code} via @{author} "
                    f"(hide_like={bool(hide_like)}, attempts={attempts})"
                )
            except Exception:  # noqa: BLE001
                pass
            return summary
        except Exception as exc:  # noqa: BLE001 - per-candidate failure: continue
            log.warning("candidate %s failed (%d/%d): %s: %s", code, attempts, limit, type(exc).__name__, exc)
            failed_codes.append({"code": code, "error": f"{type(exc).__name__}: {exc}"})
            _cleanup_tmp(video_path, thumb_path)
            continue

    raise RuntimeError(f"all {attempts} candidates failed: {[f['code'] for f in failed_codes]}")
