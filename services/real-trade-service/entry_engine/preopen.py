"""entry_engine/preopen.py - 09:08 pre-open snapshot: indicative gap of the queued candidates + cache warm-up (group 330, 2026-10-12).

WHY: between 09:00 and 09:15 NSE runs its pre-open call auction. The indicative price at ~09:08 already shows which queued names gap
hard (a +6 % gap is a chase; a -3 % gap is a falling stock) and the system sat idle until 09:15 and then paid for cold caches (previous-day
candles, results dates, VIX) on the first entry cycle. Two jobs, both best-effort and never on the trading path:
  1. WARM  : previous-day candles for the queued symbols, the VIX reading and the results-date / negative-news lookup (capped).
  2. GAP   : record price vs previous close per queued symbol IF the price is fresh (age <= PREOPEN_MAX_PRICE_AGE_S). gap_reason() then
             keeps a candidate that gapped up >= PREOPEN_GAP_UP_BLOCK_PCT or down >= PREOPEN_GAP_DOWN_BLOCK_PCT out until
             PREOPEN_GAP_GATE_UNTIL_IST (default 11:00). It is a gate, not a forecast: it only refuses to chase / catch.

HONEST LIMIT: whether the market-data feed has a fresh indicative price for a symbol at 09:08 depends on the broker feed. A symbol with no
fresh price simply has no gap recorded and nothing changes for it. Everything fails open. In-memory, per IST day (a restart after 09:15 just
means no snapshot that day). Off with PREOPEN_SNAPSHOT_ENABLED=false.
"""
from __future__ import annotations

import asyncio
import logging
import math
import threading
from datetime import datetime, timezone
from typing import Optional

import config
from tz_utils import ist_now, ist_time_at_or_after, ist_today_str, is_nse_holiday, parse_hhmm

logger = logging.getLogger("real-trade-preopen")

_lock = threading.Lock()
_snap: dict = {"day": None, "gaps": {}, "captured_at": None, "warmed": 0}


def reset() -> None:
    with _lock:
        _snap.update({"day": None, "gaps": {}, "captured_at": None, "warmed": 0})


def gap_pct(price, prev_close) -> Optional[float]:
    try:
        p, c = float(price), float(prev_close)
        if not (math.isfinite(p) and math.isfinite(c)) or p <= 0 or c <= 0:
            return None
        g = (p - c) / c * 100.0
        return g if abs(g) <= 60.0 else None          # beyond any circuit band: the previous close is wrong
    except (TypeError, ValueError):
        return None


def due(now: Optional[datetime] = None) -> bool:
    """True from PREOPEN_SNAPSHOT_TIME_IST until the 09:15 bell on a trading day when today's snapshot has not been taken. Never raises."""
    try:
        if not config.PREOPEN_SNAPSHOT_ENABLED:
            return False
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(n):
            return False
        with _lock:
            if _snap["day"] == ist_today_str(now):
                return False
        return (ist_time_at_or_after(parse_hhmm(config.PREOPEN_SNAPSHOT_TIME_IST, 9, 8), now)
                and not ist_time_at_or_after(parse_hhmm("09:15", 9, 15), now))
    except Exception:  # noqa: BLE001
        return False


def record(ticks: dict, now: Optional[datetime] = None) -> int:
    """Store the gap of every tick whose price is fresh; returns how many were stored. Never raises."""
    n_ok = 0
    try:
        now_utc = now or datetime.now(timezone.utc)
        gaps = {}
        for sym, t in (ticks or {}).items():
            try:
                if not getattr(t, "age_known", True):
                    continue
                as_of = t.as_of if t.as_of.tzinfo else t.as_of.replace(tzinfo=timezone.utc)
                if (now_utc - as_of).total_seconds() > float(config.PREOPEN_MAX_PRICE_AGE_S):
                    continue
                g = gap_pct(t.price, getattr(t, "prev_close", None))
                if g is not None:
                    gaps[str(sym).upper()] = g
            except Exception:  # noqa: BLE001
                continue
        with _lock:
            _snap.update({"day": ist_today_str(now), "gaps": gaps, "captured_at": now_utc.isoformat()})
        n_ok = len(gaps)
    except Exception as e:  # noqa: BLE001
        logger.debug("preopen.record failed (ignored): %s", e)
    return n_ok


def status() -> dict:
    with _lock:
        return {"day": _snap["day"], "symbols_with_gap": len(_snap["gaps"]), "captured_at": _snap["captured_at"],
                "warmed": _snap["warmed"], "gaps": dict(sorted(_snap["gaps"].items(), key=lambda kv: -abs(kv[1]))[:15])}


def gap_reason(symbol: str, now: Optional[datetime] = None) -> Optional[str]:
    """A "PREOPEN_GAP_UP/DOWN:..." reason while the gate window is open, else None. Never raises."""
    try:
        if not config.PREOPEN_SNAPSHOT_ENABLED:
            return None
        with _lock:
            if _snap["day"] != ist_today_str(now):
                return None
            g = _snap["gaps"].get(str(symbol or "").upper().replace(".NS", "").strip())
        if g is None:
            return None
        if ist_time_at_or_after(parse_hhmm(config.PREOPEN_GAP_GATE_UNTIL_IST, 11, 0), now):
            return None
        up, dn = float(config.PREOPEN_GAP_UP_BLOCK_PCT or 0), float(config.PREOPEN_GAP_DOWN_BLOCK_PCT or 0)
        if up > 0 and g >= up:
            return f"PREOPEN_GAP_UP: indicative open {g:+.1f}% vs previous close >= +{up:.1f}% - not chasing a big gap-up"
        if dn > 0 and g <= -dn:
            return f"PREOPEN_GAP_DOWN: indicative open {g:+.1f}% vs previous close <= -{dn:.1f}% - not catching a gap-down"
        return None
    except Exception:  # noqa: BLE001
        return None


def _warm_blocking(symbols: list) -> int:
    n = 0
    try:
        from entry_engine import event_risk, prev_day, vix_gate
        prev_day.prefetch(symbols)                      # background threads, never blocks
        vix_gate.snapshot()
        for sym in symbols[: max(0, int(config.PREOPEN_WARM_MAX_SYMBOLS))]:
            event_risk.lookup(sym)                      # one cached call per symbol per day (also fills the news verdict)
            n += 1
    except Exception as e:  # noqa: BLE001
        logger.debug("preopen warm failed (ignored): %s", e)
    return n


async def run(symbols: list) -> dict:
    """Take the snapshot for `symbols` and warm the caches. Never raises; the snapshot day is set even when nothing priced, so it runs once."""
    try:
        from market_feed.feed import get_quotes
        syms = sorted({str(s).upper() for s in (symbols or []) if s})
        ticks = await get_quotes(syms) if syms else {}
        stored = record(ticks)
        warmed = await asyncio.to_thread(_warm_blocking, syms)
        with _lock:
            if _snap["day"] != ist_today_str():
                _snap.update({"day": ist_today_str(), "gaps": {}, "captured_at": datetime.now(timezone.utc).isoformat()})
            _snap["warmed"] = warmed
        logger.info("[preopen] snapshot: %d queued symbols, %d with a fresh gap, %d warmed", len(syms), stored, warmed)
        return status()
    except Exception as e:  # noqa: BLE001
        logger.warning("[preopen] snapshot failed (ignored): %s", e)
        with _lock:
            _snap["day"] = ist_today_str()
        return status()
