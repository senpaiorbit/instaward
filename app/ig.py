"""Async Instagram client (aiograpi). Import-safe: no login at import.

Archive policy: real `media_archive` + DB flags; fallback to `media_delete`
only if archive raises, plus local-only mark. See README.
"""
import asyncio
import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

log = logging.getLogger("instaward-bot")

_client: Any = None
_lock = asyncio.Lock()
# In-process session reuse: monotonic timestamp + method of last successful
# login. Avoids re-login (the most suspicious IG call) on every request.
_authed_at: float | None = None
_auth_method: str | None = None


def get_lock() -> asyncio.Lock:
    return _lock


def get_client() -> Any:
    """Singleton aiograpi Client with conservative delays. No I/O."""
    global _client
    if _client is None:
        from aiograpi import Client

        try:
            from app import config as cfg

            lo = float(getattr(cfg, "IG_JITTER_MIN", 1.0))
            hi = float(getattr(cfg, "IG_JITTER_MAX", 3.0))
            delay = [lo, hi] if hi >= lo and lo >= 0 else [1, 3]
        except Exception:  # noqa: BLE001
            delay = [1, 3]
        _client = Client(delay_range=delay)
        # Checkpoint handler: Telegram notify is done in ensure_login's
        # ChallengeRequired catch; handler here just raises to surface it.
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


def _session_ttl_sec() -> float:
    try:
        from app import config as cfg

        return max(0.0, float(getattr(cfg, "SESSION_REUSE_TTL_MIN", 120) or 0)) * 60.0
    except Exception:  # noqa: BLE001
        return 120.0 * 60.0


def _is_fresh() -> bool:
    if _client is None or _authed_at is None:
        return False
    ttl = _session_ttl_sec()
    if ttl <= 0:
        return False
    return (time.monotonic() - _authed_at) < ttl


def _mark_authed(method: str) -> None:
    global _authed_at, _auth_method
    _authed_at = time.monotonic()
    _auth_method = method


def mark_session_stale() -> None:
    """Invalidate the cached session so the next call re-logs in."""
    global _authed_at, _auth_method
    _authed_at = None
    _auth_method = None


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


def _is_rate_limited(exc: BaseException) -> bool:
    blob = f"{type(exc).__name__} {exc}".lower()
    keys = (
        "429", "rate limit", "ratelimit", "rate_limit", "too many requests",
        "slow down", "slowdown", "throttl", "please wait", "try again later",
    )
    return any(k in blob for k in keys)


def _is_two_factor_error(exc: BaseException) -> bool:
    """True when a plain password login stopped at two-factor verification."""
    blob = f"{type(exc).__name__} {exc}".lower()
    if "two" in blob and ("factor" in blob or "step" in blob):
        return True
    if "verification" in blob:
        return True
    if "login_required" in blob or "login required" in blob:
        return True
    return False


async def jitter_delay() -> None:
    """Human-like pause before write calls / between paginated reads."""
    try:
        from app import config as cfg

        lo = float(getattr(cfg, "IG_JITTER_MIN", 1.0))
        hi = float(getattr(cfg, "IG_JITTER_MAX", 3.0))
    except Exception:  # noqa: BLE001
        lo, hi = 1.0, 3.0
    if hi < lo:
        lo, hi = hi, lo
    if hi <= 0:
        return
    await asyncio.sleep(random.uniform(max(0.0, lo), hi))


async def read_with_backoff(label: str, fn: Callable, *args: Any, **kwargs: Any) -> Any:
    """Run a READ-ONLY IG call with exponential backoff on 429/rate-limit.

    Up to 3 attempts total for rate-limits only. Auth/challenge errors
    invalidate the cached session and raise immediately. NEVER use for
    clip_upload / media_archive (writes get a single attempt).
    """
    import inspect

    delay = 2.0
    for attempt in range(1, 4):
        try:
            res = fn(*args, **kwargs)
            if inspect.isawaitable(res):
                res = await res
            return res
        except Exception as exc:  # noqa: BLE001
            if is_auth_error(exc):
                mark_session_stale()
                raise
            if _is_rate_limited(exc) and attempt < 3:
                log.warning("%s rate-limited (try %d/3), backing off %.0fs", label, attempt, delay)
                await asyncio.sleep(delay)
                delay *= 2.0
                continue
            raise


async def _verify_session(cl: Any) -> None:
    """Light read to prove sessionid cookies work. Raises on failure."""
    import inspect

    username = os.getenv("INSTAGRAM_USERNAME", "")
    errors: list[str] = []
    # 1) Timeline feed (needs no username).
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
    # 2) Username lookup (needs INSTAGRAM_USERNAME).
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
    raise RuntimeError(f"session verify failed ({'; '.join(errors) or 'no probe available'})")


def _inject_instagram_cookies(cl: Any, sessionid: str, csrf: str, ds_user_id: str) -> None:
    """Manually inject session cookies for .instagram.com (defensive).

    Tries every cookie-jar API the client may expose. Raises RuntimeError if
    no supported jar is found. Only called at runtime, never at import.
    """
    cookies = {"sessionid": sessionid}
    if csrf:
        cookies["csrftoken"] = csrf
    if ds_user_id:
        cookies["ds_user_id"] = ds_user_id
    injected = False

    # 1) Plain-dict style jar (instagrapi exposes `cookie_dict`).
    try:
        jar = getattr(cl, "cookie_dict", None)
        if isinstance(jar, dict):
            jar.update(cookies)
            injected = True
    except Exception:  # noqa: BLE001
        pass

    # 2) Jar objects with .set(name, value, domain=...) on client/private/session.
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
        # 3) Client-level set_cookie helper if present.
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
    """Attempt sessionid login BEFORE the password path.

    Returns the client on success, None to fall through to passwords.
    Never raises: failures log a warning and fall through.
    """
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

    # a) Native sessionid login when the client supports it.
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
            _mark_authed("sessionid")
            return cl
        except Exception as exc:  # noqa: BLE001
            log.warning("login_by_sessionid failed, trying cookie inject: %s", type(exc).__name__)
    # b) Manual cookie injection + light-read verify.
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
        _mark_authed("cookie")
        return cl
    except Exception as exc:  # noqa: BLE001
        log.warning("sessionid login failed, falling through to password: %s", type(exc).__name__)
        return None


async def ensure_login() -> Any:
    """Login via sessionid cookies first (bypasses 2FA), else username/password.

    Sessionid path runs BEFORE the password path. On sessionid failure it
    falls through to the existing password flow (2FA/notify unchanged). On
    success, settings are dumped and persisted to Turso session_cache.
    Order for password path: set_settings(dict) BEFORE login. Serialized
    with the global lock. Notifies Telegram on ChallengeRequired.
    """
    from app import db as dbmod

    async with _lock:
        cl = get_client()
        # 0a) Reuse the in-process session while fresh (no login call at all).
        if _is_fresh():
            return cl
        # 0b) Sessionid login (bypasses 2FA when 2FA blocks passwords).
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
        # 1) Turso-cached session
        try:
            cached = await dbmod.load_session("ig_session")
            if cached:
                settings = json.loads(cached) if isinstance(cached, str) else cached
        except Exception as exc:  # noqa: BLE001
            log.warning("load_session failed: %s", exc)
        # 2) Env-provided session state overrides/complements
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
            if not _is_two_factor_error(exc):
                if is_auth_error(exc):
                    mark_session_stale()
                name = type(exc).__name__
                if "Challenge" in name or "challenge" in str(exc).lower():
                    try:
                        from app import telegramlog as tg

                        await tg.notify_checkpoint(username)
                    except Exception:  # noqa: BLE001
                        pass
                raise
            # --- 2FA: exactly ONE verification attempt (no code-retry loops).
            import inspect

            from app import config as cfg

            def _live2(name: str) -> str:
                v = os.getenv(name)
                if v is not None and str(v).strip() != "":
                    return str(v)
                return ""

            seed = getattr(cfg, "INSTAGRAM_TOTP_SEED", "") or _live2("INSTAGRAM_TOTP_SEED")
            one_shot = getattr(cfg, "INSTAGRAM_2FA_CODE", "") or _live2("INSTAGRAM_2FA_CODE")
            seed = str(seed or "").strip().strip("'\" ").replace(" ", "")
            one_shot = str(one_shot or "").strip().strip("'\" ")
            code = ""
            source = "none"
            if seed:
                totp_fn = getattr(cl, "totp_generate_code", None)
                if callable(totp_fn):
                    try:
                        # Sync call immediately before use (fresh 30s window).
                        gen = totp_fn(seed)
                        if inspect.isawaitable(gen):
                            gen = await gen
                        code = str(gen or "").strip()
                        source = "totp" if code else "none"
                    except Exception as gexc:  # noqa: BLE001
                        log.warning("totp code generation failed: %s", type(gexc).__name__)
            if not code and one_shot:
                code = one_shot
                source = "manual"
            if not code:
                log.warning("2FA required but no code source configured (source=none)")
                if is_auth_error(exc):
                    mark_session_stale()
                name = type(exc).__name__
                if "Challenge" in name or "challenge" in str(exc).lower():
                    try:
                        from app import telegramlog as tg

                        await tg.notify_checkpoint(username)
                    except Exception:  # noqa: BLE001
                        pass
                raise
            log.info("2FA verification required; single verification attempt (source=%s)", source)
            try:
                await cl.login(username, password, verification_code=code)
            except Exception as exc2:  # noqa: BLE001
                if is_auth_error(exc2):
                    mark_session_stale()
                name2 = type(exc2).__name__
                if "Challenge" in name2 or "challenge" in str(exc2).lower():
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
            except Exception as save_exc:  # noqa: BLE001
                log.warning("save_session after 2FA login failed: %s", save_exc)
            _mark_authed("password-2fa")
            return cl
        try:
            dumped = _dump_settings(cl)
            if dumped:
                await dbmod.save_session("ig_session", dumped)
        except Exception as exc:  # noqa: BLE001
            log.warning("save_session failed: %s", exc)
        _mark_authed("password")
        return cl


async def fetch_candidates(n: int = 30) -> list[Any]:
    """Merge timeline feed + reels + explore reels. Serialized with lock.

    Call-volume discipline: stops as soon as n candidates are held, caps
    timeline pagination, and spaces paginated calls with jittered sleeps.
    Reads retry with backoff on 429 only; auth errors invalidate the session.
    """
    cl = get_client()
    try:
        n = max(1, int(n))
    except (TypeError, ValueError):
        n = 30
    out: list[Any] = []
    seen: set[str] = set()

    def _add(items: Any) -> None:
        if not items or len(out) >= n:
            return
        medias = items if isinstance(items, list) else getattr(items, "medias", items)
        try:
            iterable = list(medias)  # type: ignore[arg-type]
        except TypeError:
            return
        for m in iterable:
            code = str(getattr(m, "code", getattr(m, "pk", id(m))))
            if code not in seen:
                seen.add(code)
                out.append(m)
            if len(out) >= n:
                break

    async with _lock:
        # Timeline with pagination via next_max_id (bounded page count).
        try:
            page = await read_with_backoff("timeline", cl.get_timeline_feed, "cold_start_fetch")
            _add(getattr(page, "medias", page) if not isinstance(page, dict) else page.get("medias"))
            next_max_id = page.get("next_max_id") if isinstance(page, dict) else getattr(page, "next_max_id", None)
            pages = 1
            while len(out) < n and next_max_id and pages < 5:
                await jitter_delay()
                page = await read_with_backoff(
                    "timeline", cl.get_timeline_feed, "cold_start_fetch", max_id=next_max_id
                )
                if isinstance(page, dict):
                    _add(page.get("medias"))
                    next_max_id = page.get("next_max_id")
                else:
                    _add(getattr(page, "medias", None))
                    next_max_id = getattr(page, "next_max_id", None)
                pages += 1
        except Exception as exc:  # noqa: BLE001
            log.warning("timeline fetch failed: %s: %s", type(exc).__name__, exc)
        # Reels + explore reels (only while still short of candidates).
        for fn_name in ("reels", "explore_reels"):
            if len(out) >= n:
                break
            try:
                fn = getattr(cl, fn_name, None)
                if fn is None:
                    continue
                await jitter_delay()
                items = await read_with_backoff(fn_name, fn, amount=n)
                _add(items)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s failed: %s: %s", fn_name, type(exc).__name__, exc)
    return out[:n]


def pick_random_unprocessed(candidates: list[Any], processed_codes: set[str]) -> Any | None:
    import random

    fresh = [m for m in candidates if str(getattr(m, "code", getattr(m, "pk", ""))) not in processed_codes]
    if not fresh:
        return None
    return random.choice(fresh)


def get_best_video_url(media: Any) -> Optional[str]:
    for attr in ("video_url",):
        v = getattr(media, attr, None)
        if v:
            return str(v)
    # Fallbacks: video_versions / image_versions2
    for attr in ("video_versions",):
        try:
            vers = getattr(media, attr, None)
            if vers:
                first = vers[0] if isinstance(vers, list) else vers
                url = getattr(first, "url", first.get("url") if isinstance(first, dict) else None)
                if url:
                    return str(url)
        except Exception:  # noqa: BLE001
            pass
    thumb = getattr(media, "thumbnail_url", None)
    return str(thumb) if thumb else None


async def download_to_tmp(url: str, prefix: str = "vid") -> Path:
    """Stream-download URL to /tmp (ephemeral only). Returns local Path."""
    dest_dir = Path("/tmp/videos")
    dest_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(url.encode()).hexdigest()[:16]
    dest = dest_dir / f"{prefix}_{digest}.mp4"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            with open(dest, "wb") as fh:
                async for chunk in resp.aiter_bytes(1024 * 64):
                    fh.write(chunk)
    return dest


async def cache_thumbnail(url: str | None, code: str) -> Optional[Path]:
    """Download thumbnail URL and convert to JPEG at /tmp/thumbs/{code}.jpg.

    Accepts direct .jpg/.jpeg/.png/.webp URLs. Returns Path or None.
    """
    if not url:
        return None
    dest_dir = Path("/tmp/thumbs")
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / f"{code}.jpg"
    if dest.exists() and dest.stat().st_size > 0:
        return dest
    tmp_raw = dest_dir / f"{code}.raw"
    try:
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            tmp_raw.write_bytes(resp.content)
        from PIL import Image

        with Image.open(tmp_raw) as im:
            rgb = im.convert("RGB")
            rgb.save(dest, "JPEG", quality=88)
        return dest
    except Exception as exc:  # noqa: BLE001
        log.warning("thumbnail cache failed for %s: %s", code, exc)
        return None
    finally:
        try:
            if tmp_raw.exists():
                tmp_raw.unlink()
        except OSError:
            pass