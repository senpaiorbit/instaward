"""Environment config parsing (no pydantic). Import-safe: no network calls.

Canonical names are authoritative; legacy aliases are fallbacks only.
No secret values are ever logged, printed, or persisted here.
"""
import logging
import os
import re

from dotenv import load_dotenv

load_dotenv()


def _get(name: str, default: str = "") -> str:
    v = os.getenv(name)
    if v is None or str(v).strip() == "":
        return default
    return v


def _get_first(*names: str, default: str = "") -> str:
    """Return first non-empty env var among names (canonical first)."""
    for n in names:
        v = os.getenv(n)
        if v is not None and str(v).strip() != "":
            return str(v)
    return default


def parse_duration_to_seconds(raw: str, default_seconds: int = 24 * 3600) -> int:
    """Parse '12h', '90m', '2d', '3600', '3600s' -> seconds."""
    if raw is None or str(raw).strip() == "":
        return default_seconds
    s = str(raw).strip().lower()
    if s.isdigit():
        return int(s)
    m = re.fullmatch(r"(\d+)\s*([smhd])", s)
    if m:
        n = int(m.group(1))
        unit = m.group(2)
        mult = {"s": 1, "m": 60, "h": 3600, "d": 86400}[unit]
        return n * mult
    raise ValueError(f"Invalid duration: {raw!r} (expected like '12h', '90m', '3600s')")


def _parse_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ValueError(f"Invalid int for {name}")


def _parse_int_raw(raw: str, default: int, label: str) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        raise ValueError(f"Invalid int for {label}")


def _parse_jitter(raw: str, default: tuple[float, float] = (1.0, 3.0)) -> tuple[float, float]:
    """Parse 'min,max' seconds (e.g. '1,3') -> (lo, hi) floats."""
    if raw is None or str(raw).strip() == "":
        return default
    s = str(raw).strip().replace(";", ",")
    parts = [p for p in s.replace(" ", "").split(",") if p != ""]
    try:
        if len(parts) == 1:
            v = float(parts[0])
            if v < 0:
                return default
            return (v, v)
        lo, hi = float(parts[0]), float(parts[1])
        if lo < 0 or hi < 0:
            return default
        return (min(lo, hi), max(lo, hi))
    except ValueError:
        raise ValueError("Invalid jitter range (expected 'min,max' seconds)")


def _parse_bool_like(raw: str, default: int) -> int:
    if raw is None or str(raw).strip() == "":
        return default
    s = str(raw).strip().lower()
    if s in ("1", "true", "yes", "y", "on"):
        return 1
    if s in ("0", "false", "no", "n", "off"):
        return 0
    try:
        return 1 if int(s) != 0 else 0
    except ValueError:
        raise ValueError("Invalid boolean flag")


# --- Auth key: ENV_KEY (alias: UPLOAD_SECRET) ---
ENV_KEY: str = _get_first("ENV_KEY", "UPLOAD_SECRET", default="")

# --- Turso: TURSO_URL (alias: TURSO_DATABASE_URL) ---
TURSO_URL: str = _get_first("TURSO_URL", "TURSO_DATABASE_URL", default="")
TURSO_AUTH_TOKEN: str = _get("TURSO_AUTH_TOKEN", "")

# --- Instagram login ---
INSTAGRAM_USERNAME: str = _get("INSTAGRAM_USERNAME", "")
INSTAGRAM_PASSWORD: str = _get("INSTAGRAM_PASSWORD", "")
INSTAGRAM_SESSION_STATE: str = _get("INSTAGRAM_SESSION_STATE", "")


def _clean_session_cookie(raw: str) -> str:
    """URL-decode a raw cookie value; strip whitespace/quotes. No logging."""
    if not raw:
        return ""
    from urllib.parse import unquote

    s = str(raw).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return unquote(s).strip().strip("'\" ")


# Raw session cookies for sessionid login (bypasses 2FA). These are browser
# cookies, NOT aiograpi settings JSON (INSTAGRAM_SESSION_STATE). Cookies
# expire; the username/password path remains as fallback.
INSTAGRAM_SESSIONID: str = _clean_session_cookie(
    _get_first("INSTAGRAM_SESSIONID", "SESSION_ID", "INSTAGRAM_SESSION", default="")
)
INSTAGRAM_CSRFTOKEN: str = _clean_session_cookie(
    _get_first("INSTAGRAM_CSRFTOKEN", "CSRF_TOKEN", default="")
)
INSTAGRAM_DS_USER_ID: str = _clean_session_cookie(
    _get_first("INSTAGRAM_DS_USER_ID", "DS_USER_ID", default="")
)
# Documented unused legacy field: DESTINATION_USERNAME (no effect).

# 2FA for username/password login on protected accounts. Values are never
# logged. TOTP seed is the reusable base32 manual-entry key; the one-shot
# code is a user-supplied 6-digit TOTP or 8-digit backup code.


def _clean_secret(raw: str) -> str:
    """Strip whitespace/quotes from a secret value. Never logged."""
    if not raw:
        return ""
    s = str(raw).strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    return s.strip().strip("'\" ")


INSTAGRAM_TOTP_SEED: str = _clean_secret(_get("INSTAGRAM_TOTP_SEED", "")).replace(" ", "")
INSTAGRAM_2FA_CODE: str = _clean_secret(_get("INSTAGRAM_2FA_CODE", ""))

TELEGRAM_BOT_TOKEN: str = _get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID: str = _get("TELEGRAM_CHAT_ID", "")

ARCHIVE_TIME_RAW: str = _get("ARCHIVE_TIME", "24h")
ARCHIVE_TIME_SEC: int = parse_duration_to_seconds(ARCHIVE_TIME_RAW, 24 * 3600)
ARCHIVE_VIEWS: int = _parse_int("ARCHIVE_VIEWS", 1000)
# DB-limited archive scan size (keeps the free-plan job small).
ARCHIVE_BATCH: int = _parse_int("ARCHIVE_BATCH", 50)

# --- Upload quality gate (resolution + size; Pillow/MP4 probing, no ffmpeg needed) ---
QUALITY_ENABLED: int = _parse_int("QUALITY_ENABLED", 1)
QUALITY_MIN_BYTES: int = _parse_int("QUALITY_MIN_BYTES", 300000)
QUALITY_MIN_WIDTH: int = _parse_int("QUALITY_MIN_WIDTH", 720)
QUALITY_MIN_HEIGHT: int = _parse_int("QUALITY_MIN_HEIGHT", 960)
QUALITY_STRICT: int = _parse_int("QUALITY_STRICT", 0)

# --- Auto-comment on own just-published repost (off by default; &comment=1) ---
COMMENT_ENABLED: int = _parse_int("COMMENT_ENABLED", 0)
COMMENT_TEXT: str = _get("COMMENT_TEXT", "FOLLOW ME \U0001F525|FOLLOW FOR MORE \U0001F525|FOLLOW ME ❤️")

# In-process authenticated-client reuse (biggest IG-call saver on free plan).
SESSION_REUSE_TTL_MIN: int = _parse_int("SESSION_REUSE_TTL_MIN", 120)

# Jittered human-like delay before write calls (clip_upload/media_archive)
# and between paginated reads. Format "min,max" seconds.
IG_CALL_JITTER_SEC_RAW: str = _get("IG_CALL_JITTER_SEC", "1,3")
IG_JITTER_MIN, IG_JITTER_MAX = _parse_jitter(IG_CALL_JITTER_SEC_RAW, (1.0, 3.0))

# HIDELIKE canonical int flag; alias HIDE_LIKE_VIEW_COUNTS accepts
# true/false/1/0 (alias only used when HIDELIKE is empty).
_HIDELIKE_RAW: str = _get_first("HIDELIKE", "HIDE_LIKE_VIEW_COUNTS", default="1")
try:
    HIDELIKE: int = int(_HIDELIKE_RAW.strip())
    HIDELIKE = 1 if HIDELIKE != 0 else 0
except ValueError:
    HIDELIKE = _parse_bool_like(_HIDELIKE_RAW, 1)

BOTLOG: int = _parse_int("BOTLOG", 1)

# --- Thumbnails / covers ---
# Canonical THUMBNAIL_URL; when empty, COVER_FILE (single image) or COVER_DIR
# (directory of images, COVER_MODE=random/static) act as local fallback.
COVER_MODE: str = _get("COVER_MODE", "static").strip().lower() or "static"
if COVER_MODE not in ("random", "static"):
    COVER_MODE = "static"
COVER_DIR: str = _get("COVER_DIR", "")
COVER_FILE: str = _get("COVER_FILE", "")
_THUMB_CANONICAL: str = _get("THUMBNAIL_URL", "")
if _THUMB_CANONICAL:
    THUMBNAIL_URL: str = _THUMB_CANONICAL
elif COVER_FILE:
    THUMBNAIL_URL = COVER_FILE
elif COVER_DIR:
    THUMBNAIL_URL = COVER_DIR
else:
    THUMBNAIL_URL = ""

# --- Fetch / pacing ---
FETCH_COUNT: int = _parse_int_raw(
    _get_first("FETCH_COUNT", "REEL_FETCH_COUNT", default="30"), 30, "FETCH_COUNT"
)

# Conservative pacing for personal use.
MIN_POST_INTERVAL_MIN: int = _parse_int("MIN_POST_INTERVAL_MIN", 30)
MAX_PER_DAY: int = _parse_int("MAX_PER_DAY", 10)
# Legacy per-run cap; clamped so it never exceeds the daily cap.
_MAX_UPLOADS_ALIAS_RAW: str = _get("MAX_UPLOADS_PER_RUN", "")
if _MAX_UPLOADS_ALIAS_RAW:
    _alias_cap = _parse_int_raw(_MAX_UPLOADS_ALIAS_RAW, MAX_PER_DAY, "MAX_UPLOADS_PER_RUN")
    MAX_UPLOADS_PER_RUN: int = min(_alias_cap, MAX_PER_DAY)
else:
    MAX_UPLOADS_PER_RUN = MAX_PER_DAY

# --- Logging ---
LOG_LEVEL: str = _get_first("LOG_LEVEL", default="INFO").strip().upper() or "INFO"
try:
    _level = getattr(logging, LOG_LEVEL, logging.INFO)
    logging.getLogger("instaward-bot").setLevel(_level)
except Exception:  # noqa: BLE001 - never fail import on bad level
    pass
