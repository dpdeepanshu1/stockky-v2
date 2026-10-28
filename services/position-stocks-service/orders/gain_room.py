"""orders/gain_room.py - E251 (2026-10-28): "before the order, be sure there is a real possibility of gain".

The stop / target of an entry are known by the time this runs. Two independent checks ask whether THAT target is realistically reachable today:

  1. odds  - replay this exact stop % / target % on the stock's own 1-minute bars so far today (orders/trade_odds.py, which until now only RECORDED the
             answer). If the target came first clearly less often than break-even needs (GAIN_ROOM_ODDS_MARGIN_PTS points below, with at least
             GAIN_ROOM_ODDS_MIN_RESOLVED resolved replays) the stock is not moving enough today for this target: refuse.
  2. range - the stock has not already used up its normal day. expected_high = today's low x (1 + GAIN_ROOM_RANGE_MULT x the stock's average daily range %).
             The room from the current price up to that level must be at least GAIN_ROOM_MIN_TARGET_FRAC x the target %. A stock that already ran two
             normal days' worth of range has little left for a fresh 2-3 % target and a lot of give-back risk.

Both fail OPEN: no bars, too few replays, no daily history, any error = no opinion. GAIN_ROOM_MODE: live (refuse) | log (log only) | off.
The reason starts with GAIN_ROOM so the dashboard / skip counters can tell it apart. Never raises into the trading loop.
"""
from __future__ import annotations

import logging
from typing import Any, Optional, Sequence

import config

logger = logging.getLogger("position-stocks-gain-room")


def _cfg(name: str, default):
    try:
        v = getattr(config, name)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def mode() -> str:
    m = str(_cfg("GAIN_ROOM_MODE", "live") or "live").strip().lower()
    return m if m in ("live", "log", "off") else "live"


def odds_reason(bars: Optional[Sequence[Any]], stop_pct: float, target_pct: float) -> Optional[str]:
    """Reason when the replayed odds are clearly under break-even, else None. Pure."""
    from orders import trade_odds
    if not bars or stop_pct <= 0 or target_pct <= 0:
        return None
    v = trade_odds.evaluate(bars, "LONG", stop_pct, target_pct)
    if not v or v.get("p_target") is None:
        return None
    n = int(v.get("n_resolved") or 0)
    if n < int(_cfg("GAIN_ROOM_ODDS_MIN_RESOLVED", 25)):
        return None
    p_t, be = float(v["p_target"]), float(v["be_win"])
    margin = float(_cfg("GAIN_ROOM_ODDS_MARGIN_PTS", 8.0)) / 100.0
    if p_t < be - margin:
        return (f"GAIN_ROOM:ODDS:target came first in {p_t * 100:.0f}% of {n} replays today vs {be * 100:.0f}% needed "
                f"(stop {stop_pct:.2f}% / target {target_pct:.2f}%) - this stock is not moving enough for this target")
    return None


def range_reason(day_low: Optional[float], ltp: float, avg_range_pct: Optional[float], target_pct: float) -> Optional[str]:
    """Reason when the stock has little of its normal day left above the current price, else None. Pure."""
    frac = float(_cfg("GAIN_ROOM_MIN_TARGET_FRAC", 0.5))
    if frac <= 0 or not day_low or day_low <= 0 or ltp <= 0 or not avg_range_pct or avg_range_pct <= 0 or target_pct <= 0:
        return None
    mult = float(_cfg("GAIN_ROOM_RANGE_MULT", 2.0))
    expected_high = day_low * (1.0 + mult * avg_range_pct / 100.0)
    room = (expected_high - ltp) / ltp * 100.0
    need = frac * target_pct
    if room < need:
        return (f"GAIN_ROOM:RANGE:only {max(room, 0.0):.2f}% left to {mult:g}x its normal daily range ({avg_range_pct:.2f}%) from today's low "
                f"{day_low:.2f}; the {target_pct:.2f}% target needs at least {need:.2f}%")
    return None


def reject_reason(symbol: str, ltp: float, stop_pct: float, target_pct: float) -> Optional[str]:
    """GAIN_ROOM reason for the entry path, else None. live -> the reason; log -> logged, None returned; off -> None. Never raises."""
    try:
        m = mode()
        if m == "off":
            return None
        why = None
        try:
            from feed import bar_store
            why = odds_reason(bar_store.get_bars(str(symbol or "").strip().upper()), float(stop_pct), float(target_pct))
        except Exception as e:  # noqa: BLE001
            logger.debug("gain-room: odds check failed for %s (no opinion): %s", symbol, e)
        if not why:
            try:
                from feed import ws_client
                from screening import prev_day
                rng = ws_client.get_day_range(symbol)
                pdv = prev_day.get(symbol)
                why = range_reason(rng[0] if rng else None, float(ltp), getattr(pdv, "avg_range_pct", None) if pdv else None, float(target_pct))
            except Exception as e:  # noqa: BLE001
                logger.debug("gain-room: range check failed for %s (no opinion): %s", symbol, e)
        if why and m == "log":
            logger.info("gain-room (log only): %s would be skipped - %s", symbol, why)
            return None
        return why
    except Exception as e:  # noqa: BLE001
        logger.debug("gain-room: check failed for %s (no opinion): %s", symbol, e)
        return None