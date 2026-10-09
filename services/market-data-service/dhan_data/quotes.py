"""dhan_data/quotes.py - live quotes through Dhan's market-quote API with ONE call per second (group 270).

Dhan allows 1000 instruments per request but only 1 request per second. This system asks for quotes one symbol at a
time (/quote) and in chunks (/quotes/bulk) from many threads, so a per-request call would be rate-limited at once.
The batcher below merges every caller into a shared pending set; a single worker thread sends ONE request per
interval carrying up to DHAN_QUOTE_MAX_BATCH symbols, stores the rows, and wakes the waiting callers.

  - on-demand symbols (a caller is waiting) go first, background symbols (the live poller) fill the rest
  - a row fresher than DHAN_QUOTE_FRESH_S is served from memory with no network
  - a caller waits at most DHAN_QUOTE_WAIT_S, then falls through to the next provider
  - any failure releases the waiters at once (they never sit out the full wait)
"""
from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Callable, Dict, Iterable, List, Optional

from . import client, config, scrip_master
from .errors import DhanError

logger = logging.getLogger("dhan-data.quotes")

_cond = threading.Condition()
_demand: "OrderedDict[str, float]" = OrderedDict()     # symbols a caller is waiting for
_bg: "OrderedDict[str, float]" = OrderedDict()         # symbols requested in the background
_rows: Dict[str, dict] = {}                            # key -> latest row (carries "_mono")
_fail: Dict[str, float] = {}                           # key -> monotonic time of the last attempt that returned nothing
_listeners: List[Callable[[List[dict]], None]] = []
_thread: Optional[threading.Thread] = None
_stop = False
_stats = {"batches": 0, "symbols_last_batch": 0, "last_batch_ms": None, "last_batch_at": None, "rows_cached": 0,
          "empty_symbols": 0, "prev_close_disagreements": 0, "rows_mapped": 0}


# ── keys ──────────────────────────────────────────────────────────────────────────────────────────────────────
def key_for(symbol: str) -> str:
    s = (symbol or "").strip().upper()
    if s.startswith("^"):
        return s
    return s.replace(".NS", "").replace(".BO", "").strip()


# ── mapping ───────────────────────────────────────────────────────────────────────────────────────────────────
def _f(v) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and math.isfinite(x) else None


_LTT_FORMATS = ("%d/%m/%Y %H:%M:%S", "%Y-%m-%d %H:%M:%S", "%d-%m-%Y %H:%M:%S", "%Y-%m-%dT%H:%M:%S")


def parse_ltt(raw) -> Optional[datetime]:
    """Dhan's last_trade_time -> aware UTC datetime. The documented text format is not guaranteed, so a value that
    does not parse is simply None (never guessed). Naive values are IST wall clock."""
    if raw in (None, "", 0):
        return None
    try:
        if isinstance(raw, (int, float)):
            v = float(raw)
            if v > 1e11:
                v /= 1000.0
            return datetime.fromtimestamp(v, tz=timezone.utc) if v > 1e8 else None
        s = str(raw).strip()
        from zoneinfo import ZoneInfo
        for fmt in _LTT_FORMATS:
            try:
                return datetime.strptime(s, fmt).replace(tzinfo=ZoneInfo("Asia/Kolkata")).astimezone(timezone.utc)
            except ValueError:
                continue
    except Exception:  # noqa: BLE001
        return None
    return None


def map_item(symbol: str, item: dict, now_mono: Optional[float] = None) -> Optional[dict]:
    """One Dhan market-quote item -> the row this service uses. None when there is no usable price."""
    if not isinstance(item, dict):
        return None
    price = _f(item.get("last_price"))
    if price is None or price <= 0:
        return None
    ohlc = item.get("ohlc") if isinstance(item.get("ohlc"), dict) else {}
    prev = _f(ohlc.get("close"))
    if prev is not None and prev <= 0:
        prev = None
    net = _f(item.get("net_change"))
    if prev is not None and net is not None and abs((price - net) - prev) > max(0.01, 0.01 * prev):
        # diagnostic for review risk R3: ohlc.close may not be yesterday's close in-session. Counted, never "fixed" silently.
        _stats["prev_close_disagreements"] += 1
    chg = round((price - prev) / prev * 100.0, 2) if prev else None
    ltt = parse_ltt(item.get("last_trade_time"))
    vol = item.get("volume")
    try:
        vol = int(float(vol)) if vol not in (None, "") else None
    except (TypeError, ValueError):
        vol = None
    row = {
        "symbol": symbol,
        "price": price,
        "previous_close": prev,
        "day_change_pct": chg,
        "open": _f(ohlc.get("open")),
        "day_high": _f(ohlc.get("high")),
        "day_low": _f(ohlc.get("low")),
        "volume": vol,
        "avg_price": _f(item.get("average_price")),
        "upper_circuit": _f(item.get("upper_circuit_limit")),
        "lower_circuit": _f(item.get("lower_circuit_limit")),
        "last_trade_at": ltt.isoformat() if ltt else None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "source": "dhan",
        "_mono": time.monotonic() if now_mono is None else now_mono,
    }
    return row


def public_row(row: dict) -> dict:
    """The row without internal fields."""
    return {k: v for k, v in row.items() if not k.startswith("_")}


# ── worker ────────────────────────────────────────────────────────────────────────────────────────────────────
def add_listener(fn: Callable[[List[dict]], None]) -> None:
    if fn not in _listeners:
        _listeners.append(fn)


def _endpoint() -> str:
    return "marketfeed/ohlc" if config.env_str("DHAN_QUOTE_ENDPOINT", "quote").lower() == "ohlc" else "marketfeed/quote"


def _ensure_thread() -> None:
    global _thread, _stop
    with _cond:
        if _thread is not None and _thread.is_alive():
            return
        _stop = False
        _thread = threading.Thread(target=_worker, name="dhan-quote-batcher", daemon=True)
        _thread.start()


def stop() -> None:
    global _stop
    with _cond:
        _stop = True
        _cond.notify_all()


def _take_batch() -> List[str]:
    cap = config.quote_max_batch()
    batch: List[str] = []
    for src in (_demand, _bg):
        while src and len(batch) < cap:
            k, _ = src.popitem(last=False)
            if k not in batch:
                batch.append(k)
    return batch


def run_one_batch(batch: List[str]) -> List[dict]:
    """Send ONE request for `batch` (keys) and store the rows. Returns the rows stored. Releases waiters either way.
    Separated from the thread loop so tests can drive it directly."""
    t0 = time.monotonic()
    rows: List[dict] = []
    try:
        by_seg: Dict[str, List[int]] = {}
        key_by_id: Dict[tuple, str] = {}
        for k in batch:
            ident = scrip_master.security_id(k)
            if not ident:
                continue
            seg, sid = ident
            by_seg.setdefault(seg, []).append(sid)
            key_by_id[(seg, sid)] = k
        if not by_seg:
            return rows
        body = client.post(_endpoint(), by_seg, limiter=client.quote_limiter)
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, dict):
            raise DhanError("quote response had no data block")
        now_m = time.monotonic()
        for seg, items in data.items():
            if not isinstance(items, dict):
                continue
            for sid_s, item in items.items():
                try:
                    k = key_by_id.get((seg, int(sid_s)))
                except ValueError:
                    continue
                if not k:
                    continue
                row = map_item(k, item, now_m)
                if row:
                    rows.append(row)
        client.note_success()
    except DhanError as e:
        client.note_failure(e)
        logger.debug("dhan quote batch of %d failed: %s", len(batch), e)
        rows = []
    except Exception as e:  # noqa: BLE001 - the worker must never die
        client.note_failure(DhanError(type(e).__name__))
        logger.warning("dhan quote batch crashed: %s: %s", type(e).__name__, str(e)[:120])
        rows = []
    finally:
        done = time.monotonic()
        with _cond:
            got = set()
            for row in rows:
                _rows[row["symbol"]] = row
                got.add(row["symbol"])
            for k in batch:
                if k not in got:
                    _fail[k] = done
            _stats["batches"] += 1
            _stats["symbols_last_batch"] = len(batch)
            _stats["last_batch_ms"] = round((done - t0) * 1000.0, 1)
            _stats["last_batch_at"] = time.time()
            _stats["rows_cached"] = len(_rows)
            _stats["rows_mapped"] += len(rows)
            _stats["empty_symbols"] += len(batch) - len(got)
            _cond.notify_all()
    for fn in list(_listeners):
        try:
            if rows:
                fn(rows)
        except Exception as e:  # noqa: BLE001
            logger.debug("dhan quote listener failed: %s", type(e).__name__)
    return rows


def _worker() -> None:
    while True:
        with _cond:
            while not _stop and not _demand and not _bg:
                _cond.wait(1.0)
            if _stop:
                return
        window = config.quote_batch_window_s()
        if window > 0:
            time.sleep(window)                     # let concurrent callers join this batch
        with _cond:
            batch = _take_batch()
        if not batch:
            continue
        if not client.available():
            with _cond:
                done = time.monotonic()
                for k in batch:
                    _fail[k] = done
                _cond.notify_all()
            time.sleep(0.2)
            continue
        run_one_batch(batch)


# ── public API ──────────────────────────────────────────────────────────────────────────────────────────────────
def request(symbols: Iterable[str], background: bool = True) -> int:
    """Queue symbols without waiting. Background symbols that already have a row fresher than the poll interval are
    skipped (so symbols the websocket keeps fresh are not re-polled). Returns how many were queued."""
    if not config.enabled():
        return 0
    skip_age = config.live_poll_interval_s() if background else 0.0
    now = time.monotonic()
    queued = 0
    with _cond:
        for s in symbols:
            k = key_for(s)
            if not k:
                continue
            row = _rows.get(k)
            if background and row is not None and (now - row["_mono"]) < skip_age:
                continue
            (_bg if background else _demand)[k] = now
            queued += 1
        if queued:
            _cond.notify_all()
    if queued:
        _ensure_thread()
    return queued


def peek(symbol: str, max_age_s: Optional[float] = None) -> Optional[dict]:
    """Latest cached row if it is younger than max_age_s (default DHAN_QUOTE_FRESH_S). No network, no waiting."""
    k = key_for(symbol)
    limit = config.quote_fresh_s() if max_age_s is None else max_age_s
    with _cond:
        row = _rows.get(k)
    if row is not None and (time.monotonic() - row["_mono"]) <= limit:
        return row
    return None


def inject_rows(rows: Iterable[dict]) -> None:
    """Websocket ticks land here so REST callers see them without another request."""
    with _cond:
        for row in rows:
            if row.get("symbol") and row.get("price"):
                _rows[row["symbol"]] = row
        _stats["rows_cached"] = len(_rows)


def get_quotes(symbols: Iterable[str], max_age_s: Optional[float] = None,
               wait_s: Optional[float] = None) -> Dict[str, dict]:
    """{key: row} for the symbols Dhan could price now. Fresh rows are served immediately; the rest wait for the next
    batch (bounded). Symbols Dhan does not know, a paused/disabled Dhan, and a timeout all just return fewer rows."""
    out: Dict[str, dict] = {}
    if not config.enabled():
        return out
    keys = list(dict.fromkeys(key_for(s) for s in symbols if key_for(s)))
    if not keys:
        return out
    limit = config.quote_fresh_s() if max_age_s is None else max_age_s
    now = time.monotonic()
    need: List[str] = []
    with _cond:
        for k in keys:
            row = _rows.get(k)
            if row is not None and (now - row["_mono"]) <= limit:
                out[k] = row
            else:
                need.append(k)
    if not need:
        return out
    if not scrip_master.ensure_loaded(block=False):
        return out                                  # no map yet: fall through to the next provider at once
    need = [k for k in need if scrip_master.security_id(k)]
    if not need or not client.available():
        return out
    start = time.monotonic()
    n_batches = max(1, math.ceil(len(need) / max(1, config.quote_max_batch())))
    base_wait = config.quote_wait_s() if wait_s is None else wait_s
    deadline = start + max(base_wait, n_batches * (client.quote_limiter.interval() + config.quote_batch_window_s()) + 2.0)
    with _cond:
        for k in need:
            _demand[k] = start
            _demand.move_to_end(k)
        _cond.notify_all()
    _ensure_thread()
    with _cond:
        while True:
            pending = [k for k in need
                       if not ((_rows.get(k) and _rows[k]["_mono"] >= start) or _fail.get(k, 0.0) >= start)]
            if not pending:
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            _cond.wait(min(remaining, 0.25))
        for k in need:
            row = _rows.get(k)
            if row is not None and row["_mono"] >= start:
                out[k] = row
    return out


def get_quote(symbol: str, **kw) -> Optional[dict]:
    return get_quotes([symbol], **kw).get(key_for(symbol))


def status() -> dict:
    with _cond:
        s = dict(_stats)
        s["pending_demand"] = len(_demand)
        s["pending_background"] = len(_bg)
    s["worker_alive"] = bool(_thread is not None and _thread.is_alive())
    return s


def _reset_for_tests() -> None:
    global _stop
    stop()
    t = _thread
    if t is not None and t.is_alive():
        t.join(timeout=2.0)
    with _cond:
        _demand.clear(); _bg.clear(); _rows.clear(); _fail.clear(); _listeners.clear()
        for k in _stats:
            _stats[k] = None if k in ("last_batch_ms", "last_batch_at") else 0
        _stop = False
