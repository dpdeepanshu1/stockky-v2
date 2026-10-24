"""entry_engine/orb_bars.py - E113 (plan open item 3): close-based trigger and trigger-bar volume for the Real Trade ORB recorder. RECORD ONLY.

Position Stocks got these two fields in E104 from its tick-built 1-minute bar store. Real Trade has no such store, so the bars come from market-data-service
GET /history/{symbol}?period=5d&interval=1m (the same call the chart context uses), fetched on a background thread and read from a cache:
the entry decision NEVER waits for the network. A symbol seen for the first time has no bars yet (no new keys on that decision); later decisions do.

Fields (added to flow_json["orb"], next to the unchanged tick-based verdict): close_trigger (the first COMPLETED bar after ORB_RANGE_END_IST closed ABOVE the range
high; a wick that closes back inside is not a trigger), n_range_bars, and for that trigger bar trigger_bar_at, trigger_close, trigger_volume, range_avg_volume,
volume_ratio (trigger volume / average volume of the 09:15 .. ORB_RANGE_END_IST bars).

Honesty rule: history can lag the live minute. When the newest cached bar is older than ORB_BARS_MAX_LAG_S the fields are NOT written (a stale cache would say
"no close trigger" when the trigger bar simply had not arrived). `stats()` counts hit / miss / stale so the share of decisions that carried the fields is visible.
ORB_MODE=off stops all of it (orb_shadow never gets here); ORB_BARS_FETCH=false stops only the fetching. Nothing is ordered, blocked or resized. Never raises.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional, Tuple

import config
from tz_utils import ist_now, ist_today_str, parse_hhmm

logger = logging.getLogger("real-trade-orb-bars")


def _envf(name: str, default: float) -> float:
    try:
        v = float((os.getenv(name) or "").strip() or default)
        return v if math.isfinite(v) and v > 0 else default
    except ValueError:
        return default


FETCH_ON = ((os.getenv("ORB_BARS_FETCH") or "").strip() or "true").lower() not in ("0", "false", "no", "off")
REFRESH_S = _envf("ORB_BARS_REFRESH_S", 60.0)          # a symbol's bars are re-fetched at most this often
MAX_LAG_S = _envf("ORB_BARS_MAX_LAG_S", 180.0)         # newest cached bar older than this: write no fields
TIMEOUT_S = _envf("ORB_BARS_TIMEOUT_S", 4.0)
MAX_INFLIGHT = int(_envf("ORB_BARS_MAX_INFLIGHT", 3.0))
MAX_CACHE = 600

_lock = threading.Lock()
_cache: Dict[Tuple[str, str], Dict[str, Any]] = {}     # (symbol, IST day) -> {"bars": [(t,o,h,l,c,v)...], "at": monotonic}
_last_try: Dict[Tuple[str, str], float] = {}
_inflight: set = set()
_stats: Dict[str, int] = {"requests": 0, "ok": 0, "failed": 0, "hit": 0, "miss": 0, "stale": 0}


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if math.isfinite(x) else None


def _t(name: str, dh: int, dm: int):
    return parse_hhmm(str(getattr(config, name, "") or ""), dh, dm)


def _fetch_rows(symbol: str) -> list:
    """The raw 1-minute candle rows (market-data answer). Replaced in tests."""
    import httpx
    from market_feed.feed import MARKET_DATA_URL
    sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    with httpx.Client(timeout=TIMEOUT_S) as client:
        r = client.get(f"{MARKET_DATA_URL}/history/{sym}", params={"period": "5d", "interval": "1m"})
    if r.status_code != 200:
        raise RuntimeError(f"history HTTP {r.status_code}")
    return (r.json() or {}).get("candles") or []


_fetch: Callable[[str], list] = _fetch_rows


def _worker(key: Tuple[str, str]) -> None:
    try:
        from stockky_ms.timeframes import rows_to_bars
        bars = rows_to_bars(_fetch(key[0]))
        with _lock:
            _cache[key] = {"bars": bars, "at": time.monotonic()}
            _stats["ok"] += 1
    except Exception as e:  # noqa: BLE001 - a failed fetch only means no fields; the next refresh window retries
        with _lock:
            _stats["failed"] += 1
        logger.debug("orb bars fetch failed for %s: %s", key[0], e)
    finally:
        with _lock:
            _inflight.discard(key)


def get_bars(symbol: str, now_mono: Optional[float] = None, now: Optional[datetime] = None) -> Optional[List[tuple]]:
    """Cached bars for today, or None. Never blocks: when the cache is missing or older than REFRESH_S it starts ONE background fetch (bounded by MAX_INFLIGHT)."""
    try:
        sym = str(symbol or "").strip().upper()
        if not sym:
            return None
        key = (sym, ist_today_str(now))                         # the same clock the verdict uses
        mono = time.monotonic() if now_mono is None else now_mono
        start = False
        with _lock:
            got = _cache.get(key)
            if FETCH_ON and key not in _inflight and len(_inflight) < MAX_INFLIGHT and mono - _last_try.get(key, -1e9) >= REFRESH_S:
                _last_try[key] = mono
                _inflight.add(key)
                _stats["requests"] += 1
                start = True
                if len(_cache) > MAX_CACHE or len(_last_try) > MAX_CACHE * 4:
                    for k in [k for k in _cache if k[1] != key[1]]:
                        _cache.pop(k, None)
                    for k in [k for k in _last_try if k[1] != key[1]]:
                        _last_try.pop(k, None)
        if start:
            threading.Thread(target=_worker, args=(key,), name="orb-bars", daemon=True).start()
        return list(got["bars"]) if got else None
    except Exception as e:  # noqa: BLE001
        logger.debug("orb get_bars failed: %s", e)
        return None


def bar_fields(symbol: str, rh: float, now: Optional[datetime] = None, bars: Optional[Any] = None) -> Dict[str, Any]:
    """The record-only fields from `bars` (rows of (t, o, h, l, c, v), t = epoch seconds of the bar start). Same rules as Position Stocks E104.
    {} when there are no bars or no range high. A bar that is still forming is never used; bars before 09:15 are ignored; one unreadable row is skipped."""
    try:
        if not bars or rh is None:
            return {}
        n = ist_now(now)
        now_ts = n.timestamp()
        re_t = _t("ORB_RANGE_END_IST", 9, 20)
        open_ts = n.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
        end_ts = n.replace(hour=re_t.hour, minute=re_t.minute, second=0, microsecond=0).timestamp()
        rows = []
        for b in bars:
            try:
                t, c, v = _f(b[0]), _f(b[4]), _f(b[5])
            except (TypeError, IndexError, KeyError):
                continue
            if t is not None and c is not None:
                rows.append((t, c, v))
        range_vols: List[float] = []
        trigger = None
        for t, c, v in sorted(rows, key=lambda r: r[0]):
            if t < open_ts or t + 60 > now_ts:                                   # before the open, or the bar is still forming
                continue
            if t < end_ts:
                if v is not None and v >= 0:
                    range_vols.append(v)
            elif trigger is None and c > rh:
                trigger = (t, c, v)
        out: Dict[str, Any] = {"close_trigger": trigger is not None, "n_range_bars": len(range_vols)}
        if trigger is not None:
            t, c, v = trigger
            out.update(trigger_bar_at=datetime.fromtimestamp(t, n.tzinfo).strftime("%H:%M"), trigger_close=round(c, 2))
            avg = sum(range_vols) / len(range_vols) if range_vols else 0.0
            if v is not None and avg > 0:
                out.update(trigger_volume=int(v), range_avg_volume=int(round(avg)), volume_ratio=round(v / avg, 2))
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("orb bar_fields failed: %s", e)
        return {}


def _newest_bar_end(bars: Any) -> Optional[float]:
    best = None
    for b in bars or []:
        try:
            t = _f(b[0])
        except (TypeError, IndexError, KeyError):
            continue
        if t is not None and (best is None or t > best):
            best = t
    return None if best is None else best + 60.0


def fields_for(symbol: str, rh: float, now: Optional[datetime] = None, bars: Optional[Any] = None) -> Dict[str, Any]:
    """What orb_shadow merges into the verdict: the bar fields, or {} (no cache yet, stale cache, nothing readable). Counts hit / miss / stale. Never raises."""
    try:
        if bars is None:
            bars = get_bars(symbol, now=now)
        if not bars:
            with _lock:
                _stats["miss"] += 1
            return {}
        end = _newest_bar_end(bars)
        if end is None or end < ist_now(now).timestamp() - MAX_LAG_S:
            with _lock:
                _stats["stale"] += 1
            return {}
        out = bar_fields(symbol, rh, now, bars)
        with _lock:
            _stats["hit" if out else "miss"] += 1
        return out
    except Exception as e:  # noqa: BLE001
        logger.debug("orb fields_for failed: %s", e)
        return {}


def stats() -> Dict[str, int]:
    with _lock:
        return dict(_stats)


def _reset_for_tests() -> None:
    global _fetch
    with _lock:
        _cache.clear(); _last_try.clear(); _inflight.clear()
        for k in _stats:
            _stats[k] = 0
    _fetch = _fetch_rows
