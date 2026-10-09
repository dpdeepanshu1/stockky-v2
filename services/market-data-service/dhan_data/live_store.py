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
from typing import List

from . import config

logger = logging.getLogger("dhan-data.live-store")

_q: "queue.Queue[List[dict]]" = queue.Queue(maxsize=3)
_thread = None
_lock = threading.Lock()
_stats = {"batches_written": 0, "rows_written": 0, "dropped_batches": 0, "last_error": None,
          "coalesced_batches": 0, "failed_chunks": 0, "timeouts": 0}
_last_warn = 0.0
_WARN_EVERY_S = 60.0


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
    params = build_params(rows)
    if not params:
        return 0
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
    for chunk in _chunks(params, config.live_write_chunk()):
        try:
            with engine.begin() as conn:
                conn.execute(stmt, chunk)
            written += len(chunk)
        except Exception as e:  # noqa: BLE001 - keep writing the other chunks
            last_exc = e
            with _lock:
                _stats["failed_chunks"] += 1
                if _is_timeout(e):
                    _stats["timeouts"] += 1
                _stats["last_error"] = f"{type(e).__name__}: {str(e)[:100]}"
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
