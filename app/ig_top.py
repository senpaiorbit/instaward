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
    import inspect

    delay = 2.0
    for attempt in range(1, 4):
        try:
            res = fn(*args, **kwargs)
            if inspect.isawaitable(res):
                await res
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
