"""FastAPI entrypoint. Import-safe: scheduler starts in lifespan, no IG at import."""
# 30s cron support: all endpoints accept ?cronjob=1 for instant (<2s) background jobs
import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from app import config
from app.telegramlog import TelegramLogHandler

log = logging.getLogger("instaward-bot")
logging.basicConfig(level=logging.INFO)
log.addHandler(TelegramLogHandler())


def _check_key(key: str) -> None:
    if not config.ENV_KEY or key != config.ENV_KEY:
        raise HTTPException(status_code=403, detail="forbidden")


def _is_cron(cronjob: int | None) -> bool:
    try:
        return bool(cronjob) and int(cronjob) != 0
    except Exception:
        return False


async def _turso_precheck() -> None:
    """Raise HTTP 503 with actionable hint if Turso auth is broken."""
    from app import db as dbmod

    health = await dbmod.db_health()
    if not health.get("ok"):
        msg = health.get("error", "db unreachable")
        hint = health.get("hint", "")
        detail = f"Turso DB unreachable: {msg}"
        if hint:
            detail += f"\n{hint}"
        raise HTTPException(status_code=503, detail=detail)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app import db as dbmod

    try:
        await dbmod.init_db()
        h = await dbmod.db_health()
        if h.get("ok"):
            log.info("db health ok at startup")
        else:
            log.warning("db health FAILED at startup: %s", h.get("error"))
            if h.get("hint"):
                log.warning("%s", h.get("hint"))
    except Exception as exc:  # noqa: BLE001 - DB may be unconfigured locally
        log.warning("init_db failed at startup: %s", exc)
    scheduler = None
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler

        from app.archive import run_archive

        scheduler = AsyncIOScheduler()

        async def _archive_tick() -> None:
            try:
                await run_archive(config.ARCHIVE_TIME_SEC, config.ARCHIVE_VIEWS)
            except Exception as exc:  # noqa: BLE001
                log.warning("scheduled archive failed: %s", exc)
                try:
                    from app import telegramlog as tg

                    await tg.notify_error("scheduled archive", exc)
                except Exception:  # noqa: BLE001
                    pass

        scheduler.add_job(_archive_tick, "interval", minutes=60)
        scheduler.start()
    except Exception as exc:  # noqa: BLE001
        log.warning("scheduler start failed: %s", exc)
    try:
        yield
    finally:
        if scheduler is not None:
            try:
                scheduler.shutdown(wait=False)
            except Exception:  # noqa: BLE001
                pass


app = FastAPI(title="instaward-bot")


@app.get("/health")
async def health(cronjob: int | None = Query(None)) -> dict[str, str]:
    return {"status": "ok"}


@app.get("/turso_check")
async def turso_check(key: str = Query(""), cronjob: int | None = Query(None)) -> dict:
    _check_key(key)
    from app import db as dbmod

    h = await dbmod.db_health()
    return {"ok": h.get("ok", False), "health": h}


@app.get("/reconnect")
async def reconnect(key: str = Query(""), cronjob: int | None = Query(None)) -> dict:
    _check_key(key)
    from app import db as dbmod
    from app import ig as igmod

    if _is_cron(cronjob):
        h = await dbmod.db_health()
        if not h.get("ok"):
            raise HTTPException(status_code=503, detail=f"Turso DB unreachable: {h.get('error')}\n{h.get('hint','')}")
        try:
            igmod.mark_session_stale()
        except Exception:
            pass
        async def _do_reconnect() -> None:
            try:
                await igmod.ensure_login()
            except Exception as exc:
                log.warning("background reconnect failed: %s", exc)
        asyncio.create_task(_do_reconnect())
        return {"ok": True, "reconnected": "background", "cronjob": 1}
    h = await dbmod.db_health()
    if not h.get("ok"):
        raise HTTPException(status_code=503, detail=f"Turso DB unreachable: {h.get('error')}\n{h.get('hint','')}")
    try:
        igmod.mark_session_stale()
    except Exception:
        pass
    try:
        cl = await igmod.ensure_login()
        uid = str(getattr(cl, "user_id", "") or "")
        return {"ok": True, "reconnected": True, "user_id": uid}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


@app.get("/upload")
async def upload(key: str = Query(""), hidelike: int | None = Query(None), botlog: int | None = Query(None), thumb: str | None = Query(None), attempts: int | None = Query(None, ge=1, le=100), amount: int | None = Query(None, ge=1, le=50), comment: int | None = Query(None), cronjob: int | None = Query(None)) -> dict:
    _check_key(key)
    is_cron = _is_cron(cronjob)
    if is_cron and amount is None:
        amount = 1
    await _turso_precheck()
    from app import curation
    from app import telegramlog as tg
    hide_flag = None if hidelike is None else bool(hidelike)
    comment_flag = None if comment is None else bool(comment)
    if amount is None and not is_cron:
        try:
            summary = await curation.run_curation(hide_flag, thumbnail_override=thumb or None, max_attempts=attempts, comment=comment_flag)
            return {"ok": True, "summary": summary}
        except curation.RateLimited as exc:
            raise HTTPException(status_code=429, detail=exc.detail)
        except HTTPException:
            raise
        except Exception as exc:  # noqa: BLE001
            blob = f"{type(exc).__name__} {exc}".lower()
            if "invalidtoken" in blob or ("jwt" in blob and "hrana" in blob):
                from app.db import _turso_error_hint
                raise HTTPException(status_code=503, detail=f"Turso auth failed: {exc}\n{_turso_error_hint()}")
            await tg.notify_error("upload", exc)
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    from app import jobs as jobsmod
    existing = jobsmod.current_running("upload")
    jobsmod.prune()
    if existing:
        return {"ok": True, "job_id": existing["id"], "status": "running", "target_count": amount, "note": "already running", "cronjob": 1 if is_cron else 0}
    job = jobsmod.create_job("upload", {"hide_flag": hide_flag, "thumbnail_override": thumb or None, "max_attempts": attempts, "target_count": amount, "comment": comment_flag})
    kwargs = {"hide_like": hide_flag, "thumbnail_override": thumb or None, "max_attempts": attempts, "target_count": amount, "comment": comment_flag}
    asyncio.create_task(jobsmod.run_upload_job(job["id"], kwargs))
    return {"ok": True, "job_id": job["id"], "status": "running", "target_count": amount, "comment": comment_flag, "cronjob": 1 if is_cron else 0}


@app.get("/live")
async def live(key: str = Query(""), cronjob: int | None = Query(None)) -> dict:
    _check_key(key)
    await _turso_precheck()
    from app import db as dbmod
    rows = await dbmod.get_recent(5)
    return {"ok": True, "items": rows}


@app.get("/archive")
async def archive(key: str = Query(""), time: str | None = Query(None), views: int | None = Query(None), wait: int | None = Query(None), all: int | None = Query(None), cronjob: int | None = Query(None)) -> dict:
    _check_key(key)
    await _turso_precheck()
    from app import telegramlog as tg
    from app.archive import run_archive
    try:
        time_sec = config.parse_duration_to_seconds(time, config.ARCHIVE_TIME_SEC) if time else config.ARCHIVE_TIME_SEC
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    views_thresh = config.ARCHIVE_VIEWS if views is None else views
    all_flag = bool(all)
    is_cron = _is_cron(cronjob)
    if is_cron:
        wait = 0
    if wait and not is_cron:
        try:
            summary = await run_archive(time_sec, views_thresh, all=all_flag)
            return {"ok": True, "time_sec": time_sec, "views": views_thresh, "all": all_flag, "summary": summary}
        except Exception as exc:  # noqa: BLE001
            await tg.notify_error("archive", exc)
            raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
    from app import jobs as jobsmod
    existing = jobsmod.current_running("archive")
    jobsmod.prune()
    if existing:
        return {"ok": True, "job_id": existing["id"], "status": "running", "time_sec": time_sec, "views": views_thresh, "all": all_flag, "note": "already running", "cronjob": 1 if is_cron else 0}
    job = jobsmod.create_job("archive", {"time_sec": time_sec, "views": views_thresh, "all": all_flag})
    asyncio.create_task(jobsmod.run_archive_job(job["id"], time_sec, views_thresh, all_flag))
    return {"ok": True, "job_id": job["id"], "status": "running", "time_sec": time_sec, "views": views_thresh, "all": all_flag, "cronjob": 1 if is_cron else 0}


@app.get("/a_job")
async def a_job(key: str = Query(""), id: str = Query(""), cronjob: int | None = Query(None)) -> dict:
    _check_key(key)
    from app import jobs as jobsmod
    job = jobsmod.get_job((id or "").strip())
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return {"ok": True, "job": job}


@app.get("/archive_one")
async def archive_one(key: str = Query(""), code: str = Query(""), cronjob: int | None = Query(None)) -> dict:
    _check_key(key)
    await _turso_precheck()
    from app import ig as igmod
    from app import telegramlog as tg
    from app.archive import archive_single
    if _is_cron(cronjob):
        shortcode = (code or "").strip()
        if not shortcode:
            raise HTTPException(status_code=400, detail="code is required")
        from app import jobs as jobsmod
        job = jobsmod.create_job("archive_one", {"code": shortcode})
        async def _do_one() -> None:
            try:
                cl = await igmod.ensure_login()
                summary = await archive_single(cl, shortcode)
                job.update(status="done", finished_at=jobsmod._now_iso(), result=summary)
            except Exception as exc:
                job.update(status="error", finished_at=jobsmod._now_iso(), error=f"{type(exc).__name__}: {exc}")
        asyncio.create_task(_do_one())
        return {"ok": True, "job_id": job["id"], "status": "running", "code": shortcode, "cronjob": 1}
    shortcode = (code or "").strip()
    if not shortcode:
        raise HTTPException(status_code=400, detail="code is required")
    try:
        log.info("archive_one request code=%s", shortcode)
        cl = await igmod.ensure_login()
        summary = await archive_single(cl, shortcode)
        return {"ok": True, "summary": summary}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        await tg.notify_error("archive_one", exc)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
