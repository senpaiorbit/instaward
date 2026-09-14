"""Archive job: archive stale/low-view reposts via real media_archive.

Policy:
- Only DB-tracked rows (processed_media WHERE archived=0 AND archive_scanned=0).
- For each: media_info + insights for age/views.
- If age > threshold AND views <= threshold -> media_archive(f"{pk}_{user_id}").
- Fallback to local-only mark if archive fails; NEVER delete.
- Always set archive_scanned=1 so we never hard-scan everything.
"""
import logging
from datetime import datetime, timezone
from typing import Any

log = logging.getLogger("instaward-bot")


def _parse_published_at(raw: Any) -> datetime | None:
    if not raw:
        return None
    try:
        if isinstance(raw, (int, float)):
            return datetime.fromtimestamp(float(raw), tz=timezone.utc)
        dt = datetime.fromisoformat(str(raw).replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except Exception:  # noqa: BLE001
        return None


def _as_count(v: Any) -> int | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    return None


def _is_clip_media(obj: Any) -> bool:
    try:
        if isinstance(obj, dict):
            ptype = obj.get("product_type")
            if isinstance(ptype, str) and ptype.strip().lower() == "clips":
                return True
            return "clips_metadata" in obj and obj.get("clips_metadata") is not None
        ptype = getattr(obj, "product_type", None)
        if isinstance(ptype, str) and ptype.strip().lower() == "clips":
            return True
        return getattr(obj, "clips_metadata", None) is not None
    except Exception:
        return False


def _extract_views(info: Any, insights: Any) -> int | None:
    for obj in (insights, info):
        if obj is None:
            continue
        if isinstance(obj, dict):
            if "inline_insights_node" in obj and not any(
                k in obj for k in ("play_count", "view_count", "views", "plays")
            ):
                continue
            pc = _as_count(obj.get("play_count"))
            if pc is not None:
                return pc
            if _is_clip_media(obj):
                continue
            for k in ("view_count", "views", "plays"):
                vc = _as_count(obj.get(k))
                if vc is not None:
                    return vc
            continue
        pc = _as_count(getattr(obj, "play_count", None))
        if pc is not None:
            return pc
        if _is_clip_media(obj):
            continue
        vc = _as_count(getattr(obj, "view_count", None))
        if vc is not None:
            return vc
    return None
