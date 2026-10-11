"""session_proof.py - decide from the PRICES whether the exchange really traded today.

WHY (group 319, 2026-10-11): the holiday calendars are static date sets copied into seven places. A date nobody
listed (an ad-hoc exchange closure, a holiday NSE adds late, next year before December) is treated as a normal trading
day and the whole system runs entry/scan cycles on prices that cannot change. Your rule, in code:

    If, from 09:15 to 10:00 IST on a weekday, no stock moves and Nifty does not change, the market is not open today.

HOW: the feeds call observe(symbol, price) for every tick/quote they store. During the proof window we remember the
FIRST price seen per symbol after the bell and count a "move" whenever a later price differs from it (a 2-minute-old
quote that simply repeats does not count). At/after the window end:

  * moves >= MIN_MOVES            -> status "open"    (proven; stays open for the day)
  * moves == 0 and enough symbols were observed (>= MIN_SYMBOLS, or Nifty alone was observed) -> "closed"
  * moves == 0 but too few symbols observed (feed was down) -> "unknown" (never closes on missing data)

A "closed" verdict is DAY-SCOPED and can still be overturned: the first real move after it flips the day back to "open"
(self-healing, so a late-starting feed can never lock the system out for the whole session).

Pure stdlib, thread-safe, never raises into the caller. Off with SESSION_PROOF_ENABLED=false.
"""
from __future__ import annotations

import logging
import os
import threading
from datetime import datetime, time as _time, timezone
from typing import Dict, Optional
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
logger = logging.getLogger("session-proof")


def _env_bool(name: str, default: bool) -> bool:
    raw = (os.getenv(name) or "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    try:
        return int(((os.getenv(name) or "").strip() or default))
    except Exception:
        return default


def _env_time(name: str, default: _time) -> _time:
    raw = (os.getenv(name) or "").strip()
    try:
        hh, mm = raw.split(":")
        return _time(int(hh), int(mm))
    except Exception:
        return default


ENABLED = _env_bool("SESSION_PROOF_ENABLED", True)
WINDOW_START = _env_time("SESSION_PROOF_START_IST", _time(9, 15))
WINDOW_END = _env_time("SESSION_PROOF_END_IST", _time(10, 0))
MIN_MOVES = max(1, _env_int("SESSION_PROOF_MIN_MOVES", 1))
MIN_SYMBOLS = max(1, _env_int("SESSION_PROOF_MIN_SYMBOLS", 10))
# group320: Nifty ALONE may close the day only after this many separate in-window readings of an unchanged index
# (one stale cached index quote must never be enough; the probe takes one reading a minute).
NIFTY_MIN_OBS = max(1, _env_int("SESSION_NIFTY_MIN_OBS", 3))

# Index names that count as "Nifty" for the Nifty-unchanged half of the rule.
_NIFTY_KEYS = {"NIFTY", "NIFTY 50", "NIFTY50", "^NSEI", "NSEI"}

_lock = threading.Lock()
_state: Dict[str, object] = {}


def _now_ist(now: Optional[datetime] = None) -> datetime:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=IST)
    return dt.astimezone(IST)


def reset_state() -> None:
    """Test helper."""
    with _lock:
        _state.clear()


def _fresh(day: str) -> dict:
    return {
        "day": day,
        "first": {},          # symbol -> first price seen inside the window
        "moved": set(),       # symbols whose price differed from their first price
        "nifty_seen": False,
        "nifty_obs": 0,        # in-window Nifty readings (group320)
        "nifty_moved": False,
        "proven_open": False,  # sticky for the day once a real move is seen
        "last_status": None,   # group321: for transition logging
    }


def _day_state(now_ist: datetime) -> dict:
    day = now_ist.strftime("%Y-%m-%d")
    st = _state.get("cur")
    if not isinstance(st, dict) or st.get("day") != day:
        st = _fresh(day)
        _state["cur"] = st
    return st


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except Exception:
        return None
    if f != f or f in (float("inf"), float("-inf")) or f <= 0:
        return None
    return f


def observe(symbol: str, price, now: Optional[datetime] = None) -> None:
    """Record one stored tick/quote. Cheap (dict ops under a lock); never raises."""
    try:
        if not ENABLED:
            return
        ist = _now_ist(now)
        if ist.weekday() >= 5:
            return
        t = ist.time()
        if t < WINDOW_START:
            return
        p = _num(price)
        sym = str(symbol or "").strip().upper()
        if p is None or not sym:
            return
        with _lock:
            st = _day_state(ist)
            # After the window only a MOVE matters (it can overturn a "closed" verdict).
            in_window = t < WINDOW_END
            first = st["first"]
            if in_window and sym in _NIFTY_KEYS:
                st["nifty_obs"] += 1
            if sym not in first:
                if in_window:
                    first[sym] = p
                    if sym in _NIFTY_KEYS:
                        st["nifty_seen"] = True
                return
            if abs(first[sym] - p) > 1e-9:
                st["moved"].add(sym)
                if sym in _NIFTY_KEYS:
                    st["nifty_moved"] = True
                if len(st["moved"]) >= MIN_MOVES:
                    st["proven_open"] = True
    except Exception:
        return


def _log_transition(st: dict, out: dict) -> None:
    """group321: log every change of the verdict (called with the lock held). Before this nothing was logged, so a day
    wrongly judged closed could only be found by calling /market/session. closed / overturned are WARNING."""
    try:
        new = out.get("status")
        old = st.get("last_status")
        if new == old:
            return
        st["last_status"] = new
        msg = ("session proof: %s -> %s (day=%s, observed=%s, moved=%s, nifty_readings=%s, nifty_moved=%s)"
               % (old or "start", new, out.get("day"), out.get("observed_symbols"), out.get("moved_symbols"),
                  out.get("nifty_readings"), out.get("nifty_moved")))
        if new == "closed" or old == "closed":
            logger.warning(msg)
        elif new in ("open", "unknown"):
            logger.info(msg)
    except Exception:
        return


def status(now: Optional[datetime] = None) -> dict:
    """{'status': 'open'|'closed'|'unknown'|'pending'|'disabled', ...counts}.

    pending  = before the window ends and nothing proven yet (callers keep the calendar's answer).
    unknown  = window over, no moves, but too little data to call it closed (callers keep the calendar's answer).
    closed   = window over, no move anywhere, enough data (or Nifty) observed: treat today as a non-trading day.
    """
    try:
        if not ENABLED:
            return {"status": "disabled", "closed": False}
        ist = _now_ist(now)
        if ist.weekday() >= 5:
            return {"status": "unknown", "closed": False, "reason": "weekend"}
        t = ist.time()
        with _lock:
            st = _day_state(ist)
            observed = len(st["first"])
            moved = len(st["moved"])
            out = {
                "day": st["day"],
                "observed_symbols": observed,
                "moved_symbols": moved,
                "nifty_seen": bool(st["nifty_seen"]),
                "nifty_readings": int(st["nifty_obs"]),
                "nifty_moved": bool(st["nifty_moved"]),
                "window": f"{WINDOW_START.strftime('%H:%M')}-{WINDOW_END.strftime('%H:%M')} IST",
            }
            if st["proven_open"]:
                out.update(status="open", closed=False)
            elif t < WINDOW_START:
                out.update(status="pending", closed=False)
            elif t < WINDOW_END:
                out.update(status="pending", closed=False)
            elif (observed - len([k for k in st["first"] if k in _NIFTY_KEYS])) >= MIN_SYMBOLS \
                    or st["nifty_obs"] >= NIFTY_MIN_OBS:
                out.update(status="closed", closed=True)
            else:
                out.update(status="unknown", closed=False)
            _log_transition(st, out)
            return out
    except Exception:
        return {"status": "unknown", "closed": False}


def is_closed_today(now: Optional[datetime] = None) -> bool:
    return bool(status(now).get("closed"))


# ---- Nifty feed (group 320) -------------------------------------------------------------------------------------
# The "Nifty unchanged" half of the rule only counts if something calls observe("NIFTY", price). Nothing did, so in
# practice the verdict rested on stock ticks alone. The service registers a function that returns the current Nifty 50
# price (main.py: its own /quote path for ^NSEI) and the AngelOne feed loop calls probe_nifty() once per cycle; it
# polls at most every NIFTY_PROBE_S (60 s) and only while it can matter: inside the proof window, or while the day
# is judged closed (so the first real Nifty move still overturns the verdict). Never raises, never blocks the caller
# for longer than the fetcher takes (the feed loop runs it off the event loop).
NIFTY_PROBE_S = max(5, _env_int("SESSION_NIFTY_PROBE_S", 60))
_nifty_fetcher = None
_nifty_last_probe = 0.0


def register_nifty_fetcher(fn) -> None:
    """fn() -> current Nifty 50 price (float) or None. Pass None to unregister."""
    global _nifty_fetcher
    _nifty_fetcher = fn


def reset_nifty_probe() -> None:
    """Test helper."""
    global _nifty_last_probe
    _nifty_last_probe = 0.0


def probe_nifty(now: Optional[datetime] = None, mono: Optional[float] = None) -> bool:
    """Feed one Nifty price into observe() when it can matter. Returns True when a price was observed."""
    global _nifty_last_probe
    try:
        if not ENABLED or _nifty_fetcher is None:
            return False
        ist = _now_ist(now)
        if ist.weekday() >= 5 or ist.time() < WINDOW_START:
            return False
        st = status(now)
        if st.get("status") == "open":
            return False          # proven open for the day: Nifty can add nothing
        import time as _t
        t_mono = _t.monotonic() if mono is None else mono
        if _nifty_last_probe and (t_mono - _nifty_last_probe) < NIFTY_PROBE_S:
            return False
        _nifty_last_probe = t_mono
        price = _nifty_fetcher()
        if _num(price) is None:
            return False
        observe("NIFTY", price, now)
        return True
    except Exception:
        return False


# ---- Own Nifty probe thread (group 321) -----------------------------------------------------------------------
# probe_nifty() was only called from the AngelOne feed loop, so with ANGELONE_WS_FEED_ENABLED=0 (or the feed down) the
# Nifty half of the proof never ran. This daemon thread calls it independently (rate limited inside probe_nifty).
_probe_thread = None
NIFTY_THREAD_ENABLED = _env_bool("SESSION_NIFTY_THREAD", True)


def _probe_loop(stop: threading.Event, every_s: float) -> None:
    while not stop.is_set():
        try:
            probe_nifty()
        except Exception:
            pass
        stop.wait(every_s)


def start_probe_thread(every_s: float = 20.0):
    """Idempotent. Returns the stop Event (or None when disabled / already running)."""
    global _probe_thread
    try:
        if not ENABLED or not NIFTY_THREAD_ENABLED or _nifty_fetcher is None:
            return None
        if _probe_thread is not None and _probe_thread[0].is_alive():
            return None
        stop = threading.Event()
        th = threading.Thread(target=_probe_loop, args=(stop, max(5.0, float(every_s))), daemon=True, name="session-nifty-probe")
        _probe_thread = (th, stop)
        th.start()
        return stop
    except Exception:
        return None
