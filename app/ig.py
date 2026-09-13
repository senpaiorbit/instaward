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


async def ensure_login() -> Any:
    """Login with saved session state if available, else username/password.

    Order: set_settings(dict) BEFORE login. On success dump settings and
    persist to Turso session_cache + log. Serialized with the global lock.
    Notifies Telegram on ChallengeRequired.
    """
    from app import db as dbmod

    username = os.getenv("INSTAGRAM_USERNAME", "")
    password = os.getenv("INSTAGRAM_PASSWORD", "")
    if not username or not password:
        raise RuntimeError("INSTAGRAM_USERNAME/INSTAGRAM_PASSWORD are not set")

    async with _lock:
        cl = get_client()
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


async def fetch_candidates(n: int = 30) -> list[Any]:
    """Merge timeline feed + reels + explore reels. Serialized with lock."""
    cl = get_client()
    out: list[Any] = []
    seen: set[str] = set()

    def _add(items: Any) -> None:
        if not items:
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
        try:
            page = await cl.get_timeline_feed("cold_start_fetch")
            _add(getattr(page, "medias", page) if not isinstance(page, dict) else page.get("medias"))
            next_max_id = page.get("next_max_id") if isinstance(page, dict) else getattr(page, "next_max_id", None)
            while len(out) < n and next_max_id:
                await asyncio.sleep(1)
                page = await cl.get_timeline_feed("cold_start_fetch", max_id=next_max_id)
                if isinstance(page, dict):
                    _add(page.get("medias"))
                    next_max_id = page.get("next_max_id")
                else:
                    _add(getattr(page, "medias", None))
                    next_max_id = getattr(page, "next_max_id", None)
        except Exception as exc:  # noqa: BLE001
            log.warning("timeline fetch failed: %s", exc)
        for fn_name in ("reels", "explore_reels"):
            if len(out) >= n:
                break
            try:
                fn = getattr(cl, fn_name, None)
                if fn is None:
                    continue
                await asyncio.sleep(1)
                items = await fn(amount=n)
                _add(items)
            except Exception as exc:  # noqa: BLE001
                log.warning("%s failed: %s", fn_name, exc)
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
