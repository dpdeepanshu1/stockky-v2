"""A3: write-behind queue for NON-CRITICAL evidence rows (real-trade).

Why: on 13 Oct and 12 Oct the Oracle DB saturated (DPY-4024, 8 s call timeout) and the exit tick died on an INSERT into
trade_exit_decisions - a row that only records "HOLD". The entry cycle likewise writes one trade_decisions row per WAIT candidate inside the same
transaction as real orders. Evidence rows must never delay or fail a stop, a fill or an order.

What goes through the queue (plain dict payloads, never ORM objects, so nothing crosses threads):
  exit_hold       TradeExitDecision rows with action HOLD
  entry_wait      TradeDecision rows with action WAIT (no order, no position hangs off them)
  structure_obs   StructureObservation rows (evidence only; 29 Oct 2026: its INSERT timed out inside the entry cycle's transaction)
  audit           the EXIT_CYCLE summary row of trade_audit_log (29 Oct 2026: its commit stalled the exit tick for the 8 s call timeout)
What stays synchronous: orders, fills, positions, cash, every non-HOLD exit decision, every other audit row (log_action commits the caller's session).

Behaviour
  * bounded: WRITE_BEHIND_MAX_QUEUE rows (default 5000); when full the OLDEST row is dropped and counted (evidence, not state)
  * one daemon worker with its own DB session; batches of WRITE_BEHIND_BATCH rows (default 50) every WRITE_BEHIND_FLUSH_S (default 1.0 s)
  * back-off (group 347 extended to the whole cycle): after any DB call timeout - from this worker OR reported by the tick via
    note_timeout() - nothing is written for WRITE_BEHIND_BACKOFF_S (default 20 s); rows wait in the queue
  * a failed batch is retried twice, then dropped and counted; a timed-out batch is put back and waits out the back-off
  * WRITE_BEHIND_ENABLED=0 (default 1): submit() returns False and every caller writes synchronously as before
  * flush() drains the queue at shutdown (best effort, time-boxed)
A crash loses at most the queued rows (seconds of evidence). Nothing here ever raises into a caller.
"""
from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("real-trade-write-behind")


def _num(raw, default, lo, cast=float):
    try:
        v = cast(raw)
    except (TypeError, ValueError):
        return default
    return v if v >= lo else default


def enabled() -> bool:
    return ((os.environ.get("WRITE_BEHIND_ENABLED") or "").strip() or "1").lower() not in ("0", "false", "no", "off")


def max_queue() -> int:
    return int(_num((os.environ.get("WRITE_BEHIND_MAX_QUEUE") or "").strip() or "5000", 5000, 10, int))


def batch_size() -> int:
    return int(_num((os.environ.get("WRITE_BEHIND_BATCH") or "").strip() or "50", 50, 1, int))


def flush_s() -> float:
    return _num((os.environ.get("WRITE_BEHIND_FLUSH_S") or "").strip() or "1.0", 1.0, 0.05)


def backoff_s() -> float:
    return _num((os.environ.get("WRITE_BEHIND_BACKOFF_S") or "").strip() or "20", 20.0, 0.0)


def is_db_timeout(exc: BaseException) -> bool:
    txt = f"{type(exc).__name__} {exc}"
    return "DPY-4024" in txt or "call timeout" in txt.lower()


_MAX_TRIES = 3

_lock = threading.Lock()
_q: deque = deque()                      # (kind, payload, tries)
_handlers: Dict[str, Callable] = {}      # kind -> fn(db, [payload, ...])
_session_factory: Optional[Callable] = None
_worker: Optional[threading.Thread] = None
_stop = threading.Event()
_backoff_until = 0.0
_stats = {"submitted": 0, "written": 0, "dropped_full": 0, "dropped_failed": 0, "timeouts": 0, "batches": 0, "last_error": None}


def register(kind: str, fn: Callable) -> None:
    _handlers[kind] = fn


def configure(session_factory: Optional[Callable] = None) -> None:
    """Tests / boot: set the callable that returns a new DB session. Default is db.get_session_factory()()."""
    global _session_factory
    _session_factory = session_factory


def in_backoff() -> bool:
    return time.monotonic() < _backoff_until


def note_timeout() -> None:
    """A DB call timed out somewhere (the exit tick, the full tick, this worker): stop evidence writes for backoff_s(). Never raises."""
    global _backoff_until
    try:
        with _lock:
            _stats["timeouts"] += 1
            if backoff_s() > 0:
                _backoff_until = max(_backoff_until, time.monotonic() + backoff_s())
    except Exception:  # noqa: BLE001
        pass


def submit(kind: str, payload: dict) -> bool:
    """Queue one row. True = queued (the caller must NOT also write it); False = disabled / unknown kind, write synchronously."""
    try:
        if not enabled() or kind not in _handlers:
            return False
        with _lock:
            if len(_q) >= max_queue():
                _q.popleft()
                _stats["dropped_full"] += 1
            _q.append((kind, payload, 0))
            _stats["submitted"] += 1
        _ensure_worker()
        return True
    except Exception:  # noqa: BLE001
        return False


def status() -> dict:
    with _lock:
        s = dict(_stats)
        s["queued"] = len(_q)
    s["enabled"] = enabled()
    s["backoff_remaining_s"] = round(max(0.0, _backoff_until - time.monotonic()), 1)
    s["max_queue"] = max_queue()
    return s


def _new_session():
    if _session_factory is not None:
        return _session_factory()
    import db as _dbmod
    return _dbmod.get_session_factory()()


def drain_once(limit: Optional[int] = None) -> int:
    """Write up to `limit` queued rows (one batch per kind). Returns rows written. Honors the back-off. Never raises."""
    if in_backoff():
        return 0
    with _lock:
        if not _q:
            return 0
        n = limit or batch_size()
        kind = _q[0][0]
        batch: List[tuple] = []
        while _q and len(batch) < n and _q[0][0] == kind:
            batch.append(_q.popleft())
    fn = _handlers.get(kind)
    if fn is None:
        return 0
    session = None
    try:
        session = _new_session()
        fn(session, [b[1] for b in batch])
        session.commit()
        with _lock:
            _stats["written"] += len(batch)
            _stats["batches"] += 1
        return len(batch)
    except Exception as e:  # noqa: BLE001
        try:
            if session is not None:
                session.rollback()
        except Exception:  # noqa: BLE001
            pass
        timed_out = is_db_timeout(e)
        with _lock:
            _stats["last_error"] = f"{type(e).__name__}: {str(e)[:160]}"
            keep = []
            for k, p, tries in batch:
                if timed_out:
                    keep.append((k, p, tries))           # DB pressure, not a bad row: wait out the back-off
                elif tries + 1 < _MAX_TRIES:
                    keep.append((k, p, tries + 1))
                else:
                    _stats["dropped_failed"] += 1
            for item in reversed(keep):
                if len(_q) < max_queue():
                    _q.appendleft(item)
                else:
                    _stats["dropped_full"] += 1
        if timed_out:
            note_timeout()
        logger.warning("write-behind batch of %d %s row(s) failed (%s)", len(batch), kind, _stats["last_error"])
        return 0
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:  # noqa: BLE001
            pass


def _run() -> None:
    while not _stop.is_set():
        try:
            wrote = drain_once()
        except Exception:  # noqa: BLE001
            wrote = 0
        if wrote == 0 or not _q:
            _stop.wait(flush_s())


def _ensure_worker() -> None:
    global _worker
    if _worker is not None and _worker.is_alive():
        return
    with _lock:
        if _worker is not None and _worker.is_alive():
            return
        _stop.clear()
        _worker = threading.Thread(target=_run, name="rt-write-behind", daemon=True)
        _worker.start()


def flush(timeout_s: float = 5.0) -> int:
    """Shutdown: drain what is queued, ignoring the back-off only if the DB is healthy (a timeout re-opens it and we stop). Returns rows left."""
    end = time.monotonic() + max(0.0, timeout_s)
    global _backoff_until
    _backoff_until = 0.0
    while time.monotonic() < end:
        with _lock:
            if not _q:
                break
        if drain_once() == 0 and in_backoff():
            break
    with _lock:
        return len(_q)


def stop() -> None:
    _stop.set()


def reset_for_tests() -> None:
    global _backoff_until, _session_factory
    with _lock:
        _q.clear()
        for k in _stats:
            _stats[k] = None if k == "last_error" else 0
    _backoff_until = 0.0
    _session_factory = None
    _handlers.clear()
    register_defaults()


# ── row helpers (payload <-> model) ───────────────────────────────────────────────────────────────────────────────────
def decision_payload(obj) -> Optional[dict]:
    """Column values of a pending TradeDecision (no id). None when it is not a plain unflushed row. Never raises."""
    try:
        if getattr(obj, "id", None) is not None:
            return None
        out = {c.key: getattr(obj, c.key, None) for c in obj.__table__.columns if c.key != "id"}
        if out.get("created_at") is None:
            out["created_at"] = datetime.now(timezone.utc)
        return out
    except Exception:  # noqa: BLE001
        return None


def divert_pending_waits(db) -> int:
    """Move pending WAIT TradeDecision rows flagged `_wb_divertable` (set by the entry cycle's _wait helper) out of the session and into
    the queue, so the cycle's commit carries orders only. Returns how many were diverted. Anything it cannot divert stays in the session
    and is written synchronously as before. Never raises."""
    n = 0
    try:
        for obj in list(db.new):
            if (getattr(obj, "_wb_divertable", False) and getattr(obj, "action", None) == "WAIT"
                    and getattr(obj, "decision_type", None) == "ENTRY"):
                payload = decision_payload(obj)
                if payload is not None and submit("entry_wait", payload):
                    db.expunge(obj)
                    n += 1
    except Exception:  # noqa: BLE001
        pass
    return n


def _write_exit_hold(db, payloads: list) -> None:
    import models
    db.bulk_insert_mappings(models.TradeExitDecision, payloads)


def _write_entry_wait(db, payloads: list) -> None:
    import models
    db.bulk_insert_mappings(models.TradeDecision, payloads)


def _write_structure_obs(db, payloads: list) -> None:
    """E-structure-obs: StructureObservation rows (one per mode / symbol / IST day, first verdict wins). The de-duplication read happens HERE,
    on the worker's own session, not inside the entry cycle. A retried batch is safe: a row that already landed is skipped."""
    import models
    from entry_engine import structure_observation as _so
    seen: set = set()
    rows: list = []
    for p in payloads:
        key = (str(p.get("mode") or "").upper(), p.get("symbol"), p.get("obs_day"))
        if key in seen:
            continue
        seen.add(key)
        if _so._exists(db, key[0], key[1], key[2]):
            continue
        q = dict(p)
        if q.get("created_at") is None:
            q["created_at"] = datetime.now(timezone.utc)
        rows.append(q)
    if rows:
        db.bulk_insert_mappings(models.StructureObservation, rows)


def _write_audit(db, payloads: list) -> None:
    """E-structure-obs: EXIT_CYCLE summary rows of trade_audit_log (occurred_at takes its column default)."""
    import models
    db.bulk_insert_mappings(models.TradeAuditLog, payloads)


def register_defaults() -> None:
    register("exit_hold", _write_exit_hold)
    register("entry_wait", _write_entry_wait)
    register("structure_obs", _write_structure_obs)
    register("audit", _write_audit)


register_defaults()
