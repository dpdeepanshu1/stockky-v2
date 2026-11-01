"""entry_engine/open_cues.py - overseas cues read at 08:30 IST shrink the morning's long entries when the world is weak (E285 round 102).

WHY: the opening gap and the first hour of a day are set largely by what Asia and the US index futures did overnight. entry_engine/global_cues.py reads the same
sources, but only for the hold-or-sell decision at the close (15:00); nothing in the morning entry path looked at them. This module takes ONE reading of the cues that
are LIVE at 08:30 (Nikkei and Hang Seng trade from 05:30 / 06:45 IST; US index futures trade almost around the clock) and turns a weak overnight lead into a
SIZE PENALTY on every long entry until OPEN_CUES_UNTIL_IST. Europe (opens 12:30 IST) is left out on purpose: at 08:30 its last bar is yesterday's close.

RULE (assess()): needs >= GLOBAL_CUE_MIN_READINGS readings. Level 1 (weak) when the average <= OPEN_CUES_WEAK_AVG_PCT or any US-futures cue <= OPEN_CUES_WEAK_US_FUT_PCT;
level 2 (very weak) when the average <= OPEN_CUES_WEAK_AVG_PCT x OPEN_CUES_VERY_WEAK_FACTOR or a US-futures cue is that much lower. Size multiplier OPEN_CUES_WEAK_MUL (0.7)
at level 1, OPEN_CUES_VERY_WEAK_MUL (0.5) at level 2. It never refuses an entry and never sizes UP: a strong lead is no opinion.

SNAPSHOT: due() is true from OPEN_CUES_TIME_IST (08:30) on a trading day until OPEN_CUES_UNTIL_IST while no reading has been stored today (an empty answer is asked again
after OPEN_CUES_RETRY_S, so a restart at 09:40 still gets one). take() asks market-data GET /global-cues once for OPEN_CUES_SYMBOLS and stores the readings for the IST day.

HONEST LIMIT: whether Yahoo answers for these symbols from the VM at 08:30 could not be verified offline. A symbol it cannot serve is simply absent; with fewer than
GLOBAL_CUE_MIN_READINGS readings there is no opinion. GIFT Nifty is not included (no verified source). FAIL OPEN, ALWAYS. Off with OPEN_CUES_ENABLED=false.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime
from typing import Optional, Tuple

import config
from entry_engine import global_cues
from tz_utils import ist_now, ist_time_at_or_after, ist_today_str, is_nse_holiday, parse_hhmm

logger = logging.getLogger("real-trade-open-cues")

_lock = threading.Lock()
_state: dict = {"day": None, "snap": None, "taken_at": None, "tried_at": -1e9, "level": 0, "reason": None}


def reset() -> None:
    with _lock:
        _state.update({"day": None, "snap": None, "taken_at": None, "tried_at": -1e9, "level": 0, "reason": None})


def _symbols() -> list:
    return [x.strip().upper() for x in str(getattr(config, "OPEN_CUES_SYMBOLS", "") or "").split(",") if x.strip()]


def _futures() -> set:
    return {x.strip().upper() for x in str(getattr(config, "GLOBAL_CUE_US_FUTURES", "") or "").split(",") if x.strip()}


def parse_body(body) -> dict:
    """{symbol: day % change} from a /global-cues body, rows older than OPEN_CUES_MAX_AGE_S dropped. Never raises."""
    out: dict = {}
    try:
        rows = body.get("rows") if isinstance(body, dict) else None
        if not isinstance(rows, dict):
            return out
        lim = float(getattr(config, "OPEN_CUES_MAX_AGE_S", 7200.0))
        for sym, row in rows.items():
            v = global_cues.parse_row(row, max_age_s=lim)
            if v is not None:
                out[str(sym).strip().upper()] = v
    except Exception:  # noqa: BLE001
        return {}
    return out


def assess(snap: Optional[dict]) -> Tuple[int, Optional[str]]:
    """(level 0 / 1 / 2, reason text or None) from {symbol: day %}. Level 0 = no opinion or a fine lead. Never raises."""
    try:
        if not snap or len(snap) < max(1, int(getattr(config, "GLOBAL_CUE_MIN_READINGS", 2))):
            return 0, None
        lim_a = float(getattr(config, "OPEN_CUES_WEAK_AVG_PCT", -0.9) or 0.0)
        lim_f = float(getattr(config, "OPEN_CUES_WEAK_US_FUT_PCT", -1.2) or 0.0)
        k = max(1.0, float(getattr(config, "OPEN_CUES_VERY_WEAK_FACTOR", 2.0)))
        avg = sum(snap.values()) / len(snap)
        fut = _futures()
        worst_sym, worst = None, None
        for sym in sorted(fut):
            if sym in snap and (worst is None or snap[sym] < worst):
                worst_sym, worst = sym, snap[sym]
        level, why = 0, None
        if lim_a < 0 and avg <= lim_a:
            level, why = (2 if avg <= lim_a * k else 1), f"overseas cues average {avg:+.2f}% over {len(snap)} markets <= {lim_a:+.2f}%"
        if lim_f < 0 and worst is not None and worst <= lim_f:
            lv = 2 if worst <= lim_f * k else 1
            if lv > level:
                level, why = lv, f"US futures {worst_sym} {worst:+.2f}% <= {lim_f:+.2f}%"
        return (level, f"OPEN_CUES_WEAK: {why} - weak overseas lead into the open") if level else (0, None)
    except Exception:  # noqa: BLE001
        return 0, None


def due(now: Optional[datetime] = None) -> bool:
    """True on a trading day between OPEN_CUES_TIME_IST and OPEN_CUES_UNTIL_IST while today has no reading and the last try is older than OPEN_CUES_RETRY_S. Never raises."""
    try:
        if not getattr(config, "OPEN_CUES_ENABLED", True):
            return False
        n = ist_now(now)
        if n.weekday() >= 5 or is_nse_holiday(n):
            return False
        if not ist_time_at_or_after(parse_hhmm(config.OPEN_CUES_TIME_IST, 8, 30), now):
            return False
        if ist_time_at_or_after(parse_hhmm(config.OPEN_CUES_UNTIL_IST, 10, 30), now):
            return False
        with _lock:
            if _state["day"] == ist_today_str(now) and _state["snap"]:
                return False
            return time.monotonic() - _state["tried_at"] >= float(config.OPEN_CUES_RETRY_S)
    except Exception:  # noqa: BLE001
        return False


def _fetch() -> dict:
    import httpx
    from market_feed.feed import MARKET_DATA_URL
    syms = _symbols()
    if not syms:
        return {}
    with httpx.Client(timeout=float(config.GLOBAL_CUE_TIMEOUT_S)) as client:
        r = client.get(f"{MARKET_DATA_URL}/global-cues", params={"symbols": ",".join(syms)})
    if r.status_code != 200:
        raise RuntimeError(f"global-cues HTTP {r.status_code}")
    return parse_body(r.json())


def take(now: Optional[datetime] = None) -> Optional[dict]:
    """Read the cues once and store them for the IST day. Blocking (call through asyncio.to_thread). Returns the readings or None. Never raises."""
    try:
        day = ist_today_str(now)
        with _lock:
            _state["tried_at"] = time.monotonic()          # claimed before the call: a second caller waits OPEN_CUES_RETRY_S
        try:
            snap = _fetch() or None
        except Exception as e:  # noqa: BLE001
            logger.info("open_cues: fetch failed (%s: %s) - no opinion", type(e).__name__, e)
            snap = None
        level, reason = assess(snap)
        with _lock:
            if snap:
                _state.update({"day": day, "snap": snap, "taken_at": ist_now(now).isoformat(), "level": level, "reason": reason})
        if snap:
            logger.info("[open_cues] %d reading(s): %s -> %s", len(snap), ", ".join(f"{k} {v:+.2f}%" for k, v in sorted(snap.items())),
                        reason or "no penalty")
        else:
            logger.warning("[open_cues] no overseas reading this time (Yahoo / market-data) - no penalty; retry in %ss", int(config.OPEN_CUES_RETRY_S))
        return snap
    except Exception:  # noqa: BLE001
        return None


def multiplier(now: Optional[datetime] = None) -> Tuple[float, Optional[str]]:
    """(size multiplier for a long entry now, reason). (1.0, None) when off, no reading today, a fine lead or past OPEN_CUES_UNTIL_IST. Never raises."""
    try:
        if not getattr(config, "OPEN_CUES_ENABLED", True):
            return 1.0, None
        with _lock:
            if _state["day"] != ist_today_str(now) or not _state["level"]:
                return 1.0, None
            level, reason = int(_state["level"]), _state["reason"]
        if ist_time_at_or_after(parse_hhmm(config.OPEN_CUES_UNTIL_IST, 10, 30), now):
            return 1.0, None
        m = float(config.OPEN_CUES_VERY_WEAK_MUL if level >= 2 else config.OPEN_CUES_WEAK_MUL)
        return (max(0.05, min(1.0, m)), reason)
    except Exception:  # noqa: BLE001
        return 1.0, None


def status() -> dict:
    with _lock:
        return {"day": _state["day"], "readings": dict(_state["snap"] or {}), "taken_at": _state["taken_at"], "level": _state["level"], "reason": _state["reason"]}
