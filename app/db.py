"""Turso (libsql) storage. Sync client wrapped via asyncio.to_thread. Import-safe."""
import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

log = logging.getLogger("instaward-bot")

_SCHEMA_PATH = Path(__file__).resolve().parent.parent / "schema.sql"
_FALLBACK_SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_media (
  media_code TEXT PRIMARY KEY,
  author_username TEXT NOT NULL,
  original_url TEXT,
  cached_download_url TEXT,
  published_at DATETIME DEFAULT CURRENT_TIMESTAMP,
  archived INTEGER DEFAULT 0,
  archive_scanned INTEGER DEFAULT 0,
  repost_code TEXT,
  repost_pk TEXT
);
CREATE TABLE IF NOT EXISTS session_cache (
  key TEXT PRIMARY KEY,
  state_json TEXT NOT NULL,
  updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);
"""


def _load_schema_sql() -> str:
    try:
        if _SCHEMA_PATH.exists():
            return _SCHEMA_PATH.read_text(encoding="utf-8")
    except OSError:
        pass
    return _FALLBACK_SCHEMA


def _connect():
    import libsql

    url = os.environ.get("TURSO_URL", "")
    token = os.environ.get("TURSO_AUTH_TOKEN", "")
    if not url:
        raise RuntimeError("TURSO_URL is not set")
    kwargs: dict[str, Any] = {"database": url}
    if token:
        kwargs["auth_token"] = token
    return libsql.connect(**kwargs)


def _init_db_sync() -> None:
    con = _connect()
    try:
        con.executescript(_load_schema_sql())
        con.commit()
        for _ddl in (
            "ALTER TABLE processed_media ADD COLUMN repost_code TEXT",
            "ALTER TABLE processed_media ADD COLUMN repost_pk TEXT",
        ):
            try:
                con.execute(_ddl)
                con.commit()
            except Exception as exc:  # noqa: BLE001
                if "duplicate" in str(exc).lower():
                    continue
                log.warning("migration %s failed: %s", _ddl, exc)
    finally:
        con.close()


def _insert_processed_sync(code: str, author: str, original_url: str = "", cached_url: str = "") -> None:
    con = _connect()
    try:
        con.execute(
            "INSERT OR IGNORE INTO processed_media"
            "(media_code, author_username, original_url, cached_download_url) VALUES (?,?,?,?)",
            (code, author, original_url or "", cached_url or ""),
        )
        con.commit()
    finally:
        con.close()


def _get_processed_codes_sync() -> set[str]:
    con = _connect()
    try:
        rows = con.execute("SELECT media_code FROM processed_media").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def _get_processed_authors_sync() -> set[str]:
    con = _connect()
    try:
        rows = con.execute("SELECT DISTINCT author_username FROM processed_media").fetchall()
        return {r[0] for r in rows}
    finally:
        con.close()


def _get_recent_sync(limit: int = 5) -> list[dict[str, Any]]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT media_code, author_username, original_url, published_at, archived, archive_scanned,"
            " repost_code, repost_pk"
            " FROM processed_media ORDER BY published_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "media_code": r[0],
                "author_username": r[1],
                "original_url": r[2],
                "published_at": r[3],
                "archived": r[4],
                "archive_scanned": r[5],
                "repost_code": r[6] if len(r) > 6 else None,
                "repost_pk": r[7] if len(r) > 7 else None,
            }
            for r in rows
        ]
    finally:
        con.close()


def _get_archive_candidates_sync(limit: int = 50) -> list[dict[str, Any]]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT media_code, author_username, original_url, published_at, archived, archive_scanned,"
            " repost_code, repost_pk"
            " FROM processed_media WHERE archived = 0 AND archive_scanned = 0"
            " ORDER BY published_at ASC LIMIT ?",
            (limit,),
        ).fetchall()
        return [
            {
                "media_code": r[0],
                "author_username": r[1],
                "original_url": r[2],
                "published_at": r[3],
                "archived": r[4],
                "archive_scanned": r[5],
                "repost_code": r[6] if len(r) > 6 else None,
                "repost_pk": r[7] if len(r) > 7 else None,
            }
            for r in rows
        ]
    finally:
        con.close()


def _mark_archived_sync(code: str, archived: int = 1) -> None:
    con = _connect()
    try:
        con.execute(
            "UPDATE processed_media SET archived = ?, archive_scanned = 1 WHERE media_code = ?",
            (1 if archived else 0, code),
        )
        con.commit()
    finally:
        con.close()


def _mark_scanned_sync(code: str, archived: int = 0) -> None:
    con = _connect()
    try:
        con.execute(
            "UPDATE processed_media SET archived = ?, archive_scanned = 1 WHERE media_code = ?",
            (1 if archived else 0, code),
        )
        con.commit()
    finally:
        con.close()


def _update_repost_sync(code: str, repost_code: str, repost_pk: str) -> None:
    con = _connect()
    try:
        con.execute(
            "UPDATE processed_media SET repost_code = ?, repost_pk = ? WHERE media_code = ?",
            (repost_code, repost_pk, code),
        )
        con.commit()
    finally:
        con.close()


def _last_published_at_sync() -> Optional[str]:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT published_at FROM processed_media ORDER BY published_at DESC LIMIT 1"
        ).fetchall()
        return rows[0][0] if rows else None
    finally:
        con.close()


def _count_today_sync() -> int:
    con = _connect()
    try:
        rows = con.execute(
            "SELECT COUNT(*) FROM processed_media WHERE date(published_at) = date('now')"
        ).fetchall()
        return int(rows[0][0]) if rows else 0
    finally:
        con.close()


def _save_session_sync(key: str, state_json: str) -> None:
    con = _connect()
    try:
        con.execute(
            "INSERT INTO session_cache(key, state_json, updated_at) VALUES (?,?,CURRENT_TIMESTAMP)"
            " ON CONFLICT(key) DO UPDATE SET state_json=excluded.state_json,"
            " updated_at=CURRENT_TIMESTAMP",
            (key, state_json),
        )
        con.commit()
    finally:
        con.close()


def _load_session_sync(key: str) -> Optional[str]:
    con = _connect()
    try:
        rows = con.execute("SELECT state_json FROM session_cache WHERE key = ?", (key,)).fetchall()
        return rows[0][0] if rows else None
    finally:
        con.close()


async def init_db() -> None:
    await asyncio.to_thread(_init_db_sync)


async def insert_processed(code: str, author: str, original_url: str = "", cached_url: str = "") -> None:
    await asyncio.to_thread(_insert_processed_sync, code, author, original_url, cached_url)


async def get_processed_codes() -> set[str]:
    return await asyncio.to_thread(_get_processed_codes_sync)


async def get_processed_authors() -> set[str]:
    return await asyncio.to_thread(_get_processed_authors_sync)


async def get_recent(limit: int = 5) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_get_recent_sync, limit)


async def get_unscanned(limit: int = 50) -> list[dict[str, Any]]:
    """Alias used by archive job: rows with archived=0 AND archive_scanned=0."""
    return await asyncio.to_thread(_get_archive_candidates_sync, limit)


async def get_archive_candidates(limit: int = 50) -> list[dict[str, Any]]:
    return await asyncio.to_thread(_get_archive_candidates_sync, limit)


async def mark_archived(code: str, archived: int = 1) -> None:
    await asyncio.to_thread(_mark_archived_sync, code, archived)


async def mark_scanned(code: str, archived: int = 0) -> None:
    await asyncio.to_thread(_mark_scanned_sync, code, archived)


async def update_repost(code: str, repost_code: str, repost_pk: str) -> None:
    await asyncio.to_thread(_update_repost_sync, code, repost_code, repost_pk)


async def last_published_at() -> Optional[str]:
    return await asyncio.to_thread(_last_published_at_sync)


async def count_today() -> int:
    return await asyncio.to_thread(_count_today_sync)


async def save_session(key: str, state: dict[str, Any] | str) -> None:
    payload = state if isinstance(state, str) else json.dumps(state)
    await asyncio.to_thread(_save_session_sync, key, payload)


async def load_session(key: str) -> Optional[str]:
    return await asyncio.to_thread(_load_session_sync, key)
