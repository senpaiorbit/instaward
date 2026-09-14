"""Sessionid login helpers (2FA bypass) + runtime compat shims. Import-safe.

Call install_sessionid_first() once at startup (see app/main.py). It wraps
app.ig.ensure_login with: (1) raw session cookies tried first, (2) 2FA-aware
password login (TOTP seed or one-shot code) when cookies are absent/expired,
(3) backfilled newer helpers (jitter_delay / is_auth_error /
mark_session_stale) when the deployed app/ig.py predates them. Falls through
to the original password flow when nothing is configured. No secrets logged.
"""
import asyncio
import inspect
import json
import logging
import os
import random
from typing import Any, Optional

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


def _is_two_factor_error(exc: BaseException) -> bool:
    blob = f"{type(exc).__name__} {exc}".lower()
    if "two" in blob and ("factor" in blob or "step" in blob):
        return True
    return "verification" in blob or "login_required" in blob


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


def _clean_secret(raw: str) -> str:
    return _clean_cookie(raw).replace(" ", "")


def _resolve_2fa_code(cl: Any) -> tuple[str, str]:
    """Return (code, source). Source is totp/manual/none. Secrets never logged."""
    seed = _clean_secret(_live_cookie("INSTAGRAM_TOTP_SEED"))
    one_shot = _clean_cookie(_live_cookie("INSTAGRAM_2FA_CODE"))
    if seed:
        totp_fn = getattr(cl, "totp_generate_code", None)
        if callable(totp_fn):
            try:
                gen = totp_fn(seed)
                code = str(gen or "").strip()
                if code:
                    return code, "totp"
            except Exception as gexc:  # noqa: BLE001
                log.warning("totp code generation failed: %s", type(gexc).__name__)
    if one_shot:
        return one_shot, "manual"
    return "", "none"


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


async def _password_login_with_2fa(cl: Any, dbmod: Any) -> Any | None:
    """Username/password login with single 2FA attempt. None when unconfigured."""
    username = os.getenv("INSTAGRAM_USERNAME", "")
    password = os.getenv("INSTAGRAM_PASSWORD", "")
    if not username or not password:
        return None
    try:
        cached = await dbmod.load_session("ig_session")
        settings: Optional[dict[str, Any]] = None
        if cached:
            try:
                settings = json.loads(cached) if isinstance(cached, str) else cached
            except Exception:  # noqa: BLE001
                settings = None
        if settings:
            try:
                cl.set_settings(settings)
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        log.warning("load_session failed: %s", exc)
    try:
        await cl.login(username, password)
        _persist_session(dbmod, cl)
        return cl
    except Exception as exc:  # noqa: BLE001
        if not _is_two_factor_error(exc):
            raise
    code, source = _resolve_2fa_code(cl)
    if not code:
        log.warning("2FA required but no code source configured (source=none)")
        return None
    log.info("2FA verification required; single verification attempt (source=%s)", source)
    await cl.login(username, password, verification_code=code)
    _persist_session(dbmod, cl)
    log.info("instagram password+2fa login succeeded (source=%s)", source)
    return cl


def _persist_session(dbmod: Any, cl: Any) -> None:
    import asyncio as _asyncio

    async def _save() -> None:
        try:
            dumped = _dump_settings(cl)
            if dumped:
                await dbmod.save_session("ig_session", dumped)
        except Exception as exc:  # noqa: BLE001
            log.warning("save_session failed: %s", exc)

    try:
        loop = _asyncio.get_running_loop()
        loop.create_task(_save())
    except RuntimeError:
        pass


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
    """Wrap app.ig.ensure_login: cookies, then 2FA password, then original."""
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
        try:
            cl = igmod.get_client()
            async with igmod.get_lock():
                hit2 = await _password_login_with_2fa(cl, dbmod)
            if hit2 is not None:
                return hit2
        except Exception as exc:  # noqa: BLE001 - fall through to original
            log.warning("2fa password attempt errored: %s", type(exc).__name__)
        return await orig()

    wrapped._sessionid_patched = True  # type: ignore[attr-defined]
    igmod.ensure_login = wrapped  # type: ignore[assignment]
    log.info("sessionid-first login patch installed")
