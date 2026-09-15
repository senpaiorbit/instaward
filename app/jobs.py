"""In-memory background-job registry. Import-safe: stdlib only, no app imports at module level."""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Any

_JOBS: dict[str, dict] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_job(kind: str, params: dict) -> dict:
    job_id = uuid.uuid4().hex[:12]
    now = _now_iso()
    job: dict = {
        "id": job_id,
        "kind": kind,
        "params": params,
        "status": "running",
        "checked": 0,
        "archived": 0,
        "kept": 0,
        "errors": [],
        "created_at": now,
        "started_at": now,
        "finished_at": None,
        "result": None,
        "error": None,
    }
    _JOBS[job_id] = job
    return job


def get_job(job_id: str) -> dict | None:
    job = _JOBS.get(job_id)
    if job is None:
        return None
    copy = dict(job)
    copy["errors"] = list(job.get("errors") or [])
    return copy


def _age_sec(job: dict) -> float:
    try:
        iso = job.get("created_at") or job.get("started_at") or ""
        dt = datetime.fromisoformat(str(iso).replace("Z", ""))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - dt).total_seconds()
    except Exception:
        return 0.0


def _is_stale(job: dict, max_age: int = 480) -> bool:
    if job.get("status") != "running":
        return False
    age = _age_sec(job)
    if age < max_age:
        return False
    attempted = job.get("attempted")
    checked = job.get("checked", 0)
    if attempted is not None:
        return int(attempted or 0) == 0
    return int(checked or 0) == 0


def current_running(kind: str) -> dict | None:
    for job in _JOBS.values():
        if job.get("kind") == kind and job.get("status") == "running":
            if _is_stale(job):
                try:
                    job.update(status="error", finished_at=_now_iso(), error="stale timeout (no progress >8min)")
                except Exception:
                    pass
                continue
            return job
    return None


def prune(keep: int = 20) -> None:
    try:
        keep_n = int(keep)
    except (TypeError, ValueError):
        keep_n = 20
    done = [j for j in _JOBS.values() if j.get("status") in ("done", "error")]
    if len(done) <= keep_n:
        return
    done.sort(key=lambda j: str(j.get("created_at") or ""))
    for old in done[: len(done) - keep_n]:
        _JOBS.pop(old.get("id", ""), None)


async def run_archive_job(job_id: str, time_sec: int, views_thresh: int, all_flag: bool = False) -> None:
    from app import archive as archmod

    job = _JOBS.get(job_id)
    if job is None:
        return
    try:
        summary = await archmod.run_archive(time_sec, views_thresh, job=job, all=all_flag)
        job.update(status="done", finished_at=_now_iso(), result=summary)
    except Exception as exc:  # noqa: BLE001
        job.update(status="error", finished_at=_now_iso(), error=f"{type(exc).__name__}: {exc}")


async def run_upload_job(job_id: str, kwargs: dict) -> None:
    from app import curation as curmod

    job = _JOBS.get(job_id)
    if job is None:
        return
    try:
        summary = await asyncio.wait_for(curmod.run_curation(job=job, **kwargs), timeout=420)
        job.update(status="done", finished_at=_now_iso(), result=summary)
    except asyncio.TimeoutError:
        job.update(status="error", finished_at=_now_iso(), error="TimeoutError: upload exceeded 7min (fetch/quality/upload hung)")
    except Exception as exc:  # noqa: BLE001
        job.update(status="error", finished_at=_now_iso(), error=f"{type(exc).__name__}: {exc}")
