"""execution/gap_cache.py - E276 round 53: each carried stock's own 95th-percentile adverse overnight gap, for the heat gap allowance.

peek(symbol)  -> the cached p95 gap in % (None = not known yet). No I/O, never blocks, never raises: it is called inside the risk check.
warm(symbols) -> starts ONE background thread that fetches the daily bars of symbols not cached (or whose entry expired) and stores
                 risk_engine.portfolio_heat.p95_gap_pct(opens, closes). A failed or too-short history is cached as None for FAIL_TTL_S so it is not retried every cycle.
The caller (execution/heat_state.py) uses max(HEAT_GAP_MIN_PCT, p95) when known and HEAT_GAP_DEFAULT_PCT otherwise, so a slow or failed fetch only ever means the flat default."""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Dict, Iterable, Optional, Tuple

from risk_engine import portfolio_heat as ph

logger = logging.getLogger("real-trade-gap-cache")

OK_TTL_S = 12 * 3600.0
FAIL_TTL_S = 30 * 60.0
MAX_PER_PASS = 12

_lock = threading.Lock()
_cache: Dict[str, Tuple[Optional[float], float]] = {}     # SYMBOL -> (p95 or None, expires at monotonic s)
_running = False


def _key(symbol) -> str:
    return str(symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()


def reset() -> None:
    global _running
    with _lock:
        _cache.clear()
        _running = False


def peek(symbol) -> Optional[float]:
    try:
        with _lock:
            hit = _cache.get(_key(symbol))
        return hit[0] if hit else None
    except Exception:  # noqa: BLE001
        return None


def _default_fetch(symbol: str):
    from entry_engine.structure_observation import fetch_daily_bars
    return fetch_daily_bars(symbol, 120)


def compute(symbol: str, fetch: Optional[Callable[[str], list]] = None) -> Optional[float]:
    """p95 gap for one symbol from its daily bars (ts, open, high, low, close, volume); None when unavailable. Never raises."""
    try:
        bars = (fetch or _default_fetch)(symbol) or []
        return ph.p95_gap_pct([b[1] for b in bars], [b[4] for b in bars], 20)
    except Exception as e:  # noqa: BLE001
        logger.info("gap cache: %s history unavailable (%s)", symbol, e)
        return None


def _pass(symbols: list, fetch: Optional[Callable[[str], list]]) -> None:
    global _running
    try:
        for s in symbols[:MAX_PER_PASS]:
            v = compute(s, fetch)
            ttl = OK_TTL_S if v is not None else FAIL_TTL_S
            with _lock:
                _cache[s] = (v, time.monotonic() + ttl)
    finally:
        with _lock:
            _running = False


def warm(symbols: Iterable[str], fetch: Optional[Callable[[str], list]] = None, sync: bool = False) -> bool:
    """Queue the symbols that are missing or expired. Returns True when a pass was started. sync=True runs it inline (tests)."""
    global _running
    try:
        now = time.monotonic()
        with _lock:
            if _running:
                return False
            need = []
            for s in symbols:
                k = _key(s)
                hit = _cache.get(k)
                if k and k not in need and (hit is None or hit[1] <= now):
                    need.append(k)
            if not need:
                return False
            _running = True
        if sync:
            _pass(need, fetch)
        else:
            threading.Thread(target=_pass, args=(need, fetch), daemon=True, name="gap-cache").start()
        return True
    except Exception as e:  # noqa: BLE001
        logger.warning("gap cache: warm failed (non-fatal): %s", e)
        with _lock:
            _running = False
        return False
