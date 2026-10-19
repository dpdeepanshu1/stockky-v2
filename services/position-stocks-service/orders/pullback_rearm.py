"""orders/pullback_rearm.py - pullback re-arm entries (2026-10-19).

WHY: on 2026-10-19 the entry gates (RANGE_GATE:NEAR_DAY_HIGH, OPENING_GATE:EXTENDED_ABOVE_OR, ...) correctly refused to buy
strong stocks at their highs (JUSTDIAL, KARURVYSYA, EUROPRATIK, ...), but then simply forgot them. Several of those stocks
pulled back 1-3 % and went on to new highs; the gates never gave the system a second look at a better price.

WHAT: when attempt_entry() skips a candidate for being at / extended above its high, the symbol is ARMED here. Every cycle,
scan() reads the live price of each armed symbol (independent of the screener, which drops a stock once its window return
fades) and fires a TRIGGER only when ALL of these hold:
  1. a real pullback happened:   REARM_MIN_PULLBACK_PCT <= (peak - trough) / peak <= REARM_MAX_PULLBACK_PCT
  2. it is bouncing:             price >= trough * (1 + REARM_BOUNCE_PCT / 100)
  3. the low has held:           the trough is at least REARM_HOLD_S old (no falling knife)
  4. not back at the high:       price <= peak * (1 - REARM_MIN_BELOW_PEAK_PCT / 100), so there is room again
  5. not below VWAP (when known)
A trigger goes through the NORMAL entry path (quality gate, risk, liquidity, VWAP, circuit, depth, loss brake, market filter,
capital ...). Only the gates that caused the arming (NEAR_DAY_HIGH / EXTENDED_ABOVE_OR) are waived, and the PRICE_GUARD day-gain
cap stays. The stop is placed under the pullback low (never wider than the normal adaptive stop).

Pure bookkeeping + reads of the in-memory tick feed. Never raises into the trading loop. Off with PULLBACK_REARM_ENABLED=0.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger("position-stocks-pullback-rearm")
_IST = ZoneInfo("Asia/Kolkata")

# Skip reasons (as logged by attempt_entry) that arm a symbol. Everything else (quality, liquidity, VWAP, price guard ...) does not.
ARMING_PREFIXES = ("RANGE_GATE:NEAR_DAY_HIGH", "OPENING_GATE:NEAR_DAY_HIGH", "OPENING_GATE:EXTENDED_ABOVE_OR")
# Gate reasons waived for a re-arm entry (the very ones that armed it).
WAIVED_PREFIXES = ("OPENING_GATE:NEAR_DAY_HIGH", "OPENING_GATE:EXTENDED_ABOVE_OR")


def _cfg(name: str, default):
    try:
        v = getattr(config, name)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    return bool(_cfg("PULLBACK_REARM_ENABLED", True))


@dataclass
class Setup:
    symbol: str
    window_minutes: int
    pct_change: float
    armed_at: float
    peak: float
    trough: float
    trough_at: float
    skip_reason: str = ""
    last_wait: str = ""


_SETUPS: dict = {}
_ATTEMPTS: dict = {}        # symbol -> (ist_date, count)
_LOCK = threading.Lock()


def _today() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def reset() -> None:
    """Test helper / day rollover."""
    with _LOCK:
        _SETUPS.clear()
        _ATTEMPTS.clear()


def _attempts_today(symbol: str) -> int:
    d, n = _ATTEMPTS.get(symbol, (None, 0))
    return n if d == _today() else 0


def is_arming_reason(reason: Optional[str]) -> bool:
    return bool(reason) and str(reason).startswith(ARMING_PREFIXES)


def is_waived_opening_reason(reason: Optional[str]) -> bool:
    return bool(reason) and str(reason).startswith(WAIVED_PREFIXES)


def arm(symbol: str, reason: str, ltp: float, pct_change: float, window_minutes: int,
        day_high: Optional[float] = None, now: Optional[float] = None) -> bool:
    """Remember a symbol that was skipped for being at / extended above its high. Returns True when newly armed."""
    try:
        if not enabled() or not is_arming_reason(reason) or ltp <= 0:
            return False
        if pct_change < float(_cfg("REARM_MIN_DAY_PCT", 1.5)):
            return False
        if ltp < float(_cfg("MIN_STOCK_PRICE", 0.0) or 0.0):
            return False
        t = time.time() if now is None else now
        with _LOCK:
            if symbol in _SETUPS or _attempts_today(symbol) >= int(_cfg("REARM_MAX_ATTEMPTS", 2)):
                return False
            if len(_SETUPS) >= int(_cfg("REARM_MAX_ARMED", 25)):
                return False
            peak = max(ltp, float(day_high or 0.0))
            _SETUPS[symbol] = Setup(symbol=symbol, window_minutes=int(window_minutes), pct_change=float(pct_change),
                                    armed_at=t, peak=peak, trough=ltp, trough_at=t, skip_reason=str(reason)[:60])
        logger.info("pullback-rearm: ARMED %s at %.2f (peak %.2f, %+.2f%% on the day) after %s", symbol, ltp, peak, pct_change, str(reason)[:50])
        return True
    except Exception as e:  # noqa: BLE001
        logger.debug("pullback-rearm: arm failed for %s: %s", symbol, e)
        return False


def evaluate(s: Setup, ltp: float, day_high: Optional[float], vwap: Optional[float], now: float) -> tuple:
    """Update the setup with a fresh price and decide. Returns (verdict, why), verdict in TRIGGER / WAIT / DROP."""
    if ltp <= 0:
        return "WAIT", "NO_PRICE"
    if now - s.armed_at > float(_cfg("REARM_MAX_AGE_MIN", 90.0)) * 60.0:
        return "DROP", "EXPIRED"
    dh = float(day_high or 0.0)
    if ltp > s.peak:                      # new high: a new leg starts, the old pullback no longer counts
        s.peak, s.trough, s.trough_at = ltp, ltp, now
    elif dh > s.peak:
        s.peak = dh
    if ltp < s.trough:
        s.trough, s.trough_at = ltp, now
    depth = (s.peak - s.trough) / s.peak * 100.0
    if depth > float(_cfg("REARM_MAX_PULLBACK_PCT", 3.5)):
        return "DROP", f"PULLBACK_TOO_DEEP:{depth:.2f}%"
    if depth < float(_cfg("REARM_MIN_PULLBACK_PCT", 0.8)):
        return "WAIT", f"NO_PULLBACK_YET:{depth:.2f}%"
    bounce = (ltp - s.trough) / s.trough * 100.0
    if bounce < float(_cfg("REARM_BOUNCE_PCT", 0.3)):
        return "WAIT", f"NO_BOUNCE_YET:{bounce:.2f}%"
    if now - s.trough_at < float(_cfg("REARM_HOLD_S", 30.0)):
        return "WAIT", "LOW_NOT_HELD_YET"
    below_peak = (s.peak - ltp) / s.peak * 100.0
    if below_peak < float(_cfg("REARM_MIN_BELOW_PEAK_PCT", 0.3)):
        return "WAIT", f"BACK_AT_HIGH:{below_peak:.2f}%"
    if vwap and vwap > 0 and ltp < vwap:
        return "WAIT", f"BELOW_VWAP:{vwap:.2f}"
    return "TRIGGER", f"pullback {depth:.2f}% from {s.peak:.2f}, low {s.trough:.2f}, bounce {bounce:.2f}%"


def _live(symbol: str) -> tuple:
    """(ltp, day_high, vwap) from the in-memory tick feed; any of them may be None."""
    from feed import ws_client
    ltp = ws_client.get_last_ltp(symbol)
    rng = None
    vwap = None
    try:
        rng = ws_client.get_day_range(symbol)
    except Exception:  # noqa: BLE001
        pass
    try:
        vwap = ws_client.get_exchange_vwap(symbol)
    except Exception:  # noqa: BLE001
        pass
    return ltp, (rng[1] if rng else None), vwap


def scan(now: Optional[float] = None, live_fn=None) -> list:
    """Evaluate every armed symbol once. Returns trigger dicts [{symbol, ltp, peak, trough, window_minutes, pct_change, why}].
    Dropped symbols leave the armed set. A triggered symbol stays armed until the caller calls consume(symbol) when it actually
    attempts the entry (so a second trigger is not lost when the first one enters)."""
    hits: list = []
    if not enabled():
        return hits
    t = time.time() if now is None else now
    getter = live_fn or _live
    with _LOCK:
        items = list(_SETUPS.values())
    for s in items:
        try:
            ltp, dh, vwap = getter(s.symbol)
            verdict, why = evaluate(s, float(ltp or 0.0), dh, vwap, t)
        except Exception as e:  # noqa: BLE001
            logger.debug("pullback-rearm: scan %s failed: %s", s.symbol, e)
            continue
        if verdict == "WAIT":
            s.last_wait = why
        elif verdict == "DROP":
            with _LOCK:
                _SETUPS.pop(s.symbol, None)
            logger.info("pullback-rearm: dropped %s (%s)", s.symbol, why)
        else:
            hits.append({"symbol": s.symbol, "ltp": float(ltp), "peak": s.peak, "trough": s.trough,
                         "window_minutes": s.window_minutes, "pct_change": s.pct_change, "why": why})
            logger.info("pullback-rearm: TRIGGER %s at %.2f - %s", s.symbol, float(ltp), why)
    return hits


def consume(symbol: str) -> None:
    """The caller is attempting this trigger now: leave the armed set and count one attempt (re-arming is capped per day)."""
    with _LOCK:
        _SETUPS.pop(symbol, None)
        _ATTEMPTS[symbol] = (_today(), _attempts_today(symbol) + 1)


def structure_stop_pct(ltp: float, trough: float, adaptive_stop_pct: float) -> float:
    """Stop distance (%) for a re-arm entry: just under the pullback low, never wider than the normal adaptive stop and never
    tighter than REARM_MIN_STOP_PCT (a stop inside normal noise is how VIDYAWIRES lost 0.76 % in 11 minutes)."""
    buf = float(_cfg("REARM_STOP_BUFFER_PCT", 0.15))
    floor = float(_cfg("REARM_MIN_STOP_PCT", 0.7))
    if ltp <= 0 or trough <= 0:
        return adaptive_stop_pct
    structural = (ltp - trough * (1 - buf / 100.0)) / ltp * 100.0
    return round(min(adaptive_stop_pct, max(structural, floor)), 4)


def status() -> dict:
    with _LOCK:
        return {"enabled": enabled(), "armed": [
            {"symbol": s.symbol, "peak": round(s.peak, 2), "trough": round(s.trough, 2), "age_s": int(time.time() - s.armed_at),
             "waiting_on": s.last_wait or "-", "armed_by": s.skip_reason} for s in _SETUPS.values()],
            "attempts_today": {k: v[1] for k, v in _ATTEMPTS.items() if v[0] == _today()}}