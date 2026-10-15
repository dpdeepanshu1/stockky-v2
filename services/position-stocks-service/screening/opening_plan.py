"""screening/opening_plan.py - 09:10 opening plan: per-symbol stop/target volatility for the first minutes after the bell (group 391, 2026-10-15).

WHY: at 09:15 a symbol has no 5-minute bars, so orders/adaptive.py fell through to the legacy path whose stop floor is MIN_STOP_PCT (2%) and whose
target lands near 4.4% - the same flat levels for every name at the open. This module gives that moment a real volatility number instead:
  * at OPENING_PLAN_TIME_IST (default 09:10) build() reads the previous-session daily ATR % of each symbol from screening/prev_day.py (CACHE ONLY - the
    09:08 pre-open snapshot warmed it; a miss starts prev_day's own background fetch and is retried by the next lookup);
  * bar_atr_pct(symbol) returns that ATR scaled to one ADAPTIVE_BAR_MINUTES bar (daily % x OPENING_PLAN_BAR_FRACTION) while the plan window is open,
    and adaptive._compute_levels feeds it to the same bar-level stop/target rules the live bars use.
Live bars always win: the plan is only asked when the tick buffer / bar store / history call gave fewer than ADAPTIVE_BAR_MIN_BARS bars.
No network call on the entry path, in-memory, one build per IST day, nothing raises. Off with OPENING_PLAN_ENABLED=0.
HONEST LIMIT: the fraction is an assumption; the daily ATR can lag a regime change. A symbol with no previous-day data has no plan and takes the legacy path.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Optional

import config
from tz_utils import ist_now, ist_time_at_or_after, ist_today_str, is_nse_holiday, parse_hhmm

logger = logging.getLogger("position-stocks-opening-plan")

_plan: dict = {"day": None, "bars": {}, "built_at": None, "universe": 0}


def reset() -> None:
    _plan.update({"day": None, "bars": {}, "built_at": None, "universe": 0})


def _norm(symbol) -> str:
    return str(symbol or "").upper().replace(".NS", "").strip()


def _daily_pct(pd) -> Optional[float]:
    """Daily volatility % of one PrevDay: 14-day ATR %, else the mean daily range %. None when neither is usable."""
    for v in (getattr(pd, "atr_pct", None), getattr(pd, "avg_range_pct", None)):
        try:
            if v is not None and math.isfinite(float(v)) and float(v) > 0:
                return float(v)
        except (TypeError, ValueError):
            continue
    return None


def expected_bar_pct(pd) -> Optional[float]:
    """Expected range of one ADAPTIVE_BAR_MINUTES bar, in % of price, from a PrevDay. None when there is no usable daily number."""
    try:
        d = _daily_pct(pd)
        frac = float(config.OPENING_PLAN_BAR_FRACTION)
        if d is None or not math.isfinite(frac) or frac <= 0:
            return None
        return d * frac
    except Exception:  # noqa: BLE001
        return None


def due(now: Optional[datetime] = None) -> bool:
    """True from OPENING_PLAN_TIME_IST until OPENING_PLAN_USE_UNTIL_IST on a trading day while today's plan is not built. Never raises."""
    try:
        if not config.OPENING_PLAN_ENABLED:
            return False
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(n) or _plan["day"] == ist_today_str(now):
            return False
        return (ist_time_at_or_after(parse_hhmm(config.OPENING_PLAN_TIME_IST, 9, 10), now)
                and not ist_time_at_or_after(parse_hhmm(config.OPENING_PLAN_USE_UNTIL_IST, 10, 0), now))
    except Exception:  # noqa: BLE001
        return False


def window_open(now: Optional[datetime] = None) -> bool:
    """True on a trading day from OPENING_PLAN_TIME_IST until OPENING_PLAN_USE_UNTIL_IST. Never raises."""
    try:
        if not config.OPENING_PLAN_ENABLED:
            return False
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(n):
            return False
        return (ist_time_at_or_after(parse_hhmm(config.OPENING_PLAN_TIME_IST, 9, 10), now)
                and not ist_time_at_or_after(parse_hhmm(config.OPENING_PLAN_USE_UNTIL_IST, 10, 0), now))
    except Exception:  # noqa: BLE001
        return False


def build(symbols, now: Optional[datetime] = None) -> dict:
    """Plan every symbol whose previous-day data is cached; ask prev_day to fetch the rest. Marks the day (once per IST day). Never raises."""
    try:
        from screening import prev_day
        bars: dict = {}
        syms = [_norm(s) for s in (symbols or []) if _norm(s)]
        for s in syms:
            pd = prev_day.get(s)            # cached value, or None + a background fetch for next time
            b = expected_bar_pct(pd) if pd is not None else None
            if b is not None:
                bars[s] = b
        _plan.update({"day": ist_today_str(now), "bars": bars, "universe": len(syms),
                      "built_at": ist_now(now).isoformat()})
        logger.info("[opening-plan] built: %d of %d symbols have a previous-day volatility", len(bars), len(syms))
    except Exception as e:  # noqa: BLE001
        logger.warning("[opening-plan] build failed (ignored): %s", e)
        _plan["day"] = ist_today_str(now)
    return status()


def bar_atr_pct(symbol: str, now: Optional[datetime] = None) -> Optional[float]:
    """Expected bar range % for the symbol while the window is open, else None. Planned value first; a symbol missing from the plan is read from the
    prev_day cache (peek: never a fetch). Never raises."""
    try:
        if not window_open(now):
            return None
        sym = _norm(symbol)
        if not sym:
            return None
        if _plan["day"] == ist_today_str(now):
            v = _plan["bars"].get(sym)
            if v is not None:
                return v
        from screening import prev_day
        pd = prev_day.peek(sym)
        return expected_bar_pct(pd) if pd is not None else None
    except Exception:  # noqa: BLE001
        return None


def status() -> dict:
    top = sorted(_plan["bars"].items(), key=lambda kv: -kv[1])[:10]
    return {"day": _plan["day"], "planned": len(_plan["bars"]), "universe": _plan["universe"], "built_at": _plan["built_at"],
            "widest_bar_pct": {k: round(v, 3) for k, v in top}}
