"""orders/candidate_log_writer.py - Batch 1 (2026-11-02): SKIPPED candidate-log rows off the entry hot path.

Why: entry._log_candidate used to INSERT + COMMIT one scalp_candidate_log row per skip, on the same Oracle session the
entry decision uses. Over 10 days that wrote 1,845 STOP_ATR_FLOOR rows for 147 symbols (~12 per symbol) and 413
STRUCTURE_BAD_LOCATION rows for 33, and every row was an Oracle round trip that could hit the 8 s call timeout.

What this does for SKIPPED rows only (ENTERED rows stay synchronous - they are rare and ordering matters):
  1. De-duplicates by (symbol, decision, first two ':' parts of the reason) inside CANDIDATE_LOG_DEDUPE_S (default 300 s).
     The first row of a window is kept, so the gate scorecard (which de-duplicates anyway) sees the same first sample.
  2. Queues the row and returns at once. One daemon thread writes batches (own session, one commit per batch).
  3. A failed batch is rolled back and dropped with a warning (the log is an audit trail, never worth blocking a trade);
     after a DB call timeout the thread backs off CANDIDATE_LOG_BACKOFF_S.

Rollback: CANDIDATE_LOG_ASYNC=false puts back the old one-row-one-commit path (entry.py checks `enabled()`).
Also here: purge_old() - retention for the table (CANDIDATE_LOG_RETENTION_DAYS, default 30), run once a day by main.py.
Nothing in this module raises into a caller.
"""
from __future__ import annotations

import logging
import queue
import re
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import config

logger = logging.getLogger("position-stocks.candidate_log")

_q: "queue.Queue[Dict[str, Any]]" = queue.Queue(maxsize=int(getattr(config, "CANDIDATE_LOG_QUEUE_MAX", 5000) or 5000))
_seen: Dict[Tuple[str, str, str], float] = {}
_seen_lock = threading.Lock()
_NUM = re.compile(r"[-+]?\d+(?:\.\d+)?")
_thread: Optional[threading.Thread] = None
_thread_lock = threading.Lock()
_backoff_until = 0.0
stats: Dict[str, int] = {"queued": 0, "written": 0, "deduped": 0, "dropped_full": 0, "failed_batches": 0}


def enabled() -> bool:
    return bool(getattr(config, "CANDIDATE_LOG_ASYNC", True))


def _key(symbol: str, decision: str, reason: str) -> Tuple[str, str, str]:
    r = ":".join(str(reason or decision).split(":")[:2])[:60]
    r = _NUM.sub("#", r)                                     # "2.35 ATR past" and "2.37 ATR past" are the same refusal
    return (str(symbol), str(decision), r)


def is_duplicate(symbol: str, decision: str, reason: str, now: Optional[float] = None) -> bool:
    """True when the same (symbol, decision, reason head) was queued less than CANDIDATE_LOG_DEDUPE_S ago. Records it otherwise."""
    window = float(getattr(config, "CANDIDATE_LOG_DEDUPE_S", 300.0) or 0.0)
    if window <= 0:
        return False
    now = time.monotonic() if now is None else now
    k = _key(symbol, decision, reason)
    with _seen_lock:
        last = _seen.get(k)
        if last is not None and now - last < window:
            stats["deduped"] += 1
            return True
        _seen[k] = now
        if len(_seen) > 20000:                               # bounded: drop everything older than the window
            for kk in [kk for kk, t in _seen.items() if now - t >= window]:
                _seen.pop(kk, None)
    return False


def submit(row: Dict[str, Any]) -> bool:
    """Queue one SKIPPED row (a dict of ScalpCandidateLog column values). True when queued or de-duplicated away;
    False when it could not be queued (caller may then write it the old way)."""
    try:
        if is_duplicate(row.get("symbol", ""), row.get("decision", "SKIPPED"), row.get("reason") or ""):
            return True
        _ensure_thread()
        try:
            _q.put_nowait(dict(row, created_at=datetime.utcnow()))
        except queue.Full:
            stats["dropped_full"] += 1
            return False
        stats["queued"] += 1
        return True
    except Exception:  # noqa: BLE001
        logger.debug("candidate_log_writer.submit failed", exc_info=True)
        return False


def _ensure_thread() -> None:
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    with _thread_lock:
        if _thread is not None and _thread.is_alive():
            return
        _thread = threading.Thread(target=_run, name="candidate-log-writer", daemon=True)
        _thread.start()


def _drain(max_rows: int) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        rows.append(_q.get(timeout=2.0))
    except queue.Empty:
        return rows
    while len(rows) < max_rows:
        try:
            rows.append(_q.get_nowait())
        except queue.Empty:
            break
    return rows


def write_batch(rows: List[Dict[str, Any]]) -> int:
    """Insert `rows` on a fresh session with one commit. Returns the number written (0 on failure). Never raises."""
    global _backoff_until
    if not rows:
        return 0
    session = None
    try:
        import db as _db
        import models
        factory = _db.get_session_factory()
        if factory is None:
            return 0
        session = factory()
        session.add_all([models.ScalpCandidateLog(**r) for r in rows])
        session.commit()
        stats["written"] += len(rows)
        return len(rows)
    except Exception as e:  # noqa: BLE001
        stats["failed_batches"] += 1
        txt = f"{type(e).__name__} {e}"
        if "DPY-4024" in txt or "call timeout" in txt.lower():
            _backoff_until = time.monotonic() + float(getattr(config, "CANDIDATE_LOG_BACKOFF_S", 30.0) or 0.0)
        logger.warning("candidate log: dropped a batch of %d row(s): %s", len(rows), str(e)[:200])
        try:
            if session is not None:
                session.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("candidate log: rollback failed", exc_info=True)
        return 0
    finally:
        try:
            if session is not None:
                session.close()
        except Exception:  # noqa: BLE001
            logger.debug("candidate log: session close failed", exc_info=True)


def _run() -> None:
    batch_max = int(getattr(config, "CANDIDATE_LOG_BATCH_MAX", 200) or 200)
    while True:
        try:
            wait = _backoff_until - time.monotonic()
            if wait > 0:
                time.sleep(min(wait, 5.0))
                continue
            rows = _drain(batch_max)
            if rows:
                write_batch(rows)
        except Exception:  # noqa: BLE001
            logger.debug("candidate log writer loop error", exc_info=True)
            time.sleep(1.0)


def flush(timeout_s: float = 5.0) -> None:
    """Write whatever is queued now (used at shutdown and in tests)."""
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        rows: List[Dict[str, Any]] = []
        while len(rows) < 1000:
            try:
                rows.append(_q.get_nowait())
            except queue.Empty:
                break
        if not rows:
            return
        write_batch(rows)


def purge_old(db: Any, now: Optional[datetime] = None, chunk: int = 5000, max_chunks: int = 20) -> int:
    """Delete scalp_candidate_log rows older than CANDIDATE_LOG_RETENTION_DAYS in chunks (short transactions).
    0 days = keep forever. Returns rows deleted. Never raises."""
    days = float(getattr(config, "CANDIDATE_LOG_RETENTION_DAYS", 30.0) or 0.0)
    if days <= 0:
        return 0
    total = 0
    try:
        import models
        M = models.ScalpCandidateLog
        cutoff = (now or datetime.utcnow()) - timedelta(days=days)
        for _ in range(max_chunks):
            ids = [r[0] for r in db.query(M.id).filter(M.created_at < cutoff).order_by(M.id).limit(chunk).all()]
            if not ids:
                break
            # range delete, not IN (...): Oracle caps an IN list at 1000 items (ORA-01795)
            n = db.query(M).filter(M.id <= ids[-1], M.created_at < cutoff).delete(synchronize_session=False)
            db.commit()
            total += int(n or 0)
            if len(ids) < chunk:
                break
    except Exception as e:  # noqa: BLE001
        logger.warning("candidate log retention purge failed: %s", str(e)[:200])
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            logger.debug("candidate log purge: rollback failed", exc_info=True)
    if total:
        logger.info("candidate log retention: deleted %d row(s) older than %.0f days", total, days)
    return total
