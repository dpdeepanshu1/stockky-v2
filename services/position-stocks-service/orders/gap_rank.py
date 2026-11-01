"""gap_rank.py - E285 round 103: where today's gap ranks among the stock's OWN overnight gaps (the facts `gap_pctile` of setup_engine). The same file lives in
position-stocks-service/orders and real-trade-service/entry_engine (IDENTICAL in scripts/check_shared_copies.py).

WHY: setup_engine halves a setup's size when the gap is above the 95th percentile of the stock's own gaps (GAP_HALF_PCTILE), but nothing ever computed `gap_pctile`, so
the rule never fired: a +3 % gap on a stock that gaps +3 % every other week was treated like a +3 % gap on a stock that normally moves 0.4 %.

HOW: warm(symbols) starts ONE background thread that fetches each symbol's daily bars (structure_observation.fetch_daily_bars, market-data /history) and keeps its last
GAP_RANK_SESSIONS (60) overnight gaps for the IST day. Today's own bar is dropped, so the gap is never ranked against itself. pctile(symbol, gap_pct) is a cache read:
no I/O, never blocks, never raises; None = not known (history not loaded yet, fewer than GAP_RANK_MIN_SESSIONS (30) gaps, switch off) and then nothing changes (the old size).
A failed or too-short history is cached as unknown for FAIL_TTL_S so it is not retried every cycle. Switch: GAP_RANK_ENABLED.
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Iterable, List, Optional, Tuple

import config

try:
    from entry_engine import adaptive_params as _ap          # real-trade-service
except ImportError:                                          # position-stocks-service
    from orders import adaptive_params as _ap

logger = logging.getLogger("gap-rank")

OK_TTL_S = 12 * 3600.0
FAIL_TTL_S = 30 * 60.0
LOOKBACK_DAYS = 120            # calendar days of daily bars: about 80 sessions, enough for 60 gaps

_lock = threading.Lock()
_cache: Dict[str, Tuple[Optional[List[float]], float]] = {}      # SYMBOL -> (own gaps in % or None, expires at monotonic s)
_running = False


def _key(symbol) -> str:
    return str(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


def reset() -> None:
    global _running
    with _lock:
        _cache.clear()
        _running = False


def enabled() -> bool:
    return bool(getattr(config, "GAP_RANK_ENABLED", True))


def _sessions() -> int:
    try:
        return max(10, int(getattr(config, "GAP_RANK_SESSIONS", 60)))
    except (TypeError, ValueError):
        return 60


def _min_sessions() -> int:
    try:
        return max(5, int(getattr(config, "GAP_RANK_MIN_SESSIONS", 30)))
    except (TypeError, ValueError):
        return 30


def _default_fetch(symbol: str):
    try:
        from entry_engine.structure_observation import fetch_daily_bars
    except ImportError:
        from orders.structure_observation import fetch_daily_bars
    return fetch_daily_bars(symbol, LOOKBACK_DAYS)


def _ist_day(ts: float) -> int:
    return int((float(ts) + 19800.0) // 86400)


def compute(symbol: str, fetch: Optional[Callable[[str], list]] = None, now_ts: Optional[float] = None) -> Optional[List[float]]:
    """The stock's own overnight gaps (%), oldest first, today's bar dropped; None when the bars are unavailable or give fewer than GAP_RANK_MIN_SESSIONS gaps. Never raises."""
    try:
        bars = (fetch or _default_fetch)(symbol) or []
        today = _ist_day(now_ts if now_ts is not None else time.time())
        bars = [b for b in bars if _ist_day(b[0]) < today]
        gaps = _ap.overnight_gaps([b[1] for b in bars], [b[4] for b in bars], _sessions())
        return gaps if len(gaps) >= _min_sessions() else None
    except Exception as e:  # noqa: BLE001
        logger.info("gap rank: %s history unavailable (%s)", symbol, e)
        return None


def load(symbol: str, fetch: Optional[Callable[[str], list]] = None) -> Optional[List[float]]:
    """Fetch and cache one symbol's gaps (blocking). Never raises."""
    k = _key(symbol)
    if not k:
        return None
    gaps = compute(k, fetch)
    with _lock:
        _cache[k] = (gaps, time.monotonic() + (OK_TTL_S if gaps is not None else FAIL_TTL_S))
    return gaps


def _pass(symbols: List[str], fetch: Optional[Callable[[str], list]]) -> None:
    global _running
    try:
        for s in symbols:
            load(s, fetch)
    finally:
        with _lock:
            _running = False


def warm(symbols: Iterable[str], fetch: Optional[Callable[[str], list]] = None, sync: bool = False, limit: int = 40) -> bool:
    """Queue the symbols that are missing or expired (at most `limit` per pass). True when a pass was started. sync=True runs it inline (tests). Never raises."""
    global _running
    try:
        if not enabled():
            return False
        now = time.monotonic()
        with _lock:
            if _running:
                return False
            need: List[str] = []
            for s in symbols or ():
                k = _key(s)
                hit = _cache.get(k)
                if k and k not in need and (hit is None or hit[1] <= now):
                    need.append(k)
                if len(need) >= max(1, int(limit)):
                    break
            if not need:
                return False
            _running = True
        if sync:
            _pass(need, fetch)
        else:
            threading.Thread(target=_pass, args=(need, fetch), daemon=True, name="gap-rank").start()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("gap rank: warm failed (non-fatal): %s", e)
        with _lock:
            _running = False
        return False


def pctile(symbol: str, gap_pct) -> Optional[float]:
    """Today's gap % ranked among the stock's own gaps, 0..1; None = not known. Cache read only; asks warm() for the symbol when it is not cached yet. Never raises."""
    try:
        if not enabled() or gap_pct is None:
            return None
        k = _key(symbol)
        with _lock:
            hit = _cache.get(k)
        if hit is None or hit[1] <= time.monotonic():
            warm([k], limit=1)                       # background; this entry has no rank, the next cycle has it
        if hit is None or hit[0] is None:
            return None
        return _ap.gap_pctile(gap_pct, hit[0], _min_sessions())
    except Exception:  # noqa: BLE001
        return None


def status() -> dict:
    with _lock:
        known = sum(1 for v in _cache.values() if v[0] is not None)
        return {"enabled": enabled(), "cached": len(_cache), "with_history": known, "running": _running}
