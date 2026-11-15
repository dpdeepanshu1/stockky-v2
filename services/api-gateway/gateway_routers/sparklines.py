"""E557 (round 539, B3): GET /market/sparklines?symbols=A,B,C&days=30 - the last closes per symbol for the row sparklines on Hot, Surprise and IPO.
Read-only, no login (not in admin_guard.PROTECTED, like the other market reads). Prices come only through market-data-service /history (Dhan -> AngelOne -> yfinance
is decided there). Bounded on every side: at most MAX_SYMBOLS names per call, one short request each on a small pool, one overall deadline, and a short in-process
cache (a miss is remembered too, so a delisted name is not asked again on every refresh). A name with no usable history is simply left out: the row shows no line,
never a made-up one. Switch: SPARKLINES_ENABLED=0 answers an empty set.
"""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as _Timeout
from urllib.parse import quote as _urlquote

import httpx
from fastapi import APIRouter

logger = logging.getLogger("api-gateway")

sparklines_router = APIRouter()

MAX_SYMBOLS = 40
MIN_POINTS = 5
_SYMBOL_OK = re.compile(r"[A-Z0-9][A-Z0-9&.\-]{0,19}")
_cache: dict = {}
_cache_lock = threading.Lock()


def _env_float(name: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return default
    return v if math.isfinite(v) and lo <= v <= hi else default


def _enabled() -> bool:
    return (os.getenv("SPARKLINES_ENABLED") or "").strip().lower() not in ("0", "false", "no", "off")


def clean_symbols(raw: str) -> list:
    """Upper-cased, de-duplicated, order kept, only plausible NSE tickers (a path-like or oversized name is dropped), at most MAX_SYMBOLS."""
    out: list = []
    for part in (raw or "").split(","):
        s = part.strip().upper()
        if s.endswith(".NS"):
            s = s[:-3]
        if s and _SYMBOL_OK.fullmatch(s) and ".." not in s and s not in out:
            out.append(s)
        if len(out) >= MAX_SYMBOLS:
            break
    return out


def closes_from_candles(candles, days: int):
    """The last `days` finite positive closes (oldest first, 2 decimals), or None when fewer than MIN_POINTS are usable."""
    vals: list = []
    for c in candles or []:
        try:
            v = float((c or {}).get("close"))
        except (TypeError, ValueError, AttributeError):
            continue
        if math.isfinite(v) and v > 0:
            vals.append(round(v, 2))
    vals = vals[-max(MIN_POINTS, days):]
    return vals if len(vals) >= MIN_POINTS else None


def _fetch_one(symbol: str, days: int):
    base = (os.getenv("MARKET_DATA_URL") or "").strip().rstrip("/") or "http://market-data-service:8001"
    try:
        r = httpx.get(f"{base}/history/{_urlquote(symbol, safe='')}", params={"period": "3mo", "interval": "1d"}, timeout=6)
        if r.status_code != 200:
            return None
        return closes_from_candles((r.json() or {}).get("candles"), days)
    except Exception as e:  # noqa: BLE001
        logger.debug("sparkline %s: %s", symbol, e)
        return None


def collect(symbols: list, days: int) -> dict:
    """{SYMBOL: [closes]} for the names that have a usable history. Never raises; waits at most SPARKLINES_DEADLINE_S in all."""
    ttl = _env_float("SPARKLINES_TTL_S", 1800.0, 0.0, 86400.0)
    miss_ttl = min(ttl, 600.0)
    now = time.time()
    out: dict = {}
    todo: list = []
    with _cache_lock:
        for s in symbols:
            hit = _cache.get((s, days))
            if hit and hit[0] > now:
                if hit[1]:
                    out[s] = hit[1]
            else:
                todo.append(s)
    if todo:
        deadline = now + _env_float("SPARKLINES_DEADLINE_S", 8.0, 1.0, 30.0)
        pool = ThreadPoolExecutor(max_workers=min(8, len(todo)))
        try:
            futs = {s: pool.submit(_fetch_one, s, days) for s in todo}
            for s, fut in futs.items():
                try:
                    vals = fut.result(timeout=max(0.05, deadline - time.time()))
                except _Timeout:
                    continue          # too slow this time: left out and NOT cached, the next call tries again
                except Exception:  # noqa: BLE001
                    continue
                with _cache_lock:
                    if len(_cache) > 2000:
                        _cache.clear()
                    _cache[(s, days)] = (time.time() + (ttl if vals else miss_ttl), vals)
                if vals:
                    out[s] = vals
        finally:
            pool.shutdown(wait=False)
    return out


@sparklines_router.get("/market/sparklines")
async def market_sparklines(symbols: str = "", days: int = 30):
    """Last closes per symbol for the row sparklines. `series` holds only names with a usable history; `missing` lists the rest."""
    days = max(MIN_POINTS, min(int(days or 30), 60))
    names = clean_symbols(symbols)
    if not names or not _enabled():
        return {"days": days, "series": {}, "missing": names, "enabled": _enabled()}
    series = await asyncio.to_thread(collect, names, days)
    return {"days": days, "series": series, "missing": [s for s in names if s not in series], "enabled": True}
