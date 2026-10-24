"""timemachine/data.py - E111: recent 5-minute sessions of one stock from market-data-service GET /history/{symbol}, cached in memory.

Read-only. A failed fetch raises DataError (code + message); nothing here ever guesses a bar. The fetcher is injectable so tests need no network."""
from __future__ import annotations

import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote

from timemachine import engine

Fetcher = Callable[[str, str], List[dict]]          # (symbol, lookback) -> candle rows
_SYMBOL_RE = re.compile(r"^[A-Z0-9&\-]{1,20}$")
CACHE_TTL_S = 600.0
CACHE_MAX = 64
LOOKBACKS = ("1mo", "2mo")                          # 2mo asks market-data for an exact ~58-day window; if that fails the 1mo answer is used
_cache: Dict[Tuple[str, str], Tuple[float, Dict[str, List[engine.Bar]], str]] = {}
_lock = threading.Lock()
_stats = {"hits": 0, "misses": 0, "failures": 0}


class DataError(Exception):
    def __init__(self, code: str, message: str, status: int = 502):
        super().__init__(message)
        self.code, self.message, self.status = code, message, status


def normalize_symbol(raw: Any) -> str:
    s = str(raw or "").strip().upper().replace(".NS", "").replace(".BO", "")
    if not _SYMBOL_RE.match(s):
        raise DataError("BAD_SYMBOL", f"{raw!r} is not a valid NSE symbol", 400)
    return s


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def cache_stats() -> Dict[str, int]:
    with _lock:
        return {"entries": len(_cache), **_stats}


def http_fetcher(symbol: str, lookback: str) -> List[dict]:
    """The real fetch (httpx, like feed/bar_backfill). Raises DataError on anything but a usable answer."""
    import httpx
    import config
    params: Dict[str, Any] = {"interval": "5m"}
    if lookback == "2mo":
        params["days"] = 58
    else:
        params["period"] = "1mo"
    try:
        with httpx.Client(timeout=float(getattr(config, "TIME_MACHINE_FETCH_TIMEOUT_S", 25.0))) as client:
            r = client.get(f"{config.MARKET_DATA_URL}/history/{quote(symbol, safe='&')}", params=params)
    except Exception as e:  # noqa: BLE001
        raise DataError("DATA_UNREACHABLE", f"market-data-service did not answer ({type(e).__name__})", 502)
    if r.status_code == 404:
        raise DataError("NO_SUCH_SYMBOL", f"market-data-service has no history for {symbol}", 404)
    if r.status_code != 200:
        raise DataError("DATA_ERROR", f"market-data-service answered HTTP {r.status_code}", 502)
    try:
        body = r.json()
    except ValueError:
        raise DataError("DATA_ERROR", "market-data-service sent unreadable data", 502)
    return list(body.get("candles") or []) if isinstance(body, dict) else []


def load_sessions(symbol: str, lookback: str = "1mo", fetch: Optional[Fetcher] = None, now: Optional[Callable[[], float]] = None
                  ) -> Tuple[Dict[str, List[engine.Bar]], Dict[str, Any]]:
    """({day: bars} of COMPLETE sessions, info). info = {lookback_used, cached, fallback, rows}. Raises DataError."""
    sym = normalize_symbol(symbol)
    lb = lookback if lookback in LOOKBACKS else "1mo"
    clock = now or time.monotonic
    key = (sym, lb)
    with _lock:
        hit = _cache.get(key)
        if hit and clock() - hit[0] < CACHE_TTL_S:
            _stats["hits"] += 1
            return hit[1], {"lookback_used": hit[2], "cached": True, "fallback": hit[2] != lb, "rows": sum(len(v) for v in hit[1].values())}
        _stats["misses"] += 1
    f = fetch or http_fetcher

    def _fail() -> None:
        with _lock:
            _stats["failures"] += 1

    used, fallback = lb, False
    try:
        days = engine.complete_sessions(engine.parse_candles(f(sym, lb)))
        if not days and lb == "2mo":
            raise DataError("NO_BARS", "no usable 5-minute sessions in the 2-month window")
    except DataError as e:
        if lb == "2mo" and e.code not in ("NO_SUCH_SYMBOL", "BAD_SYMBOL"):
            used, fallback = "1mo", True
            try:
                days = engine.complete_sessions(engine.parse_candles(f(sym, "1mo")))
            except DataError:
                _fail()
                raise
        else:
            _fail()
            raise
    if not days:
        _fail()
        raise DataError("NO_BARS", f"no complete 5-minute sessions for {sym}: the data source may not serve 5-minute history for it", 404)
    with _lock:
        if len(_cache) >= CACHE_MAX:
            oldest = min(_cache, key=lambda k: _cache[k][0])
            _cache.pop(oldest, None)
        _cache[key] = (clock(), days, used)
    return days, {"lookback_used": used, "cached": False, "fallback": fallback, "rows": sum(len(v) for v in days.values())}
