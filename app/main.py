"""FastAPI entrypoint. Import-safe: scheduler starts in lifespan, no IG at import."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Query

from app import config
from app.telegramlog import TelegramLogHandler

log = logging.getLogger("instaward-bot")
logging.basicConfig(level=logging.INFO)
log.addHandler(TelegramLogHandler())

try:
    from app.ig_sessionid import install_sessionid_first

    install_sessionid_first()
except Exception as exc:  # noqa: BLE001 - sessionid patch is optional
    log.warning("sessionid patch not installed: %s", exc)


def _check_key(key: str) -> None:
    if not config.ENV_KEY or key != config.ENV_KEY:
        raise HTTPException(status_code=403, detail="forbidden")


@asynccontextmanager
async def lifespan(app: FastAPI):
    from app import db as dbmod

    try:
        await dbmod.init_db()
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
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/upload")
async def upload(
    key: str = Query(""),
    hidelike: int | None = Query(None),
    botlog: int | None = Query(None),
    thumb: str | None = Query(None),
    attempts: int | None = Query(None, ge=1, le=100),
) -> dict:
    _check_key(key)
    from app import curation
    from app import telegramlog as tg

    hide_flag = None if hidelike is None else bool(hidelike)
    try:
        summary = await curation.run_curation(
            hide_flag, thumbnail_override=thumb or None, max_attempts=attempts
        )
        return {"ok": True, "summary": summary}
    except curation.RateLimited as exc:
        raise HTTPException(status_code=429, detail=exc.detail)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        await tg.notify_error("upload", exc)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")


@app.get("/live")
async def live(key: str = Query("")) -> dict:
    _check_key(key)
    from app import db as dbmod

    rows = await dbmod.get_recent(5)
    return {"ok": True, "items": rows}


@app.get("/archive")
async def archive(
    key: str = Query(""),
    time: str | None = Query(None),
    views: int | None = Query(None),
) -> dict:
    _check_key(key)
    from app import telegramlog as tg
    from app.archive import run_archive

    try:
        time_sec = config.parse_duration_to_seconds(time, config.ARCHIVE_TIME_SEC) if time else config.ARCHIVE_TIME_SEC
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    views_thresh = config.ARCHIVE_VIEWS if views is None else views
    try:
        summary = await run_archive(time_sec, views_thresh)
        return {"ok": True, "summary": summary}
    except Exception as exc:  # noqa: BLE001
        await tg.notify_error("archive", exc)
        raise HTTPException(status_code=500, detail=f"{type(exc).__name__}: {exc}")
