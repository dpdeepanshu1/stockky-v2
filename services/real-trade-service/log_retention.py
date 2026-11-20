"""Round 658: nightly retention for evidence rows (real-trade).

Why: trade_decisions held 88k rows (176 MB + 80 MB of text columns) and trade_exit_decisions 310k rows (61 MB) on 20 Nov 2026, with nothing
ever deleting them. Most of those rows are repeats: a WAIT for the same symbol every cycle, a HOLD for the same position every few seconds.

What is deleted (older than LOG_RETENTION_DAYS, default 30):
  trade_decisions        rows with action WAIT that no order points at (trade_orders.decision_id). ENTER rows and every row an order
                         references are kept, so an order's history never loses its decision.
  trade_exit_decisions   rows with action HOLD. TRAIL_STOP / PARTIAL_EXIT / FULL_EXIT / EMERGENCY_EXIT rows are kept: they are the exits.
Nothing else is touched (orders, fills, positions, audit log, candidates, risk events).

When: only outside market hours - IST 15:45 until 08:30 the next morning (LOG_RETENTION_START_IST / LOG_RETENTION_END_IST), at most once per
IST day, in small batches (LOG_RETENTION_BATCH, default 500 ids per DELETE, one commit per batch, LOG_RETENTION_PAUSE_S between batches).
The run stops by itself when the window closes, after LOG_RETENTION_MAX_BATCHES batches, or on the first DB error (a call timeout also
opens write_behind's back-off); what is left is picked up on the next run.

Safety: LOG_RETENTION_DAYS below 7 is ignored (the default 30 applies), so a typo cannot wipe recent evidence. LOG_RETENTION_ENABLED=0 turns the
job off. Nothing here ever raises into the service.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Dict, Optional

logger = logging.getLogger("real-trade-log-retention")

_IST = timezone(timedelta(hours=5, minutes=30))
_CHECK_EVERY_S = 300.0
_RETRY_AFTER_ERROR_S = 1800.0


def _env(name: str, default: str) -> str:
    return (os.environ.get(name) or "").strip() or default


def enabled() -> bool:
    return _env("LOG_RETENTION_ENABLED", "1").lower() not in ("0", "false", "no", "off")


def _num(raw, default, lo, cast=float):
    try:
        v = cast(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= lo else default


def keep_days() -> int:
    return int(_num(_env("LOG_RETENTION_DAYS", "30"), 30, 7, int))


def batch_size() -> int:
    return min(1000, int(_num(_env("LOG_RETENTION_BATCH", "500"), 500, 50, int)))     # Oracle allows at most 1000 items in an IN list


def pause_s() -> float:
    return _num(_env("LOG_RETENTION_PAUSE_S", "0.5"), 0.5, 0.0)


def max_batches() -> int:
    return int(_num(_env("LOG_RETENTION_MAX_BATCHES", "400"), 400, 1, int))


def _hm(name: str, default: str) -> tuple:
    raw = _env(name, default)
    try:
        h, m = raw.split(":", 1)
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return (h, m)
    except (ValueError, AttributeError):
        pass
    h, m = default.split(":", 1)
    return (int(h), int(m))


def in_window(now_ist: datetime) -> bool:
    """True from LOG_RETENTION_START_IST (15:45) through midnight to LOG_RETENTION_END_IST (08:30). Market hours are never inside it."""
    t = (now_ist.hour, now_ist.minute)
    start, end = _hm("LOG_RETENTION_START_IST", "15:45"), _hm("LOG_RETENTION_END_IST", "08:30")
    if start > end:                                    # the normal case: the window crosses midnight
        return t >= start or t < end
    return start <= t < end


def window_day(now_ist: datetime) -> str:
    """The IST day a run belongs to: after midnight the window still belongs to the previous evening, so one evening runs once."""
    end = _hm("LOG_RETENTION_END_IST", "08:30")
    d = now_ist.date()
    if (now_ist.hour, now_ist.minute) < end:
        d = d - timedelta(days=1)
    return d.isoformat()


def cutoff_utc(now_utc: Optional[datetime] = None, days: Optional[int] = None) -> datetime:
    """Naive UTC, like the created_at / evaluated_at columns."""
    n = now_utc or datetime.now(timezone.utc)
    return (n - timedelta(days=days if days is not None else keep_days())).replace(tzinfo=None)


def _is_timeout(exc: BaseException) -> bool:
    txt = f"{type(exc).__name__} {exc}"
    return "DPY-4024" in txt or "call timeout" in txt.lower()


def prune_once(db, models, cutoff: datetime, batch: Optional[int] = None, budget: Optional[int] = None, pause: Optional[float] = None,
               keep_going: Optional[Callable[[], bool]] = None, sleep: Callable[[float], None] = time.sleep) -> Dict[str, object]:
    """One pass over both tables on the given session. Returns counts: decisions, exit_holds, batches, complete, stopped, error."""
    from sqlalchemy import select

    n = batch or batch_size()
    cap = budget or max_batches()
    gap = pause_s() if pause is None else pause
    go = keep_going or (lambda: True)
    out: Dict[str, object] = {"decisions": 0, "exit_holds": 0, "batches": 0, "complete": False, "stopped": None, "error": None}

    D, O, E = models.TradeDecision, models.TradeOrder, models.TradeExitDecision
    referenced = select(O.decision_id).where(O.decision_id.isnot(None))

    def decision_ids(limit: int) -> list:
        q = db.query(D.id).filter(D.action == "WAIT", D.created_at < cutoff, ~D.id.in_(referenced)).limit(limit)
        return [r[0] for r in q.all()]

    def exit_ids(limit: int) -> list:
        q = db.query(E.id).filter(E.action == "HOLD", E.evaluated_at < cutoff).limit(limit)
        return [r[0] for r in q.all()]

    def drop_decisions(ids: list) -> None:
        db.query(D).filter(D.id.in_(ids)).delete(synchronize_session=False)

    def drop_exits(ids: list) -> None:
        db.query(E).filter(E.id.in_(ids)).delete(synchronize_session=False)

    steps = (("decisions", decision_ids, drop_decisions), ("exit_holds", exit_ids, drop_exits))
    try:
        for label, pick, drop in steps:
            while True:
                if int(out["batches"]) >= cap:
                    out["stopped"] = "budget"
                    return out
                if not go():
                    out["stopped"] = "window"
                    return out
                ids = pick(n)
                if not ids:
                    break
                drop(ids)
                db.commit()
                out[label] = int(out[label]) + len(ids)
                out["batches"] = int(out["batches"]) + 1
                if len(ids) < n:
                    break
                if gap > 0:
                    sleep(gap)
        out["complete"] = True
    except Exception as e:  # noqa: BLE001
        out["error"] = f"{type(e).__name__}: {str(e)[:160]}"
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        if _is_timeout(e):
            try:
                import write_behind
                write_behind.note_timeout()
            except Exception:  # noqa: BLE001
                pass
    return out


# ── scheduler ────────────────────────────────────────────────────────────────────────────────────────────────────────
_lock = threading.Lock()
_thread: Optional[threading.Thread] = None
_stop = threading.Event()
_last_day: Optional[str] = None
_retry_not_before = 0.0
_last_result: Optional[Dict[str, object]] = None


def last_result() -> Optional[Dict[str, object]]:
    return _last_result


def _new_session():
    import db as _dbmod
    return _dbmod.get_session_factory()()


def maybe_run(now_utc: Optional[datetime] = None, session_factory: Optional[Callable] = None, models_mod=None,
              sleep: Callable[[float], None] = time.sleep) -> Optional[Dict[str, object]]:
    """Run the prune if it is enabled, inside the window and not yet done for this evening. Returns the result, or None when it did not run."""
    global _last_day, _retry_not_before, _last_result
    if not enabled():
        return None
    now = (now_utc or datetime.now(timezone.utc)).astimezone(_IST)
    if not in_window(now):
        return None
    day = window_day(now)
    with _lock:
        if _last_day == day or time.monotonic() < _retry_not_before:
            return None
    session = None
    try:
        if models_mod is None:
            import models as models_mod  # noqa: PLW0127
        session = (session_factory or _new_session)()
        res = prune_once(session, models_mod, cutoff_utc(now.astimezone(timezone.utc)), sleep=sleep,
                         keep_going=lambda: in_window(datetime.now(timezone.utc).astimezone(_IST)))
    except Exception as e:  # noqa: BLE001
        res = {"decisions": 0, "exit_holds": 0, "batches": 0, "complete": False, "stopped": None, "error": f"{type(e).__name__}: {str(e)[:160]}"}
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:  # noqa: BLE001
            pass
    with _lock:
        _last_result = res
        if res.get("error"):
            _retry_not_before = time.monotonic() + _RETRY_AFTER_ERROR_S
        elif res.get("complete"):
            _last_day = day
    if res.get("error"):
        logger.warning("log-retention: stopped on a DB error after %s decision and %s exit row(s) (%s) - retries in %.0f min",
                       res.get("decisions"), res.get("exit_holds"), res.get("error"), _RETRY_AFTER_ERROR_S / 60.0)
    else:
        logger.info("log-retention: deleted %s WAIT decision row(s) and %s HOLD exit row(s) older than %d days in %s batch(es) (%s)",
                    res.get("decisions"), res.get("exit_holds"), keep_days(), res.get("batches"),
                    "complete" if res.get("complete") else f"stopped: {res.get('stopped')}")
    return res


def _run() -> None:
    while not _stop.is_set():
        try:
            maybe_run()
        except Exception:  # noqa: BLE001
            pass
        _stop.wait(_CHECK_EVERY_S)


def start() -> bool:
    """Start the daemon thread (idempotent). False when the job is disabled."""
    global _thread
    if not enabled():
        logger.info("log-retention: LOG_RETENTION_ENABLED=0 - evidence rows are not pruned")
        return False
    with _lock:
        if _thread is not None and _thread.is_alive():
            return True
        _stop.clear()
        _thread = threading.Thread(target=_run, name="rt-log-retention", daemon=True)
        _thread.start()
    logger.info("log-retention: on - WAIT decisions and HOLD exit rows older than %d days are deleted after %s IST", keep_days(), _env("LOG_RETENTION_START_IST", "15:45"))
    return True


def stop() -> None:
    _stop.set()


def reset_for_tests() -> None:
    global _last_day, _retry_not_before, _last_result
    with _lock:
        _last_day = None
        _retry_not_before = 0.0
        _last_result = None