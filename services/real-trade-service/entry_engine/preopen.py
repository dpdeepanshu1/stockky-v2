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
_snap: dict = {"day": None, "gaps": {}, "captured_at": None, "warmed": 0, "passes": [], "coverage": [], "warm_done": False}


def reset() -> None:
    with _lock:
        _snap.update({"day": None, "gaps": {}, "captured_at": None, "warmed": 0, "passes": [], "coverage": [], "warm_done": False})


def _pass_times() -> list:
    """The snapshot pass times (HH:MM, before the 09:15 bell). Default: PREOPEN_SNAPSHOT_TIME_IST (09:10), 09:12, 09:14 - a symbol with no fresh price
    at the first pass gets two more chances as the pre-open auction settles. PREOPEN_SNAPSHOT_PASSES_IST (comma list) overrides."""
    raw = (getattr(config, "PREOPEN_SNAPSHOT_PASSES_IST", "") or "").strip()
    labels = [x.strip() for x in raw.split(",") if x.strip()] if raw else [config.PREOPEN_SNAPSHOT_TIME_IST, "09:12", "09:14"]
    return labels or [config.PREOPEN_SNAPSHOT_TIME_IST]


def _reached(now=None) -> list:
    return [lab for lab in _pass_times() if ist_time_at_or_after(parse_hhmm(lab, 9, 10), now)]


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
    """True before the 09:15 bell on a trading day while a snapshot pass whose time has come has not been taken yet. Never raises."""
    try:
        if not config.PREOPEN_SNAPSHOT_ENABLED:
            return False
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(n):
            return False
        if ist_time_at_or_after(parse_hhmm("09:15", 9, 15), now):
            return False
        with _lock:
            done = set(_snap["passes"]) if _snap["day"] == ist_today_str(now) else set()
        return any(lab not in done for lab in _reached(now))
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
        day = ist_today_str(now)
        with _lock:
            if _snap["day"] == day:
                merged = dict(_snap["gaps"])
                merged.update(gaps)                  # a later pass overrides an earlier reading, never drops one
                gaps_all, passes = merged, set(_snap["passes"])
            else:
                gaps_all, passes = gaps, set()
                _snap.update({"coverage": [], "warm_done": False, "warmed": 0})      # a new IST day starts clean
            passes.update(_reached(now))
            _snap.update({"day": day, "gaps": gaps_all, "captured_at": now_utc.isoformat(), "passes": sorted(passes)})
        n_ok = len(gaps)
    except Exception as e:  # noqa: BLE001
        logger.debug("preopen.record failed (ignored): %s", e)
    return n_ok


def status() -> dict:
    with _lock:
        return {"day": _snap["day"], "symbols_with_gap": len(_snap["gaps"]), "captured_at": _snap["captured_at"],
                "warmed": _snap["warmed"], "passes_done": list(_snap["passes"]), "coverage": list(_snap["coverage"]), "gaps": dict(sorted(_snap["gaps"].items(), key=lambda kv: -abs(kv[1]))[:15])}


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


def _warm_blocking(symbols: list, *, _clock=None) -> int:
    """Warm the prev-day / VIX / event caches for the queued symbols. group344: bounded by PREOPEN_WARM_BUDGET_S and stopped at the
    market open, so it cannot hold the event service while a live entry check needs it. `_clock` is for tests."""
    import time as _t
    clock = _clock or _t.monotonic
    n = 0
    try:
        from entry_engine import event_risk, prev_day, vix_gate
        prev_day.prefetch(symbols)                      # background threads, never blocks
        vix_gate.snapshot()
        budget = float(getattr(config, "PREOPEN_WARM_BUDGET_S", 0.0) or 0.0)
        stop_at_open = bool(getattr(config, "PREOPEN_WARM_STOP_AT_OPEN", True))
        start = clock()
        for sym in symbols[: max(0, int(config.PREOPEN_WARM_MAX_SYMBOLS))]:
            if budget > 0 and clock() - start >= budget:
                logger.info("[preopen] warm-up stopped after %.0fs budget: %d of %d symbols warmed", budget, n,
                            min(len(symbols), int(config.PREOPEN_WARM_MAX_SYMBOLS)))
                break
            if stop_at_open and ist_time_at_or_after(parse_hhmm("09:15", 9, 15)):
                logger.info("[preopen] warm-up stopped at the market open: %d symbol(s) warmed", n)
                break
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
        with _lock:
            first_pass = not (_snap["day"] == ist_today_str() and _snap["warm_done"])
        ticks = await get_quotes(syms) if syms else {}
        stored = record(ticks)
        warmed = await asyncio.to_thread(_warm_blocking, syms) if first_pass else 0
        with _lock:
            if _snap["day"] != ist_today_str():
                _snap.update({"day": ist_today_str(), "gaps": {}, "captured_at": datetime.now(timezone.utc).isoformat(),
                              "passes": [], "coverage": [], "warm_done": False})
            if first_pass:
                _snap["warmed"] = warmed
                _snap["warm_done"] = True
            _snap["coverage"].append({"at": datetime.now(timezone.utc).isoformat(), "queued": len(syms), "priced": len(ticks or {}),
                                      "fresh_gap": stored})
            total = len(_snap["gaps"])
        # Coverage is the one thing that cannot be verified offline: say loudly when the feed had (almost) nothing fresh at this pass.
        if syms and stored * 5 < len(syms):
            logger.warning("[preopen] LOW COVERAGE: %d of %d queued symbols had a fresh price (%d tick(s) returned) - the gap gate "
                           "is mostly blind today", stored, len(syms), len(ticks or {}))
        logger.info("[preopen] snapshot: %d queued symbols, %d fresh gap this pass (%d total), %d warmed", len(syms), stored, total, warmed)
        return status()
    except Exception as e:  # noqa: BLE001
        logger.warning("[preopen] snapshot failed (ignored): %s", e)
        with _lock:
            day = ist_today_str()
            if _snap["day"] != day:
                _snap.update({"day": day, "gaps": {}, "passes": [], "coverage": [], "warm_done": False})
            _snap["passes"] = sorted(set(_snap["passes"]) | set(_reached()))      # do not retry the same pass every tick
        return status()
