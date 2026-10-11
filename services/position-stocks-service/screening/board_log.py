"""screening/board_log.py - group309: shadow logging of Market Board signals + forward-return fill.
Rows are queued (bounded) and written by one daemon thread: neither the entry path nor a route waits on the DB; a full queue drops
the row (counted). snapshot rows = top BOARD_LOG_TOP_N per side every BOARD_LOG_EVERY_S; entry rows = one per entry the chase guard
looked at. fill_forward() computes +15/+30/+60 min and close returns from the same day's in-memory bars. Never raises into a caller."""
from __future__ import annotations

import logging
import queue
import threading
import time
from datetime import datetime, timezone
from typing import Callable, Dict, List, Optional, Sequence

import config

logger = logging.getLogger("position-stocks-board-log")

HORIZONS = (("fwd15_pct", 15), ("fwd30_pct", 30), ("fwd60_pct", 60))
IST_OFFSET_S = 330 * 60

_q: "queue.Queue[dict]" = queue.Queue(maxsize=2000)
_writer: Optional[Callable[[List[dict]], None]] = None
_thread: Optional[threading.Thread] = None
_lock = threading.Lock()
_stats = {"queued": 0, "dropped": 0, "written": 0, "write_errors": 0, "filled": 0}


def stats() -> dict:
    return dict(_stats, backlog=_q.qsize())


def reset() -> None:
    global _writer
    while True:
        try:
            _q.get_nowait()
        except queue.Empty:
            break
    _writer = None
    for k in _stats:
        _stats[k] = 0


def enqueue(row: dict) -> bool:
    try:
        _q.put_nowait(row)
        _stats["queued"] += 1
        _ensure_thread()
        return True
    except queue.Full:
        _stats["dropped"] += 1
        return False
    except Exception:  # noqa: BLE001
        return False


def _default_writer(rows: List[dict]) -> None:
    import db as _db
    from models import ScalpBoardLog
    with _db.get_session_factory()() as s:
        for r in rows:
            s.add(ScalpBoardLog(**r))
        s.commit()


def _drain(max_rows: int = 200) -> List[dict]:
    out: List[dict] = []
    while len(out) < max_rows:
        try:
            out.append(_q.get_nowait())
        except queue.Empty:
            break
    return out


def flush() -> int:
    total = 0
    while True:
        rows = _drain()
        if not rows:
            return total
        try:
            (_writer or _default_writer)(rows)
            _stats["written"] += len(rows)
            total += len(rows)
        except Exception as e:  # noqa: BLE001
            _stats["write_errors"] += 1
            logger.warning("board log: writing %d rows failed (%s: %s) - rows dropped", len(rows), type(e).__name__, e)
            return total


def _worker() -> None:
    while True:
        try:
            time.sleep(2.0)
            if not _q.empty():
                flush()
        except Exception:  # noqa: BLE001
            pass


def _ensure_thread() -> None:
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, name="board-log-writer", daemon=True)
            _thread.start()


def _day_str(ts: float) -> str:
    return datetime.fromtimestamp(ts + IST_OFFSET_S, tz=timezone.utc).strftime("%Y-%m-%d")


def snapshot_rows(payload: dict, top_n: int, now: float) -> List[dict]:
    out: List[dict] = []
    ver = payload.get("params_version")
    for key in ("bullish", "bearish"):
        for r in (payload.get(key) or [])[:top_n]:
            out.append({
                "ts": datetime.fromtimestamp(now, tz=timezone.utc), "day": _day_str(now), "kind": "snapshot",
                "symbol": r["symbol"], "side": r.get("side"), "rank": r.get("rank"), "strength": r.get("strength"),
                "conviction": r.get("conviction"), "ltp": r.get("ltp"), "day_open": r.get("open"), "vwap": r.get("vwap"),
                "chase_atr": r.get("chase_atr"), "rr": r.get("rr"), "zone_status": r.get("zone_status"),
                "in_position": bool(r.get("in_position")),
                "note": "extended" if r.get("extended") else None, "params_version": ver,
            })
    return out


def _bar_at(bars: Sequence, moment: float, max_gap_s: float) -> Optional[Sequence]:
    """Last bar starting at or before `moment`; None when absent or older than max_gap_s (no stale price carried forward)."""
    best = None
    for b in bars:
        if b[0] <= moment:
            best = b
        else:
            break
    if best is None or moment - best[0] > max_gap_s:
        return None
    return best


def forward_pct(bars: Sequence, ts: float, ltp: float, side: str, minutes: int, now: float) -> Optional[float]:
    """% return from ltp to the close of the minute containing ts+minutes, signed so positive = the side was right."""
    if not ltp or ltp <= 0 or not bars:
        return None
    target = ts + minutes * 60
    if now < target + 60:
        return None
    b = _bar_at(bars, target, 180.0)
    if b is None:
        return None
    pct = (b[4] - ltp) / ltp * 100.0
    return round(pct if side == "bull" else -pct, 4)


def close_pct(bars: Sequence, ltp: float, side: str, now: float, day_open_ts: float) -> Optional[float]:
    """Return to the 15:29 bar's close; None before 15:30 IST or when the last bar is older than 15:25."""
    if not ltp or ltp <= 0 or not bars:
        return None
    close_ts = day_open_ts + (15 * 3600 + 30 * 60) - (9 * 3600 + 15 * 60)
    if now < close_ts:
        return None
    b = _bar_at(bars, close_ts - 60, 300.0)
    if b is None:
        return None
    pct = (b[4] - ltp) / ltp * 100.0
    return round(pct if side == "bull" else -pct, 4)


def forward_updates(row: dict, bars: Sequence, now: float, day_open_ts: Optional[float]) -> Dict[str, float]:
    ts = row["ts"]
    ts = ts.timestamp() if hasattr(ts, "timestamp") and getattr(ts, "tzinfo", None) else (
        ts.replace(tzinfo=timezone.utc).timestamp() if hasattr(ts, "replace") else float(ts))
    side, ltp = row.get("side"), row.get("ltp")
    if side not in ("bull", "bear") or not ltp:
        return {}
    upd: Dict[str, float] = {}
    for col, m in HORIZONS:
        if row.get(col) is None:
            v = forward_pct(bars, ts, ltp, side, m, now)
            if v is not None:
                upd[col] = v
    if row.get("fwd_close_pct") is None and day_open_ts:
        v = close_pct(bars, ltp, side, now, day_open_ts)
        if v is not None:
            upd["fwd_close_pct"] = v
    return upd


def fill_forward(db, day: str, now: Optional[float] = None) -> int:
    try:
        from feed import bar_store
        from models import ScalpBoardLog
        from screening import board
        now = time.time() if now is None else now
        open_ts = board.session_open_ts(bar_store.day_number())
        rows = (db.query(ScalpBoardLog).filter(ScalpBoardLog.day == day)
                .filter((ScalpBoardLog.fwd15_pct.is_(None)) | (ScalpBoardLog.fwd30_pct.is_(None))
                        | (ScalpBoardLog.fwd60_pct.is_(None)) | (ScalpBoardLog.fwd_close_pct.is_(None)))
                .order_by(ScalpBoardLog.id.desc()).limit(3000).all())
        n = 0
        cache: Dict[str, list] = {}
        for r in rows:
            bars = cache.get(r.symbol)
            if bars is None:
                bars = cache[r.symbol] = bar_store.get_bars(r.symbol)
            upd = forward_updates({"ts": r.ts, "side": r.side, "ltp": r.ltp, "fwd15_pct": r.fwd15_pct, "fwd30_pct": r.fwd30_pct,
                                   "fwd60_pct": r.fwd60_pct, "fwd_close_pct": r.fwd_close_pct}, bars, now, open_ts)
            if upd:
                for k, v in upd.items():
                    setattr(r, k, v)
                n += 1
        if n:
            db.commit()
            _stats["filled"] += n
        return n
    except Exception as e:  # noqa: BLE001
        logger.warning("board log: forward fill failed (%s: %s)", type(e).__name__, e)
        try:
            db.rollback()
        except Exception:  # noqa: BLE001
            pass
        return 0


def record_snapshot(payload: dict, now: Optional[float] = None) -> int:
    now = time.time() if now is None else now
    n = 0
    for r in snapshot_rows(payload, config.BOARD_LOG_TOP_N, now):
        if enqueue(r):
            n += 1
    return n
