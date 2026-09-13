"""Sessionid login helpers (2FA bypass) + runtime compat shims. Import-safe.

Call install_sessionid_first() once at startup (see app/main.py). It wraps
app.ig.ensure_login so raw session cookies (SESSION_ID / CSRF_TOKEN /
DS_USER_ID) are tried BEFORE the username/password path, and it backfills
newer helpers (jitter_delay / is_auth_error / mark_session_stale) when the
deployed app/ig.py predates them. Falls through to passwords on any
failure. No secrets logged.
"""
import asyncio
import inspect
import logging
import os
import random
from typing import Any

log = logging.getLogger("instaward-bot")


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


def _jitter_bounds() -> tuple[float, float]:
    try:
        from app import config as cfg

        lo = float(getattr(cfg, "IG_JITTER_MIN", 1.0))
        hi = float(getattr(cfg, "IG_JITTER_MAX", 3.0))
    except Exception:  # noqa: BLE001
        try:
            lo = float(os.getenv("IG_JITTER_MIN", "1.0") or 1.0)
            hi = float(os.getenv("IG_JITTER_MAX", "3.0") or 3.0)
        except ValueError:
            lo, hi = 1.0, 3.0
    if hi < lo:
        lo, hi = hi, lo
    return max(0.0, lo), max(0.0, hi)


async def jitter_delay() -> None:
    """Human-like pause before write calls / between paginated reads."""
    lo, hi = _jitter_bounds()
    if hi <= 0:
        return
    await asyncio.sleep(random.uniform(lo, hi))


def is_auth_error(exc: BaseException) -> bool:
    """True for auth/challenge-type failures (session must be invalidated)."""
    blob = f"{type(exc).__name__} {exc}".lower()
    keys = (
        "challenge", "checkpoint", "two_step", "two-step", "two step",
        "login_required", "login required", "unauthorized", "unauthorised",
        "401", "invalid session", "session expired", "sessionid",
        "consent_required", "feedback_required",
    )
    return any(k in blob for k in keys)


def mark_session_stale() -> None:
    """Compat no-op when app.ig predates TTL cache; real cache clears itself."""
    try:
        from app import ig as igmod

        fn = getattr(igmod, "mark_session_stale", None)
        if callable(fn) and getattr(fn, "_compat_shim", False) is not True:
            fn()
    except Exception:  # noqa: BLE001
        pass


mark_session_stale._compat_shim = True  # type: ignore[attr-defined]


async def _verify_session(cl: Any) -> None:
    """Light read to prove sessionid cookies work. Raises on failure."""
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


def _clean_cookie(raw: str) -> str:
    if not raw:
        return ""
    from urllib.parse import unquote

    s = str(raw).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return unquote(s).strip().strip("'\" ")


def _live_cookie(*names: str) -> str:
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return _clean_cookie(str(v))
    return ""


async def _try_sessionid_login(cl: Any, dbmod: Any) -> Any | None:
    """Attempt sessionid login. Returns client on success, None to fall through."""
    sessionid = _live_cookie("INSTAGRAM_SESSIONID", "SESSION_ID", "INSTAGRAM_SESSION")
    if not sessionid:
        return None
    csrf = _live_cookie("INSTAGRAM_CSRFTOKEN", "CSRF_TOKEN")
    ds_user_id = _live_cookie("INSTAGRAM_DS_USER_ID", "DS_USER_ID")
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
        _inject_instagram_cookies(cl, sessionid, csrf, ds_user_id)
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


def _install_compat_helpers() -> None:
    """Backfill newer app.ig helpers when deployed ig.py predates them."""
    try:
        from app import ig as igmod
    except Exception as exc:  # noqa: BLE001
        log.warning("compat shim skipped (import): %s", exc)
        return
    for name, fn in (
        ("jitter_delay", jitter_delay),
        ("is_auth_error", is_auth_error),
        ("mark_session_stale", mark_session_stale),
    ):
        try:
            if not hasattr(igmod, name):
                setattr(igmod, name, fn)
        except Exception:  # noqa: BLE001
            continue


def install_sessionid_first() -> None:
    """Wrap app.ig.ensure_login to try session cookies before passwords."""
    _install_compat_helpers()
    try:
        from app import db as dbmod
        from app import ig as igmod
    except Exception as exc:  # noqa: BLE001 - never break import
        log.warning("sessionid patch skipped (import): %s", exc)
        return
    if getattr(igmod.ensure_login, "_sessionid_patched", False):
        return
    orig = igmod.ensure_login

    async def wrapped() -> Any:
        try:
            cl = igmod.get_client()
            async with igmod.get_lock():
                hit = await _try_sessionid_login(cl, dbmod)
            if hit is not None:
                return hit
        except Exception as exc:  # noqa: BLE001 - fall through to passwords
            log.warning("sessionid attempt errored: %s", exc)
        return await orig()

    wrapped._sessionid_patched = True  # type: ignore[attr-defined]
    igmod.ensure_login = wrapped  # type: ignore[assignment]
    log.info("sessionid-first login patch installed")
