"""screening/prev_day.py - previous-day daily candle facts for the opening gate (group 270, 2026-10-09).

Gives screening/opening_gate.py the last COMPLETED daily candle's high / low / close and the 14-day daily ATR, from
market-data-service GET /history/{symbol}?period=1mo&interval=1d.

NO NETWORK CALL ON THE ENTRY PATH: get() only reads an in-memory per-day cache. A miss starts ONE daemon-thread fetch
(at most PREVDAY_MAX_INFLIGHT at a time, a failed symbol is not retried for PREVDAY_RETRY_S) and returns None; the opening
gate fails closed on None, so the entry is skipped this scan and the next scan (10 s later) finds the cached value.
Nothing here raises into the caller.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import List, NamedTuple, Optional

import config
from tz_utils import ist_today_str

logger = logging.getLogger("position-stocks-prev-day")

ATR_WINDOW = 14
PREVDAY_MAX_INFLIGHT = 6
PREVDAY_RETRY_S = 60.0
_INLINE = False          # tests: run the fetch on the calling thread

_lock = threading.Lock()
_cache: dict = {}        # symbol -> (ist_date, PrevDay)
_inflight: set = set()
_last_try: dict = {}


class PrevDay(NamedTuple):
    high: float
    low: float
    close: float
    atr: Optional[float]     # 14-day daily ATR in price terms, None when fewer than 15 candles
    date: str

    @property
    def close_pos(self) -> Optional[float]:
        """Where the previous day closed inside its own range: 0 = at the low, 1 = at the high. None for a zero-range day."""
        span = self.high - self.low
        if span <= 1e-9:
            return None
        return max(0.0, min(1.0, (self.close - self.low) / span))

    @property
    def atr_pct(self) -> Optional[float]:
        return (self.atr / self.close * 100.0) if self.atr and self.close > 0 else None


def reset() -> None:
    with _lock:
        _cache.clear()
        _inflight.clear()
        _last_try.clear()


def _f(v) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def parse_candles(candles: List[dict], today: str, max_age_days: Optional[int] = None) -> Optional[PrevDay]:
    """Last completed daily candle (a candle dated today is dropped) plus ATR(14); None when unusable.

    max_age_days (group 273): when given, a last candle older than that many calendar days is unusable. market-data can serve
    a stale last-good history, and a candle from last week is not "the previous day"."""
    try:
        rows = [c for c in (candles or []) if str(c.get("date") or "")[:10] and str(c.get("date"))[:10] < today]
        if not rows:
            return None
        last = rows[-1]
        if max_age_days is not None:
            from datetime import date as _date
            age = (_date.fromisoformat(today) - _date.fromisoformat(str(last.get("date"))[:10])).days
            if age > max_age_days:
                logger.info("prev_day: last candle %s is %d days old (max %d) - treated as no data", str(last.get("date"))[:10], age, max_age_days)
                return None
        h, l, c = _f(last.get("high")), _f(last.get("low")), _f(last.get("close"))
        if h <= 0 or l <= 0 or c <= 0 or h < l:
            return None
        atr = None
        if len(rows) >= ATR_WINDOW + 1:
            trs = []
            for i in range(1, len(rows)):
                hi, lo, pc = _f(rows[i].get("high")), _f(rows[i].get("low")), _f(rows[i - 1].get("close"))
                if hi > 0 and lo > 0 and pc > 0:
                    trs.append(max(hi - lo, abs(hi - pc), abs(lo - pc)))
            if len(trs) >= ATR_WINDOW:
                atr = sum(trs[-ATR_WINDOW:]) / ATR_WINDOW
        return PrevDay(high=h, low=l, close=c, atr=atr, date=str(last.get("date"))[:10])
    except Exception:
        return None


def _fetch_candles(symbol: str) -> list:
    import httpx
    sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    with httpx.Client(timeout=config.OPENING_GATE_PREVDAY_TIMEOUT_S) as client:
        r = client.get(f"{config.MARKET_DATA_URL}/history/{sym}", params={"period": "1mo", "interval": "1d"})
    if r.status_code != 200:
        raise RuntimeError(f"history HTTP {r.status_code}")
    return (r.json() or {}).get("candles") or []


def _worker(symbol: str) -> None:
    today = ist_today_str()
    try:
        pd = parse_candles(_fetch_candles(symbol), today, config.OPENING_GATE_PREVDAY_MAX_AGE_DAYS)
        if pd is not None:
            with _lock:
                _cache[symbol] = (today, pd)
        else:
            logger.info("prev_day: %s has no usable previous-day candle", symbol)
    except Exception as e:
        logger.info("prev_day: fetch for %s failed (%s: %s)", symbol, type(e).__name__, e)
    finally:
        with _lock:
            _inflight.discard(symbol)


def peek(symbol: str) -> Optional[PrevDay]:
    """Cached value for today, or None. Never starts a fetch."""
    with _lock:
        hit = _cache.get(symbol)
    return hit[1] if hit and hit[0] == ist_today_str() else None


def get(symbol: str) -> Optional[PrevDay]:
    """Cached value for today; on a miss starts a background fetch and returns None."""
    try:
        v = peek(symbol)
        if v is not None:
            return v
        now = time.monotonic()
        with _lock:
            if symbol in _inflight or len(_inflight) >= PREVDAY_MAX_INFLIGHT:
                return None
            if now - _last_try.get(symbol, -1e9) < PREVDAY_RETRY_S:
                return None
            _inflight.add(symbol)
            _last_try[symbol] = now
        if _INLINE:
            _worker(symbol)
            return peek(symbol)
        threading.Thread(target=_worker, args=(symbol,), name=f"prevday-{symbol}", daemon=True).start()
    except Exception as e:
        logger.debug("prev_day.get(%s): %s", symbol, e)
    return None
