"""entry_engine/pullback_rearm.py - pullback re-arm for real-trade-service (2026-10-19).

WHY: when evaluate_mode() refuses a candidate for chasing (drift gate "ran +X% above signal ... Chasing", or the structure
gate's "chase / stretched above EMA20"), the candidate is consumed and forgotten. JUSTDIAL was refused for chasing at the
high, pulled back, and went on to new highs - the system never looked at it again at the better price.

WHAT: the chase WAIT ARMS the symbol here (per mode). Each cycle, before candidates are fetched, scan() reads the live price
of every armed symbol and fires a TRIGGER only when ALL of these hold:
  1. a real pullback happened:  REARM_MIN_PULLBACK_PCT <= (peak - trough) / peak <= REARM_MAX_PULLBACK_PCT
  2. it is bouncing:            price >= trough * (1 + REARM_BOUNCE_PCT / 100)
  3. the low has held:          the trough is at least REARM_HOLD_S old (no falling knife)
  4. not back at the high:      price <= peak * (1 - REARM_MIN_BELOW_PEAK_PCT / 100)
  5. not below VWAP (when known)
A trigger RE-QUEUES a fresh candidate (a copy of the refused one) and grants a short WAIVER for the chase checks only (drift
and structure chase/stretch). Every other gate, risk_engine, sizing, capital and the stop are exactly the normal path.

Pure bookkeeping. Never raises into the trading loop. Off with PULLBACK_REARM_ENABLED=0 (REAL and DEMO separately with
PULLBACK_REARM_MODES, default "REAL,DEMO").
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Optional
from zoneinfo import ZoneInfo

import config

logger = logging.getLogger("real-trade-pullback-rearm")
_IST = ZoneInfo("Asia/Kolkata")

# Text fragments of the WAIT reasons that arm a symbol. Only chase-type refusals arm: quality, liquidity, VWAP, regime, risk ...
# never do (waiting for a pullback would not fix them).
DRIFT_CHASE_MARK = "Chasing"


def _cfg(name: str, default):
    try:
        v = getattr(config, name)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def enabled(mode: Optional[str] = None) -> bool:
    if not bool(_cfg("PULLBACK_REARM_ENABLED", True)):
        return False
    if mode is None:
        return True
    modes = str(_cfg("PULLBACK_REARM_MODES", "REAL,DEMO")).upper().replace(" ", "")
    return str(mode).upper() in {m for m in modes.split(",") if m}


@dataclass
class Setup:
    mode: str
    symbol: str
    armed_at: float
    peak: float
    trough: float
    trough_at: float
    day_pct: float = 0.0
    armed_by: str = ""
    last_wait: str = ""
    template_candidate_id: Optional[int] = None


_SETUPS: dict = {}          # (mode, symbol) -> Setup
_ATTEMPTS: dict = {}        # (mode, symbol) -> (ist_date, count)
_WAIVERS: dict = {}         # (mode, symbol) -> expires_at (monotonic-ish wall clock)
_LOCK = threading.Lock()


def _today() -> str:
    return datetime.now(_IST).strftime("%Y-%m-%d")


def reset() -> None:
    """Test helper / day rollover."""
    with _LOCK:
        _SETUPS.clear()
        _ATTEMPTS.clear()
        _WAIVERS.clear()


def _attempts_today(key: tuple) -> int:
    d, n = _ATTEMPTS.get(key, (None, 0))
    return n if d == _today() else 0


def is_chase_reason(reason: Optional[str], *, structure: bool = False) -> bool:
    """True when a WAIT reason is a chase / stretch refusal (the only kind a pullback can cure)."""
    if not reason:
        return False
    t = str(reason)
    if structure:
        low = t.lower()
        return bool(re.search(r"\b(chase|stretched)\b", low)) and not any(
            bad in low for bad in ("bearish", "stop is too wide", "no room", "room to")
        )
    return DRIFT_CHASE_MARK in t


def arm(mode: str, symbol: str, reason: str, ltp: float, day_pct: float, day_high: Optional[float] = None,
        candidate_id: Optional[int] = None, now: Optional[float] = None) -> bool:
    """Remember a candidate refused for chasing. Returns True when newly armed."""
    try:
        if not enabled(mode) or ltp <= 0:
            return False
        if day_pct < float(_cfg("REARM_MIN_DAY_PCT", 1.0)):
            return False
        if ltp < float(_cfg("REARM_MIN_PRICE", _cfg("RISK_MIN_STOCK_PRICE", 0.0)) or 0.0):
            return False
        t = time.time() if now is None else now
        key = (str(mode).upper(), symbol)
        with _LOCK:
            if key in _SETUPS or _attempts_today(key) >= int(_cfg("REARM_MAX_ATTEMPTS", 2)):
                return False
            if len(_SETUPS) >= int(_cfg("REARM_MAX_ARMED", 25)):
                return False
            peak = max(ltp, float(day_high or 0.0))
            _SETUPS[key] = Setup(mode=key[0], symbol=symbol, armed_at=t, peak=peak, trough=ltp, trough_at=t,
                                 day_pct=float(day_pct), armed_by=str(reason)[:80], template_candidate_id=candidate_id)
        logger.info("pullback-rearm[%s]: ARMED %s at %.2f (peak %.2f, %+.2f%% on the day) - %s", key[0], symbol, ltp, peak, day_pct, str(reason)[:60])
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


def armed_symbols(mode: str) -> list:
    m = str(mode).upper()
    with _LOCK:
        return [s.symbol for (md, _), s in _SETUPS.items() if md == m]


def scan(mode: str, ticks: dict, now: Optional[float] = None) -> list:
    """Evaluate every armed symbol of this mode once, from already-fetched ticks ({symbol: tick with .price/.day_high/.vwap}).
    Returns trigger dicts [{symbol, ltp, peak, trough, why, template_candidate_id}]. Dropped setups leave the armed set; a triggered
    one stays until the caller calls consume(). Missing ticks just WAIT."""
    hits: list = []
    if not enabled(mode):
        return hits
    t = time.time() if now is None else now
    m = str(mode).upper()
    with _LOCK:
        items = [s for (md, _), s in _SETUPS.items() if md == m]
    for s in items:
        try:
            tk = ticks.get(s.symbol)
            ltp = float(getattr(tk, "price", 0.0) or 0.0) if tk is not None else 0.0
            verdict, why = evaluate(s, ltp, getattr(tk, "day_high", None), getattr(tk, "vwap", None), t)
        except Exception as e:  # noqa: BLE001
            logger.debug("pullback-rearm: scan %s failed: %s", s.symbol, e)
            continue
        if verdict == "WAIT":
            s.last_wait = why
        elif verdict == "DROP":
            with _LOCK:
                _SETUPS.pop((m, s.symbol), None)
            logger.info("pullback-rearm[%s]: dropped %s (%s)", m, s.symbol, why)
        else:
            hits.append({"symbol": s.symbol, "ltp": ltp, "peak": s.peak, "trough": s.trough, "why": why,
                         "template_candidate_id": s.template_candidate_id})
            logger.info("pullback-rearm[%s]: TRIGGER %s at %.2f - %s", m, s.symbol, ltp, why)
    return hits


def consume(mode: str, symbol: str, now: Optional[float] = None) -> None:
    """The caller is re-queuing this trigger now: leave the armed set, count one attempt (capped per day) and grant the waiver."""
    key = (str(mode).upper(), symbol)
    t = time.time() if now is None else now
    with _LOCK:
        _SETUPS.pop(key, None)
        _ATTEMPTS[key] = (_today(), _attempts_today(key) + 1)
        _WAIVERS[key] = t + float(_cfg("REARM_WAIVER_S", 420.0))


def has_waiver(mode: str, symbol: str, now: Optional[float] = None) -> bool:
    """True while a re-armed symbol may skip the chase checks (drift + structure chase/stretch). Expires by itself."""
    key = (str(mode).upper(), symbol)
    t = time.time() if now is None else now
    with _LOCK:
        exp = _WAIVERS.get(key)
        if exp is None:
            return False
        if t > exp:
            _WAIVERS.pop(key, None)
            return False
        return True


def status() -> dict:
    now = time.time()
    with _LOCK:
        return {"enabled": enabled(), "modes": str(_cfg("PULLBACK_REARM_MODES", "REAL,DEMO")),
                "armed": [{"mode": s.mode, "symbol": s.symbol, "peak": round(s.peak, 2), "trough": round(s.trough, 2),
                           "age_s": int(now - s.armed_at), "waiting_on": s.last_wait or "-", "armed_by": s.armed_by}
                          for s in _SETUPS.values()],
                "waivers": sorted(f"{k[0]}:{k[1]}" for k, exp in _WAIVERS.items() if exp > now),
                "attempts_today": {f"{k[0]}:{k[1]}": v[1] for k, v in _ATTEMPTS.items() if v[0] == _today()}}
