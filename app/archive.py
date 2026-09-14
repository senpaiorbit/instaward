"""Archive job: archive stale/low-view reposts via real media_archive.

Policy:
- Only DB-tracked rows (processed_media WHERE archived=0 AND archive_scanned=0).
- For each: media_info + insights for age/views.
- If age > threshold AND views <= threshold -> media_archive(f"{pk}_{user_id}").
- Fallback to local-only mark if archive fails; NEVER delete.
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


def _as_count(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def _is_clip_media(obj: Any) -> bool:
    try:
        if isinstance(obj, dict):
            ptype = obj.get("product_type")
            if isinstance(ptype, str) and ptype.strip().lower() == "clips":
                return True
            return "clips_metadata" in obj and obj.get("clips_metadata") is not None
        ptype = getattr(obj, "product_type", None)
        if isinstance(ptype, str) and ptype.strip().lower() == "clips":
            return True
        return getattr(obj, "clips_metadata", None) is not None
    except Exception:
        return False


def _extract_views(info: Any, insights: Any) -> int | None:
    for obj in (insights, info):
        if obj is None:
            continue
        if isinstance(obj, dict):
            if "inline_insights_node" in obj and not any(
                k in obj for k in ("play_count", "view_count", "views", "plays")
            ):
                continue
            pc = _as_count(obj.get("play_count"))
            if pc is not None:
                return pc
            if _is_clip_media(obj):
                continue
            for k in ("view_count", "views", "plays"):
                vc = _as_count(obj.get(k))
                if vc is not None:
                    return vc
            continue
        pc = _as_count(getattr(obj, "play_count", None))
        if pc is not None:
            return pc
        if _is_clip_media(obj):
            continue
        vc = _as_count(getattr(obj, "view_count", None))
        if vc is not None:
            return vc
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
            except Exception as exc:  # noqa: BLE001
                log.warning("_resolve_pk(%s) via %s failed: %s: %s", code, name, type(exc).__name__, exc)
                continue
    log.warning("_resolve_pk(%s) returned no pk", code)
    return None


_UNSET: Any = object()


async def _archive_media_once(cl: Any, code: str, pk: Any, info: Any) -> str:
    """Try archive variants in order; fallback is local-only mark. No delete."""
    from app import ig as igmod

    pk_str = str(pk)
    owner = getattr(getattr(info, "user", None), "pk", None) or getattr(
        getattr(info, "user", None), "id", ""
    )
    owner = str(owner or "").strip()
    self_uid = str(getattr(cl, "user_id", "") or "")
    full_id = f"{pk_str}_{owner}" if owner else pk_str
    log.info(
        "archive try %s pk=%s owner=%s self=%s full=%s",
        code, pk_str, owner, self_uid, full_id,
    )
    await igmod.jitter_delay()
    lock = igmod.get_lock()

    async def _attempt(label: str, fn: Any) -> bool:
        try:
            res = fn()
            import inspect
            if inspect.isawaitable(res):
                res = await res
            ok = res is True or (isinstance(res, dict) and res.get("status") == "ok")
            log.info("archive variant %s(%s) -> %r", label, code, res)
            return bool(ok)
        except Exception as exc:  # noqa: BLE001
            if igmod.is_auth_error(exc):
                igmod.mark_session_stale()
                raise
            log.warning("archive variant %s(%s) failed: %s: %s", label, code, type(exc).__name__, exc)
            return False

    async with lock:
        # 1) pk-only: lets aiograpi resolve the true owner via media_user().
        if await _attempt("pk-only", lambda: cl.media_archive(pk_str)):
            return "archive:pk-only"
        # 2) full id via library (previous behaviour).
        if full_id != pk_str and await _attempt("full-id", lambda: cl.media_archive(full_id)):
            return "archive:full-id"
        # 3) direct endpoint with pk in URL, full id in body.
        try:
            data = cl.with_action_data({"media_id": full_id})
            if await _attempt(
                "direct-pk-url",
                lambda: cl.private_request(f"media/{pk_str}/only_me/", data),
            ):
                return "archive:direct-pk-url"
        except Exception as exc:  # noqa: BLE001
            log.warning("archive variant direct-pk-url(%s) setup failed: %s", code, exc)
        # 4) legacy payload (instagrapi-style: _uid + radio_type).
        try:
            payload = {"_uid": self_uid or owner, "media_id": full_id, "radio_type": "normal"}
            if await _attempt(
                "legacy-payload",
                lambda: cl.private_request(f"media/{full_id}/only_me/", payload),
            ):
                return "archive:legacy-payload"
        except Exception as exc:  # noqa: BLE001
            log.warning("archive variant legacy-payload(%s) setup failed: %s", code, exc)
        log.warning("all archive variants failed for %s, local-only mark", code)
        return "local"


async def archive_single(cl: Any, code: str, pk: Any = _UNSET, info: Any = _UNSET) -> dict[str, Any]:
    """Archive ONE shortcode, bypassing DB age/views gating."""
    from app import db as dbmod
    from app import ig as igmod

    log.info("archive_single start code=%s", code)
    if pk is _UNSET:
        pk = await _resolve_pk(cl, code)
    log.info("archive_single %s resolved pk=%s", code, pk)
    if pk is None:
        log.warning("archive_single %s: no pk, local-only mark", code)
        await dbmod.mark_archived(code, 1)
        log.info("archive_single %s done method=local (unresolved pk)", code)
        return {"code": code, "pk": None, "archived": True, "method": "local", "reason": "unresolved-pk"}
    if info is _UNSET:
        lock = igmod.get_lock()
        async with lock:
            try:
                info = await igmod.read_with_backoff("media_info", cl.media_info, pk)
            except Exception as exc:  # noqa: BLE001
                log.warning("media_info(%s) failed: %s: %s", code, type(exc).__name__, exc)
                if igmod.is_auth_error(exc):
                    raise
                raise
        if info is None:
            raise RuntimeError(f"media_info({code}) returned no media")
        log.info("archive_single %s exists pk=%s, archiving", code, pk)
    else:
        log.info("archive_single %s using provided pk=%s info", code, pk)
    method = await _archive_media_once(cl, code, pk, info)
    await dbmod.mark_archived(code, 1)
    log.info("archive_single %s done method=%s", code, method)
    try:
        pk_str: Any = str(pk)
    except Exception:  # noqa: BLE001
        pk_str = None
    return {"code": code, "pk": pk_str, "archived": True, "method": method}


async def run_archive(time_sec: int, views_thresh: int) -> dict[str, Any]:
    from app import config
    from app import db as dbmod
    from app import ig as igmod

    cl = await igmod.ensure_login()
    try:
        batch = int(getattr(config, "ARCHIVE_BATCH", 50) or 50)
    except (TypeError, ValueError):
        batch = 50
    rows = await dbmod.get_unscanned(limit=max(1, batch))
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
                        info = await igmod.read_with_backoff("media_info", cl.media_info, pk)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("media_info(%s) failed: %s: %s", code, type(exc).__name__, exc)
                        if igmod.is_auth_error(exc):
                            raise
                    if _extract_views(info, None) is None:
                        try:
                            fn = getattr(cl, "insights_media", None)
                            if fn is not None:
                                insights = await igmod.read_with_backoff("insights_media", fn, pk)
                        except Exception as exc:  # noqa: BLE001
                            log.warning("insights_media(%s) failed: %s: %s", code, type(exc).__name__, exc)
                            if igmod.is_auth_error(exc):
                                raise

            views = _extract_views(info, insights)
            taken_at = _parse_published_at(row.get("published_at")) or _extract_taken_at(info)
            now = datetime.now(timezone.utc)
            age_sec = (now - taken_at).total_seconds() if taken_at else 0

            should_archive = False
            if (
                taken_at
                and age_sec > time_sec
                and views is not None
                and views <= views_thresh
            ):
                should_archive = True
            try:
                ptype = info.get("product_type") if isinstance(info, dict) else getattr(info, "product_type", None)
            except Exception:
                ptype = None
            try:
                mtype = info.get("media_type") if isinstance(info, dict) else getattr(info, "media_type", None)
            except Exception:
                mtype = None
            try:
                raw_pc = info.get("play_count") if isinstance(info, dict) else getattr(info, "play_count", None)
            except Exception:
                raw_pc = None
            try:
                raw_vc = info.get("view_count") if isinstance(info, dict) else getattr(info, "view_count", None)
            except Exception:
                raw_vc = None
            log.info(
                "archive row %s: pk=%s ptype=%s mtype=%s raw_pc=%s raw_vc=%s views=%s taken_at=%s age_sec=%s time_sec=%s views_thresh=%s -> %s",
                code, pk, ptype, mtype, raw_pc, raw_vc, views, taken_at, round(age_sec, 1), time_sec, views_thresh,
                "ARCHIVE" if should_archive else "keep",
            )

            if should_archive:
                result = await archive_single(cl, code, pk=pk, info=info)
                log.info("archive row %s archived via %s", code, result.get("method"))
                archived += 1
            else:
                await dbmod.mark_scanned(code, 0)
                kept += 1
        except Exception as exc:  # noqa: BLE001
            try:
                if igmod.is_auth_error(exc):
                    igmod.mark_session_stale()
            except Exception:  # noqa: BLE001
                pass
            errors.append(f"{code}: {type(exc).__name__}: {exc}")
            log.warning("archive row %s failed: %s: %s", code, type(exc).__name__, exc)

    summary = {"checked": checked, "archived": archived, "kept": kept, "errors": errors}
    try:
        from app import telegramlog as tg
        if checked:
            await tg.send_message(f"🗄 archive scan: checked={checked} archived={archived} kept={kept}")
    except Exception:  # noqa: BLE001
        pass
    return summary
