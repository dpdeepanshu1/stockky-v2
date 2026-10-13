"""dhan_data/live_store.py - writes Dhan quote rows into the shared `live_quotes` table (group 270).

real-trade-service's "Source 1" reads `live_quotes` through market-data-service's /live-quote route, so writing Dhan
rows here makes the whole trading side use them with no change on its side. Same dual dialect (Oracle MERGE /
Postgres ON CONFLICT) and same engine as angelone_ws_feed.py, but the `source` column says 'dhan'.

Writes happen on a small daemon thread fed by a bounded queue, so a slow database never delays the quote batcher.
`ohlc_json.close` carries the PREVIOUS close (that is what real-trade-service's _lq_prev_close reads); when Dhan has no
previous close it stores the LTP, which that reader treats as "unknown", exactly like the AngelOne writer.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from typing import List, Optional

from . import config

logger = logging.getLogger("dhan-data.live-store")

_q: "queue.Queue[List[dict]]" = queue.Queue(maxsize=3)
_thread = None
_lock = threading.Lock()
_stats = {"batches_written": 0, "rows_written": 0, "dropped_batches": 0, "last_error": None,
          "coalesced_batches": 0, "failed_chunks": 0, "timeouts": 0, "unchanged_skipped": 0, "priority_rows": 0,
          "memory_reads": 0}
_last_warn = 0.0
_WARN_EVERY_S = 60.0

# group344 (13 Oct log: "dhan live_quotes write: 397 of 472 rows ... DPY-4024 call timeout 8000 ms" and "live_quotes read failed
# for MEESHO ReadTimeout"): the writer re-sent ALL ~470-900 rows every 1.2 s even when the price had not moved, so Oracle was
# busy with MERGEs exactly when /live-quote and the held-position reads needed it.  Three changes, each switchable:
#   * change-only writes: a row whose price is unchanged is skipped for DHAN_LIVE_WRITE_REFRESH_S (default 4 s; 0 = write every row)
#   * priority first: symbols registered with mark_priority() (the held positions, sent by real-trade-service's priority quote
#     call) are written first, in their own small transaction, and are never skipped as "unchanged"
#   * memory first: latest() serves /live-quote from the rows the poller just priced (DHAN_LIVE_MEM_MAX_AGE_S, default 6 s;
#     0 = always read Oracle) so a held-position read does not need the database at all
_last_sent: dict = {}        # symbol -> (ltp, monotonic ts) of the last row that was written successfully
_latest: dict = {}           # symbol -> (monotonic ts, wall ts, row) of the newest row handed to submit()
_prio: dict = {}             # symbol -> monotonic expiry of its priority mark
_PRIO_TTL_S = 180.0
_LATEST_MAX = 4000


def write_mode() -> str:
    """DHAN_LIVE_WRITE: auto (default) | on | off. auto writes only when Dhan is the FIRST quote provider, so a
    shadow/after-AngelOne rollout never changes what real-trade-service reads."""
    v = config.env_str("DHAN_LIVE_WRITE", "auto").lower()
    return v if v in ("auto", "on", "off") else "auto"


def should_write() -> bool:
    m = write_mode()
    if m == "off":
        return False
    if m == "on":
        return True
    return config.quote_position() == "first"


def build_params(rows: List[dict]) -> List[dict]:
    params = []
    for r in rows:
        sym, ltp = r.get("symbol"), r.get("price")
        if not sym or not ltp or str(sym).startswith("^"):
            continue                      # live_quotes holds equities only (index quotes are served from the batcher)
        params.append({
            "s": str(sym),
            "l": float(ltp),
            "o": json.dumps({"open": r.get("open"), "high": r.get("day_high"), "low": r.get("day_low"),
                             "close": r.get("previous_close") or ltp, "prev_close": r.get("previous_close")}),
            "v": int(r.get("volume") or 0),
        })
    return params


def refresh_s() -> float:
    """DHAN_LIVE_WRITE_REFRESH_S (default 4; blank / bad -> 4; 0 or less = write every row, the old behaviour)."""
    return max(0.0, config.env_float("DHAN_LIVE_WRITE_REFRESH_S", 4.0, 0.0))


def mem_max_age_s() -> float:
    """DHAN_LIVE_MEM_MAX_AGE_S (default 6; 0 = never answer /live-quote from memory)."""
    return max(0.0, config.env_float("DHAN_LIVE_MEM_MAX_AGE_S", 6.0, 0.0))


def _clean(sym) -> str:
    return str(sym or "").upper().replace(".NS", "").replace(".BO", "").strip()


def mark_priority(symbols, ttl_s: float = _PRIO_TTL_S) -> int:
    """Mark symbols (the open positions) as priority for `ttl_s` seconds. Never raises; returns how many were marked."""
    n = 0
    try:
        until = time.monotonic() + max(1.0, float(ttl_s))
        with _lock:
            if len(_prio) > 500:
                now = time.monotonic()
                for k in [k for k, v in _prio.items() if v < now]:
                    _prio.pop(k, None)
            for s in symbols or []:
                c = _clean(s)
                if c:
                    _prio[c] = until
                    n += 1
    except Exception:  # noqa: BLE001
        pass
    return n


def priority_symbols() -> set:
    now = time.monotonic()
    with _lock:
        return {k for k, v in _prio.items() if v >= now}


def _ohlc_for(r: dict) -> dict:
    ltp = r.get("price")
    return {"open": r.get("open"), "high": r.get("day_high"), "low": r.get("day_low"),
            "close": r.get("previous_close") or ltp, "prev_close": r.get("previous_close")}


def _remember(rows: List[dict]) -> None:
    """Keep the newest row per symbol for latest(). In-memory only; never raises."""
    try:
        now_m, now_w = time.monotonic(), time.time()
        with _lock:
            if len(_latest) > _LATEST_MAX:
                _latest.clear()
            for r in rows or []:
                sym, ltp = r.get("symbol"), r.get("price")
                if not sym or not ltp or str(sym).startswith("^"):
                    continue
                _latest[_clean(sym)] = (now_m, now_w, r)
    except Exception:  # noqa: BLE001
        pass


def latest(symbol: str, max_age_s: Optional[float] = None) -> Optional[dict]:
    """The newest Dhan row for `symbol` in the shape /live-quote returns, or None when there is none younger than
    max_age_s (default DHAN_LIVE_MEM_MAX_AGE_S) or the memory path is off. Only answers while Dhan is the writer."""
    try:
        limit = mem_max_age_s() if max_age_s is None else float(max_age_s)
        if limit <= 0 or not should_write():
            return None
        with _lock:
            hit = _latest.get(_clean(symbol))
        if hit is None:
            return None
        mono, wall, r = hit
        if time.monotonic() - mono > limit:
            return None
        from datetime import datetime, timezone
        with _lock:
            _stats["memory_reads"] += 1
        return {"symbol": _clean(symbol), "ltp": float(r.get("price") or 0.0), "ohlc": _ohlc_for(r),
                "volume": int(r.get("volume") or 0), "source": "dhan",
                "updated_at": datetime.fromtimestamp(wall, tz=timezone.utc).isoformat()}
    except Exception:  # noqa: BLE001
        return None


def _filter_and_order(params: List[dict]) -> List[dict]:
    """Drop rows whose price has not changed within refresh_s() (priority symbols are never dropped) and put the
    priority symbols first. Pure apart from the stats counters."""
    prio = priority_symbols()
    window = refresh_s()
    now = time.monotonic()
    first, rest, skipped = [], [], 0
    with _lock:
        for p in params:
            sym = _clean(p["s"])
            if sym in prio:
                first.append(p)
                continue
            prev = _last_sent.get(p["s"])
            if window > 0 and prev is not None and prev[0] == p["l"] and (now - prev[1]) < window:
                skipped += 1
                continue
            rest.append(p)
        if skipped:
            _stats["unchanged_skipped"] += skipped
        if first:
            _stats["priority_rows"] += len(first)
    return first + rest


def _chunks(items: list, size: int):
    size = max(1, int(size))
    for i in range(0, len(items), size):
        yield items[i:i + size]


def coalesce(batches: List[List[dict]]) -> List[dict]:
    """Merge several queued batches into one row list. The newest row per symbol wins, so a writer that fell behind
    writes each symbol once with its latest price instead of replaying every stale batch."""
    latest: dict = {}
    for rows in batches:
        for r in rows or []:
            sym = r.get("symbol")
            if sym:
                latest[str(sym)] = r
    return list(latest.values())


def _is_timeout(exc: BaseException) -> bool:
    txt = f"{type(exc).__name__} {exc}"
    return "DPY-4024" in txt or "timed out" in txt.lower() or "timeout" in txt.lower()


def _warn_throttled(msg: str, *args) -> None:
    global _last_warn
    now = time.monotonic()
    with _lock:
        if now - _last_warn < _WARN_EVERY_S:
            return
        _last_warn = now
    logger.warning(msg, *args)


def _write_sync(rows: List[dict]) -> int:
    """Upsert `rows` in chunks of DHAN_LIVE_WRITE_CHUNK, each in its own transaction. A chunk that fails (for
    example an Oracle call timeout) is counted and skipped; the remaining chunks are still written. Returns the
    number of rows written."""
    params = _filter_and_order(build_params(rows))
    if not params:
        return 0
    n_prio = sum(1 for p in params if _clean(p["s"]) in priority_symbols())
    from angelone_ws_feed import _get_live_quotes_engine, _ensure_schema
    engine, dialect = _get_live_quotes_engine()
    if engine is None:
        return 0
    _ensure_schema(engine, dialect)
    from sqlalchemy import text
    if dialect == "oracle":
        sql = ("MERGE INTO live_quotes d USING ("
               "SELECT :s AS symbol, :l AS ltp, :o AS ohlc_json, :v AS volume FROM dual"
               ") s ON (d.symbol = s.symbol) "
               "WHEN MATCHED THEN UPDATE SET d.ltp = s.ltp, d.ohlc_json = s.ohlc_json, "
               "d.volume = s.volume, d.source = 'dhan', d.updated_at = SYSTIMESTAMP "
               "WHEN NOT MATCHED THEN INSERT (symbol, ltp, ohlc_json, volume, source, updated_at) "
               "VALUES (s.symbol, s.ltp, s.ohlc_json, s.volume, 'dhan', SYSTIMESTAMP)")
    else:
        sql = ("INSERT INTO live_quotes (symbol, ltp, ohlc_json, volume, source, updated_at) "
               "VALUES (:s, :l, :o, :v, 'dhan', now()) "
               "ON CONFLICT (symbol) DO UPDATE "
               "SET ltp=EXCLUDED.ltp, ohlc_json=EXCLUDED.ohlc_json, "
               "volume=EXCLUDED.volume, source=EXCLUDED.source, updated_at=now()")
    written = 0
    last_exc = None
    stmt = text(sql)

    def _write_chunk(chunk: list, may_split: bool) -> None:
        nonlocal written, last_exc
        try:
            with engine.begin() as conn:
                conn.execute(stmt, chunk)
            written += len(chunk)
            _now_m = time.monotonic()
            with _lock:
                for p in chunk:
                    _last_sent[p["s"]] = (p["l"], _now_m)
        except Exception as e:  # noqa: BLE001 - keep writing the other chunks
            with _lock:
                for p in chunk:
                    _last_sent.pop(p["s"], None)      # a failed row must be sent again next time, not skipped as unchanged
                _stats["failed_chunks"] += 1
                if _is_timeout(e):
                    _stats["timeouts"] += 1
                _stats["last_error"] = f"{type(e).__name__}: {str(e)[:100]}"
            # LOG FIX (2026-10-12): "333 of 483 rows written ... DPY-4024 call timeout of 8000 ms". A chunk that times out is
            # retried ONCE as two halves (each its own transaction, each well inside the call timeout) instead of losing every
            # row in it until the next poll cycle.
            if may_split and _is_timeout(e) and len(chunk) >= 20:
                mid = len(chunk) // 2
                _write_chunk(chunk[:mid], False)
                _write_chunk(chunk[mid:], False)
            else:
                last_exc = e

    if n_prio:
        _write_chunk(params[:n_prio], False)        # held positions: own small transaction, written before the rest
    for chunk in _chunks(params[n_prio:], config.live_write_chunk()):
        _write_chunk(chunk, True)
    if last_exc is not None:
        _warn_throttled("dhan live_quotes write: %d of %d rows written (last error %s: %s)",
                        written, len(params), type(last_exc).__name__, str(last_exc)[:100])
    return written


def _loop() -> None:
    while True:
        rows = _q.get()
        try:
            # Anything that queued up while the previous write ran is merged into this one (newest price per
            # symbol), so a slow database is never fed stale batches one after another.
            pending = [rows]
            while True:
                try:
                    pending.append(_q.get_nowait())
                except queue.Empty:
                    break
            if len(pending) > 1:
                rows = coalesce(pending)
                with _lock:
                    _stats["coalesced_batches"] += len(pending) - 1
            n = _write_sync(rows)
            with _lock:
                _stats["batches_written"] += 1
                _stats["rows_written"] += n
        except Exception as e:  # noqa: BLE001 - the in-memory batcher cache is the primary store
            with _lock:
                _stats["last_error"] = f"{type(e).__name__}: {str(e)[:100]}"
            logger.debug("dhan live_quotes write failed: %s", type(e).__name__)


def submit(rows: List[dict]) -> None:
    """Hand a batch of rows to the writer. Never blocks, never raises; a full queue drops the OLDEST batch."""
    global _thread
    if not rows or not should_write():
        return
    _remember(rows)
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_loop, name="dhan-live-writer", daemon=True)
            _thread.start()
    try:
        _q.put_nowait(rows)
    except queue.Full:
        try:
            _q.get_nowait()
            _stats["dropped_batches"] += 1
        except queue.Empty:
            pass
        try:
            _q.put_nowait(rows)
        except queue.Full:
            pass


def status() -> dict:
    with _lock:
        return {**_stats, "write_mode": write_mode(), "writing": should_write()}
