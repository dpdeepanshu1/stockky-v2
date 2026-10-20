"""group468: log-only market context recorded on every candidate (Mansfield relative strength and breadth).

NOTHING here gates, ranks or changes a trade. The numbers are written to the candidate's raw_payload under
"_log_only" so a later report can ask whether they carry any edge before anyone builds a rule on them.

Mansfield RS = (RS / SMA(RS, N) - 1) * 10, RS = stock close / benchmark close on the dates both have. Positive = the
stock is outperforming the benchmark relative to its own average. The classic form uses 52 weekly bars; the candidate
path only holds a 1y DAILY series, so N is a daily-bar count (default 200, about 40 weeks): a close cousin, not the
identical number. The bar count used is recorded with every reading.

Breadth = % of the symbols this service has read recently that close above their own 50-day EMA. It is the breadth of
the symbols the candidate engine has looked at (its universe), NOT of the whole exchange; `breadth_n` says how many.
Everything is pure except the small in-process caches; every public function never raises.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Any, Callable, Optional

logger = logging.getLogger("real-trade-candidates")

BENCH_SYMBOL = (os.getenv("CANDIDATE_LOG_CONTEXT_BENCH") or "").strip() or "^NSEI"
_BENCH_TTL_S = 1800.0
_BREADTH_MAX_AGE_S = 1800.0
_BREADTH_MIN_N = 20


def _flag_on() -> bool:
    return ((os.getenv("CANDIDATE_LOG_CONTEXT") or "").strip() or "1").lower() not in ("0", "false", "no", "off")


def _int_env(name: str, default: int) -> int:
    try:
        return max(5, int(((os.getenv(name) or "").strip() or default)))
    except (TypeError, ValueError):
        return default


def _series(candles: Any) -> dict:
    """{YYYY-MM-DD: close} from a candle list ([{date, close, ...}]). Bad rows are skipped."""
    out: dict = {}
    if not isinstance(candles, list):
        return out
    for c in candles:
        try:
            d = str((c or {}).get("date") or "")[:10]
            v = float((c or {}).get("close"))
            if d and v > 0 and v == v:
                out[d] = v
        except (TypeError, ValueError, AttributeError):
            continue
    return out


def mansfield_rs(stock: dict, bench: dict, n: int = 200) -> Optional[dict]:
    """Pure. stock / bench = {date: close}. None when fewer than `n` common dates. Returns {"value", "bars"}."""
    try:
        dates = sorted(set(stock) & set(bench))
        if n < 2 or len(dates) < n:
            return None
        rs = [stock[d] / bench[d] for d in dates]
        sma = sum(rs[-n:]) / n
        if sma <= 0:
            return None
        return {"value": round((rs[-1] / sma - 1.0) * 10.0, 3), "bars": n}
    except Exception:  # noqa: BLE001
        return None


def ema_last(closes: list, span: int = 50) -> Optional[float]:
    """Pure. Last value of an EMA (adjust=False, seeded with the first close). None below `span` closes."""
    try:
        vals = [float(x) for x in closes]
        if len(vals) < span or span < 1:
            return None
        k = 2.0 / (span + 1.0)
        e = vals[0]
        for v in vals[1:]:
            e = v * k + e * (1.0 - k)
        return e
    except Exception:  # noqa: BLE001
        return None


def above_ema(stock: dict, span: int = 50) -> Optional[bool]:
    """Pure. True / False = the last close is above / not above its EMA; None = not enough bars."""
    closes = [stock[d] for d in sorted(stock)]
    e = ema_last(closes, span)
    return None if e is None else bool(closes[-1] > e)


_breadth: dict = {}      # symbol -> (monotonic time, above_ema50)
_bench_cache: dict = {}  # {"at": monotonic, "series": {date: close}}


def clear_state() -> None:
    _breadth.clear()
    _bench_cache.clear()


def record_breadth(symbol: str, above: Optional[bool], now: Optional[float] = None) -> None:
    if above is not None and symbol:
        _breadth[str(symbol)] = (time.monotonic() if now is None else now, bool(above))


def breadth_reading(now: Optional[float] = None) -> dict:
    """Pure over the cache: {"breadth_pct", "breadth_n"}; breadth_pct is None until `_BREADTH_MIN_N` fresh readings exist."""
    t = time.monotonic() if now is None else now
    fresh = [a for (ts, a) in _breadth.values() if t - ts <= _BREADTH_MAX_AGE_S]
    if len(fresh) < _BREADTH_MIN_N:
        return {"breadth_pct": None, "breadth_n": len(fresh)}
    return {"breadth_pct": round(100.0 * sum(1 for a in fresh if a) / len(fresh), 1), "breadth_n": len(fresh)}


async def _bench_series(fetch: Callable, client: Any) -> dict:
    now = time.monotonic()
    hit = _bench_cache.get("series")
    if hit is not None and now - _bench_cache.get("at", 0.0) <= _BENCH_TTL_S:
        return hit
    series = _series(await fetch(client, BENCH_SYMBOL, "1y", "1d"))
    if series:                                   # an empty answer is not cached: the next candidate retries
        _bench_cache["series"], _bench_cache["at"] = series, now
    return series


async def context_for(fetch: Callable, client: Any, symbol: str, daily_candles: Any) -> Optional[dict]:
    """The log-only block for one candidate: {"mansfield_rs", "rs_bars", "above_ema50", "breadth_pct", "breadth_n", "bench"}.
    Also feeds the breadth cache. None when switched off (CANDIDATE_LOG_CONTEXT=0) or on any failure. Never raises."""
    try:
        if not _flag_on():
            return None
        stock = _series(daily_candles)
        above = above_ema(stock, 50)
        record_breadth(symbol, above)
        out: dict = {"mansfield_rs": None, "rs_bars": None, "above_ema50": above, "bench": BENCH_SYMBOL}
        n = _int_env("CANDIDATE_LOG_CONTEXT_RS_BARS", 200)
        if len(stock) >= n:
            rs = mansfield_rs(stock, await _bench_series(fetch, client), n)
            if rs:
                out["mansfield_rs"], out["rs_bars"] = rs["value"], rs["bars"]
        out.update(breadth_reading())
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("log-only context for %s skipped: %s: %s", symbol, type(e).__name__, e)
        return None
