"""Async Instagram client (aiograpi). Import-safe: no login at import.

Archive policy: real `media_archive` + DB flags; fallback to `media_delete`
only if archive raises, plus local-only mark. See README.
"""
import asyncio
import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

import httpx

log = logging.getLogger("instaward-bot")

_client: Any = None
_lock = asyncio.Lock()


def get_lock() -> asyncio.Lock:
    return _lock


def get_client() -> Any:
    """Singleton aiograpi Client with conservative delays. No I/O."""
    global _client
    if _client is None:
        from aiograpi import Client

        _client = Client(delay_range=[1, 3])

        def _challenge_handler(username: str, choice: Any = None) -> Any:
            log.warning("instagram challenge required for %s", username)
            raise RuntimeError(f"Instagram challenge required for {username}")

        try:
            _client.challenge_code_handler = _challenge_handler
        except Exception:  # noqa: BLE001 - attribute may vary by version
            pass
    return _client


def _dump_settings(cl: Any) -> dict[str, Any]:
    for name in ("get_settings", "dump_settings"):
        fn = getattr(cl, name, None)
        if callable(fn):
            try:
                data = fn()
                if isinstance(data, dict):
                    return data
            except Exception:  # noqa: BLE001
                continue
    return {}


async def _verify_session(cl: Any) -> None:
    """Light read to prove sessionid cookies work. Raises on failure."""
    import inspect

    username = os.getenv("INSTAGRAM_USERNAME", "")
    errors: list[str] = []
    for attempt in (
        lambda: cl.get_timeline_feed("cold_start_fetch"),
        lambda: cl.get_timeline_feed(),
    ):
        try:
            res = attempt()
            if inspect.isawaitable(res):
                await res
            return
        except Exception as exc:  # noqa: BLE001
            errors.append(f"timeline: {type(exc).__name__}")
    if username:
        for name in ("user_info_by_username", "user_id_from_username"):
            fn = getattr(cl, name, None)
            if callable(fn):
                try:
                    res = fn(username)
                    if inspect.isawaitable(res):
                        await res
                    return
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"{name}: {type(exc).__name__}")
                    continue
    raise RuntimeError("session verify failed: " + "; ".join(errors))


def _inject_instagram_cookies(cl: Any, sessionid: str, csrf: str, ds_user_id: str) -> None:
    """Manually inject session cookies for .instagram.com (defensive)."""
    cookies = {"sessionid": sessionid}
    if csrf:
        cookies["csrftoken"] = csrf
    if ds_user_id:
        cookies["ds_user_id"] = ds_user_id
    injected = False
    try:
        jar = getattr(cl, "cookie_dict", None)
        if isinstance(jar, dict):
            jar.update(cookies)
            injected = True
    except Exception:  # noqa: BLE001
        pass
    holders: list[Any] = [cl]
    for attr in ("private", "session", "client"):
        try:
            holders.append(getattr(cl, attr, None))
        except Exception:  # noqa: BLE001
            continue
    for holder in holders:
        if holder is None:
            continue
        for jar_attr in ("cookies", "cookie_jar", "jar"):
            try:
                jar = getattr(holder, jar_attr, None)
            except Exception:  # noqa: BLE001
                continue
            if jar is None:
                continue
            setter = getattr(jar, "set", None)
            if callable(setter):
                try:
                    for k, v in cookies.items():
                        try:
                            setter(k, v, domain=".instagram.com")
                        except TypeError:
                            setter(k, v)
                    injected = True
                except Exception:  # noqa: BLE001
                    continue
            elif isinstance(jar, dict):
                try:
                    jar.update(cookies)
                    injected = True
                except Exception:  # noqa: BLE001
                    continue
        for helper in ("set_cookie", "set_cookies"):
            fn = getattr(holder, helper, None)
            if callable(fn):
                try:
                    fn(cookies, ".instagram.com")  # type: ignore[operator]
                    injected = True
                except Exception:  # noqa: BLE001
                    continue
    if not injected:
        raise RuntimeError("no supported cookie jar found on client")


async def _try_sessionid_login(cl: Any, dbmod: Any) -> Any | None:
    """Attempt sessionid login BEFORE the password path."""
    import inspect

    from app import config as cfg

    def _live(*names: str) -> str:
        for n in names:
            v = os.getenv(n)
            if v is not None and str(v).strip() != "":
                return str(v)
        return ""

    raw = getattr(cfg, "INSTAGRAM_SESSIONID", "") or _live(
        "INSTAGRAM_SESSIONID", "SESSION_ID", "INSTAGRAM_SESSION"
    )
    if not raw or not str(raw).strip():
        return None
    try:
        from urllib.parse import unquote

        s = str(raw).strip()
        if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
            s = s[1:-1].strip()
        sessionid = unquote(s).strip().strip("'\" ")
    except Exception:  # noqa: BLE001
        sessionid = str(raw).strip()
    if not sessionid:
        return None
    csrf = getattr(cfg, "INSTAGRAM_CSRFTOKEN", "") or _live("INSTAGRAM_CSRFTOKEN", "CSRF_TOKEN")
    ds_user_id = getattr(cfg, "INSTAGRAM_DS_USER_ID", "") or _live(
        "INSTAGRAM_DS_USER_ID", "DS_USER_ID"
    )
    fn = getattr(cl, "login_by_sessionid", None)
    if callable(fn):
        try:
            res = fn(sessionid)
            if inspect.isawaitable(res):
                await res
            await _verify_session(cl)
            try:
                dumped = _dump_settings(cl)
                if dumped:
                    await dbmod.save_session("ig_session", dumped)
            except Exception as exc:  # noqa: BLE001
                log.warning("save_session after sessionid login failed: %s", exc)
            log.info("instagram sessionid login succeeded")
            return cl
        except Exception as exc:  # noqa: BLE001
            log.warning("login_by_sessionid failed, trying cookie inject: %s", type(exc).__name__)
    try:
        _inject_instagram_cookies(cl, sessionid, str(csrf or "").strip(), str(ds_user_id or "").strip())
        await _verify_session(cl)
        try:
            dumped = _dump_settings(cl)
            if dumped:
                await dbmod.save_session("ig_session", dumped)
        except Exception as exc:  # noqa: BLE001
            log.warning("save_session after cookie login failed: %s", exc)
        log.info("instagram cookie-inject login succeeded")
        return cl
    except Exception as exc:  # noqa: BLE001
        log.warning("sessionid login failed, falling through to password: %s", type(exc).__name__)
        return None


async def ensure_login() -> Any:
    """Login via sessionid cookies first (bypasses 2FA), else username/password."""
    from app import db as dbmod

    async with _lock:
        cl = get_client()
        try:
            session_client = await _try_sessionid_login(cl, dbmod)
        except Exception as exc:  # noqa: BLE001 - never break password path
            log.warning("sessionid attempt errored: %s", exc)
            session_client = None
        if session_client is not None:
            return session_client
        username = os.getenv("INSTAGRAM_USERNAME", "")
        password = os.getenv("INSTAGRAM_PASSWORD", "")
        if not username or not password:
            raise RuntimeError("INSTAGRAM_USERNAME/INSTAGRAM_PASSWORD are not set")
        settings: Optional[dict[str, Any]] = None
        try:
            cached = await dbmod.load_session("ig_session")
            if cached:
                settings = json.loads(cached) if isinstance(cached, str) else cached
        except Exception as exc:  # noqa: BLE001
            log.warning("load_session failed: %s", exc)
        env_state = os.getenv("INSTAGRAM_SESSION_STATE", "")
        if env_state:
            try:
                settings = json.loads(env_state)
            except Exception as exc:  # noqa: BLE001
                log.warning("INSTAGRAM_SESSION_STATE is not valid JSON: %s", exc)
        if settings:
            try:
                cl.set_settings(settings)
            except Exception as exc:  # noqa: BLE001
                log.warning("set_settings failed: %s", exc)
        try:
            await cl.login(username, password)
        except Exception as exc:  # noqa: BLE001
            name = type(exc).__name__
            if "Challenge" in name or "challenge" in str(exc).lower():
                try:
                    from app import telegramlog as tg

                    await tg.notify_checkpoint(username)
                except Exception:  # noqa: BLE001
                    pass
            raise
        try:
            dumped = _dump_settings(cl)
            if dumped:
                await dbmod.save_session("ig_session", dumped)
        except Exception as exc:  # noqa: BLE001
            log.warning("save_session failed: %s", exc)
        return cl
