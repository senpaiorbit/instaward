"""Archive job: archive stale/low-view reposts via real media_archive.

Policy (also in README):
- Only DB-tracked rows (processed_media WHERE archived=0 AND archive_scanned=0).
- For each: media_info + insights for age/views.
- If age > threshold OR views < threshold -> media_archive(f"{pk}_{user_id}").
- Fallback to media_delete only if archive raises; else local-only mark.
- Always set archive_scanned=1 so we never hard-scan everything.
"""
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("instaward-bot")


def _parse_published_at(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        dt = datetime.fromisoformat(str(raw).replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:  # noqa: BLE001
        return None


def _extract_views(info: Any, insights: Any) -> int | None:
    for obj in (insights, info):
        if obj is None:
            continue
        if isinstance(obj, dict):
            for k in ("play_count", "view_count", "views", "plays"):
                v = obj.get(k)
                if isinstance(v, (int, float)):
                    return int(v)
        else:
            for k in ("play_count", "view_count"):
                v = getattr(obj, k, None)
                if isinstance(v, (int, float)):
                    return int(v)
    return None


def _extract_taken_at(info: Any) -> datetime | None:
    taken = getattr(info, "taken_at", None) if not isinstance(info, dict) else info.get("taken_at")
    if taken is None:
        return None
    try:
        if isinstance(taken, (int, float)):
            return datetime.fromtimestamp(float(taken), tz=timezone.utc)
        return _parse_published_at(str(taken))
    except Exception:  # noqa: BLE001
        return None


async def _resolve_pk(cl: Any, code: str) -> Any | None:
    for name in ("media_pk_from_code", "media_pk_from_url"):
        fn = getattr(cl, name, None)
        if callable(fn):
            try:
                arg = code if "code" in name else f"https://www.instagram.com/reel/{code}/"
                res = fn(arg)
                import inspect

                if inspect.isawaitable(res):
                    res = await res
                if res:
                    return res
            except Exception:  # noqa: BLE001
                continue
    return None


async def run_archive(time_sec: int, views_thresh: int) -> dict[str, Any]:
    from app import db as dbmod
    from app import ig as igmod

    cl = await igmod.ensure_login()
    rows = await dbmod.get_unscanned(limit=50)
    checked = archived = kept = 0
    errors: list[str] = []

    for row in rows:
        code = row["media_code"]
        checked += 1
        try:
            pk = await _resolve_pk(cl, code)
            info: Any = None
            insights: Any = None
            lock = igmod.get_lock()
            if pk is not None:
                async with lock:
                    try:
                        info = await cl.media_info(pk)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("media_info(%s) failed: %s", code, exc)
                    try:
                        fn = getattr(cl, "insights_media", None)
                        if fn is not None:
                            insights = await fn(pk)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("insights_media(%s) failed: %s", code, exc)

            views = _extract_views(info, insights)
            taken_at = _extract_taken_at(info) or _parse_published_at(row.get("published_at"))
            now = datetime.now(timezone.utc)
            age_sec = (now - taken_at).total_seconds() if taken_at else 0

            should_archive = False
            if taken_at and age_sec > time_sec:
                should_archive = True
            if views is not None and views < views_thresh:
                should_archive = True

            if pk is None and taken_at is None:
                pass

            if should_archive and pk is not None:
                user_id = getattr(getattr(info, "user", None), "pk", None) or getattr(
                    getattr(info, "user", None), "id", ""
                )
                media_id = f"{pk}_{user_id}" if user_id else pk
                async with lock:
                    try:
                        await cl.media_archive(media_id)
                    except Exception as arch_exc:  # noqa: BLE001
                        log.warning("media_archive(%s) failed, trying delete: %s", code, arch_exc)
                        try:
                            await cl.media_delete(pk)
                        except Exception as del_exc:  # noqa: BLE001
                            log.warning("media_delete(%s) failed, local-only mark: %s", code, del_exc)
                await dbmod.mark_archived(code, 1)
                archived += 1
            elif should_archive and pk is None:
                await dbmod.mark_archived(code, 1)
                archived += 1
            else:
                await dbmod.mark_scanned(code, 0)
                kept += 1
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{code}: {type(exc).__name__}: {exc}")
            log.warning("archive row %s failed: %s", code, exc)

    summary = {"checked": checked, "archived": archived, "kept": kept, "errors": errors}
    try:
        from app import telegramlog as tg

        if checked:
            await tg.send_message(f"archive scan: checked={checked} archived={archived} kept={kept}")
    except Exception:  # noqa: BLE001
        pass
    return summary
