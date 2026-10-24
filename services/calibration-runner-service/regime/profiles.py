"""State -> overlay entries. A profile only ASKS; the trading service's kit still judges every entry against its SAVED value and refuses anything
that is not a tightening. This module also refuses locally (clamp), so one loose dial can never make the service reject the whole all-or-nothing call.

Which dials may be touched is fixed by the catalog (`overlay=True`); a dial that is not listed there is not listed here either.
Gates, STOPOUT_DAY_PAUSE_N, ACCOUNT_MAX_DAILY_LOSS_PCT and DEFAULT_RISK_PER_TRADE_PCT are never in a profile."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Mapping, Optional

from . import STATES
from .classifier import CLASSIFIER_VERSION

PROFILE_VERSION = "p1"
VERSION = f"{CLASSIFIER_VERSION}-{PROFILE_VERSION}"        # what trades are stamped with (kit: 1-24 chars of letters, digits . _ -)
IST = timezone(timedelta(hours=5, minutes=30))             # India has no daylight saving: a fixed offset needs no tz database
EXPIRY_IST = (15, 30)                                      # every overlay ends at 15:30 IST of the day it was made

SERVICES = ("real-trade", "position-stocks")

# key -> (kind, which direction is LOOSER). Mirrors the catalog; test_regime_package checks it against both catalogs.
DIALS: Dict[str, Dict[str, Any]] = {
    "MARKET_STATE_RISK_MULT": {"kind": "float", "looser": "higher", "services": ("real-trade", "position-stocks")},
    "MARKET_STATE_CONVICTION_ADD": {"kind": "float", "looser": "lower", "services": ("real-trade",)},
    "OPENING_ENTRY_NOT_BEFORE_IST": {"kind": "time", "looser": "earlier", "services": ("real-trade",)},
    "RISK_MAX_ENTRIES_PER_DAY": {"kind": "int", "looser": "higher", "services": ("real-trade",)},
    "ENTRY_LAST_TIME_IST": {"kind": "time", "looser": "later", "services": ("real-trade",)},
    "MAX_CONCURRENT_SCALP_POSITIONS": {"kind": "int", "looser": "higher", "services": ("position-stocks",)},
    "SCALP_ENTRY_CUTOFF_IST": {"kind": "time", "looser": "later", "services": ("position-stocks",)},
    # Short selling step 6: how much short exposure today allows (0 = none). Position Stocks only; a long is never touched.
    "MARKET_STATE_SHORT_MULT": {"kind": "float", "looser": "higher", "services": ("position-stocks",)},
}

# Dials that follow the plan's short-exposure table, not the severity ladder: shorts are OFF in calm (a rising market squeezes them) and in choppy
# (both sides get chopped), then allowed again, smaller, when the market is stressed or in panic. So their values do not tighten from state to state.
STATE_SPECIFIC_DIALS = ("MARKET_STATE_SHORT_MULT",)

# state -> value per dial; None = leave the saved value alone. Normal changes nothing (it only stamps). Calm changes only the short dial (shorts off).
# Short dial (plan table): calm 0 (off), normal / stressed unchanged (the saved SHORT_RISK_MULT already makes a short half a long), choppy 0 (off),
# panic 0.5 (a quarter of a long). The "tighter stop" for stressed / panic shorts is not a dial: position-stocks reads the stamped market state and applies SHORT_STOP_MULT_STRESSED / SHORT_STOP_MULT_PANIC itself (E101).
PROFILE: Dict[str, Dict[str, Optional[Any]]] = {
    "calm": {"MARKET_STATE_SHORT_MULT": 0.0},
    "normal": {},
    "choppy": {"MARKET_STATE_RISK_MULT": 0.8, "MARKET_STATE_CONVICTION_ADD": 3, "OPENING_ENTRY_NOT_BEFORE_IST": "09:30",
               "RISK_MAX_ENTRIES_PER_DAY": 4, "ENTRY_LAST_TIME_IST": "13:30", "MAX_CONCURRENT_SCALP_POSITIONS": 4, "SCALP_ENTRY_CUTOFF_IST": "13:30",
               "MARKET_STATE_SHORT_MULT": 0.0},
    "stressed": {"MARKET_STATE_RISK_MULT": 0.6, "MARKET_STATE_CONVICTION_ADD": 6, "OPENING_ENTRY_NOT_BEFORE_IST": "09:45",
                 "RISK_MAX_ENTRIES_PER_DAY": 3, "ENTRY_LAST_TIME_IST": "13:00", "MAX_CONCURRENT_SCALP_POSITIONS": 3, "SCALP_ENTRY_CUTOFF_IST": "13:00"},
    "panic": {"MARKET_STATE_RISK_MULT": 0.4, "MARKET_STATE_CONVICTION_ADD": 10, "OPENING_ENTRY_NOT_BEFORE_IST": "10:15",
              "RISK_MAX_ENTRIES_PER_DAY": 2, "ENTRY_LAST_TIME_IST": "12:00", "MAX_CONCURRENT_SCALP_POSITIONS": 2, "SCALP_ENTRY_CUTOFF_IST": "12:00",
              "MARKET_STATE_SHORT_MULT": 0.5},
}


def _num(kind: str, raw: Any) -> float:
    """Comparable number for a dial value: minutes after midnight for a time, else the float. Raises ValueError for junk."""
    if kind == "time":
        h, m = str(raw).strip().split(":")
        h, m = int(h), int(m)
        if not (0 <= h <= 23 and 0 <= m <= 59):
            raise ValueError(f"bad time {raw!r}")
        return float(h * 60 + m)
    x = float(str(raw).strip())
    if x != x:
        raise ValueError("nan")
    return x


def canonical(kind: str, raw: Any) -> str:
    if kind == "time":
        n = int(_num("time", raw))
        return f"{n // 60:02d}:{n % 60:02d}"
    if kind == "int":
        return str(int(round(float(raw))))
    x = float(raw)
    return str(int(x)) if x == int(x) and abs(x) >= 1 else f"{x:g}"


def compare(key: str, saved: Any, new: Any) -> str:
    """TIGHTENS / SAME / LOOSENS of `new` against `saved` for this dial (the same word the trading service uses)."""
    d = DIALS[key]
    a, b = _num(d["kind"], saved), _num(d["kind"], new)
    if abs(a - b) < 1e-9:
        return "SAME"
    higher_is_looser = d["looser"] in ("higher", "later")
    return "LOOSENS" if (b > a) == higher_is_looser else "TIGHTENS"


def expiry(now: datetime) -> Optional[datetime]:
    """15:30 IST of `now`'s IST day, or None when that moment has passed (nothing to apply late in the day)."""
    n = now.astimezone(IST)
    end = n.replace(hour=EXPIRY_IST[0], minute=EXPIRY_IST[1], second=0, microsecond=0)
    return end if end > n else None


def build_overlay(service: str, state: str, saved: Mapping[str, Any], now: datetime) -> Dict[str, Any]:
    """The body for POST /control/overlay on one service: {"state","version","until","entries","skipped"}.
    `saved` = the SAVED value per switch key as the service reports it. Dials whose saved value is unknown are skipped (never guessed);
    dials already as tight as, or tighter than, the profile are left out. entries == {} is a valid stamp-only call.
    `until` is None when it is already past 15:30 IST: the caller must not post then."""
    if service not in SERVICES:
        raise ValueError(f"unknown service {service!r}")
    if state not in STATES:
        raise ValueError(f"unknown state {state!r}")
    entries: Dict[str, str] = {}
    skipped: List[Dict[str, str]] = []
    for key, want in PROFILE[state].items():
        d = DIALS[key]
        if service not in d["services"] or want is None:
            continue
        have = saved.get(key)
        if have in (None, ""):
            skipped.append({"key": key, "why": "saved value unknown"})
            continue
        try:
            c = compare(key, have, want)
        except (ValueError, TypeError):
            skipped.append({"key": key, "why": f"saved value {have!r} not readable"})
            continue
        if c == "TIGHTENS":
            entries[key] = canonical(d["kind"], want)
        else:
            skipped.append({"key": key, "why": "saved value already as tight" if c == "SAME" else "saved value already tighter"})
    until = expiry(now)
    return {"state": state, "version": VERSION, "until": until.isoformat(timespec="seconds") if until else None, "entries": entries, "skipped": skipped}


def table() -> List[Dict[str, Any]]:
    """The whole profile as rows, for the brief and the page: one row per state."""
    return [{"state": s, "dials": dict(PROFILE[s])} for s in STATES]
