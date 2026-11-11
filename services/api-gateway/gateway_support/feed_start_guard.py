"""E447: one start guard for the two feed-start routes (start-bulk-feed, refill-additional).

Both routes share the data-feed job row (Refill Additional mirrors its progress into it, and the slow-field run uses it too). Before E447
neither one refused a second start: start-bulk-feed had no check at all, and refill-additional's "running and not force" check never fired
because force defaults to true and the frontend always sends force=true. A double click (or the UI's retry) therefore started two workers
that wrote the same job row and fought over the stop flag.

`guarded()` answers `already_running` while a feed job made progress recently, and holds a process-local claim for the time a start request
spends resolving its symbols (awaits before the job row is written). The claim is released in a `finally`, so it can never stay set.
Stateless apart from the claim; `ref` is the MainRef the route factory receives (main.py names are read at call time)."""
from typing import Any, Awaitable, Callable, Dict, Optional

_CLAIM = {"busy": False}


def job_is_fresh(job: Optional[dict], stale_sec: float, ref: Any) -> bool:
    """True when the job row was updated within `stale_sec`. A missing / unparseable `updated_at` counts as NOT fresh, so a row left at
    "running" by a dead worker can never lock the feed out."""
    try:
        ts = ref.datetime.fromisoformat(str((job or {}).get("updated_at") or ""))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=ref.IST)
        return 0 <= (ref.datetime.now(ref.IST) - ts).total_seconds() < stale_sec
    except Exception:
        return False


def live_feed_job(ref: Any) -> Optional[Dict[str, Any]]:
    """The running feed job when it made progress recently, else None. BULK_FEED_STALE_AFTER_SEC (default 600, floor 30) is how long a silent
    "running" row is believed. The Refill Additional process-local mirror is checked too (it reports "stalled" on its own after
    REFILL_STALE_AFTER_SEC)."""
    try:
        stale = float((ref.os.getenv("BULK_FEED_STALE_AFTER_SEC") or "").strip() or "600")
    except Exception:
        stale = 600.0
    try:
        job = dict(ref._feed_store().job() or {})
    except Exception:
        job = {}
    if str(job.get("status") or "").lower() == "running" and job_is_fresh(job, max(stale, 30.0), ref):
        return job
    try:
        from refill_additional import get_refill_job
        rj = dict(get_refill_job() or {})
        if str(rj.get("status") or "").lower() == "running":
            return rj
    except Exception:
        pass
    return None


async def guarded(ref: Any, label: str, start: Callable[[], Awaitable[Any]]) -> Any:
    """Run `start()` only when no other start is resolving its symbols and no feed job is making progress."""
    if _CLAIM["busy"]:
        return {"ok": True, "already_running": True, "status": "running", "message": f"A {label} start is already in progress"}
    _CLAIM["busy"] = True
    try:
        live = live_feed_job(ref)
        if live is not None:
            return {"ok": True, **live, "already_running": True, "status": "running"}
        return await start()
    finally:
        _CLAIM["busy"] = False
