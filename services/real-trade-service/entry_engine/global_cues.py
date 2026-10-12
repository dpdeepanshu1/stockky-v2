"""entry_engine/global_cues.py - live overseas cues for the hold-or-sell decision at the close (group 331, 2026-10-12).

WHY: a carried position's next gap is set largely by what the rest of the world does between 15:30 IST and the next 09:15. At 15:00 IST that is
partly visible: Europe (DAX, FTSE) opened ~12:30 IST and is live, Asia (Nikkei, Hang Seng) has just closed with its final move, and US index
futures (ES=F, NQ=F) trade almost around the clock. Their % change on the day is a LIVE cue, unlike last night's US-sector close.

SOURCE: ONE call to market-data GET /global-cues?symbols=<GLOBAL_CUE_SYMBOLS> (group 331 route). It is not /quote: that waterfall is built for NSE
equities and would turn "ES=F" into "ES=F.NS". The route asks Yahoo directly, caches per symbol, drops a market whose last bar is older than a day
(holiday) and lists what it could not read under "missing". Each row's day_change_pct (or price vs previous_close) is the reading; a row older than
GLOBAL_CUE_MAX_AGE_S (when it carries an age), outside +/-15 % or without a number is dropped. Readings are cached GLOBAL_CUE_CACHE_TTL_S here as well
(a failed fetch is cached as empty for FAILURE_RETRY_S).
RULE (reason()): needs >= GLOBAL_CUE_MIN_READINGS readings. Block when their average <= CARRY_GLOBAL_MIN_AVG_PCT, or any US-futures cue <= CARRY_GLOBAL_US_FUT_MIN_PCT.
HONEST LIMIT: whether Yahoo answers for these symbols from the VM at 15:00 could not be verified offline. A symbol it cannot serve is simply absent; with
fewer than GLOBAL_CUE_MIN_READINGS readings there is no opinion. GIFT Nifty is not included (no verified source).
FAIL OPEN, ALWAYS. Blocking: wrap snapshot() in asyncio.to_thread. Off with GLOBAL_CUES_ENABLED=false.
"""
from __future__ import annotations

import logging
import math
import threading
import time
from typing import Optional

import config

logger = logging.getLogger("real-trade-global-cues")

FAILURE_RETRY_S = 120.0
_lock = threading.Lock()
_state: dict = {"ts": -1e9, "snap": None}


def reset() -> None:
    with _lock:
        _state.update({"ts": -1e9, "snap": None})


def _num(v) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def parse_row(body, max_age_s: Optional[float] = None) -> Optional[float]:
    """The day's % change from a /global-cues row, or None when unusable / too old / implausible. Never raises."""
    try:
        if not isinstance(body, dict):
            return None
        age = body.get("age_s")
        lim = float(config.GLOBAL_CUE_MAX_AGE_S if max_age_s is None else max_age_s)
        if isinstance(age, (int, float)) and not isinstance(age, bool) and math.isfinite(age) and lim > 0 and age > lim:
            return None
        chg = _num(body.get("day_change_pct"))
        if chg is None:
            price, prev = _num(body.get("price")), _num(body.get("previous_close"))
            if price and prev and price > 0 and prev > 0:
                chg = (price - prev) / prev * 100.0
        if chg is None or abs(chg) > 15.0:
            return None
        return chg
    except Exception:  # noqa: BLE001
        return None


def parse_body(body) -> dict:
    """{symbol: day % change} from a /global-cues body ({"rows": {SYMBOL: row}}); unusable rows are left out. Never raises."""
    out: dict = {}
    try:
        rows = body.get("rows") if isinstance(body, dict) else None
        if not isinstance(rows, dict):
            return out
        for sym, row in rows.items():
            v = parse_row(row)
            if v is not None:
                out[str(sym).strip().upper()] = v
    except Exception:  # noqa: BLE001
        return {}
    return out


def _symbols() -> list:
    return [x.strip().upper() for x in str(config.GLOBAL_CUE_SYMBOLS or "").split(",") if x.strip()]


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


def snapshot() -> Optional[dict]:
    """{symbol: day % change} of the cues that read, cached; None when the feature is off or nothing read. Blocks on a cache miss. Never raises."""
    try:
        if not config.GLOBAL_CUES_ENABLED:
            return None
        now = time.monotonic()
        with _lock:
            age = now - _state["ts"]
            if age < (float(config.GLOBAL_CUE_CACHE_TTL_S) if _state["snap"] else FAILURE_RETRY_S):
                return _state["snap"]
        try:
            snap = _fetch() or None
        except Exception as e:  # noqa: BLE001
            logger.info("global_cues: fetch failed (%s: %s) - no opinion", type(e).__name__, e)
            snap = None
        with _lock:
            _state.update({"ts": time.monotonic(), "snap": snap})
        return snap
    except Exception:  # noqa: BLE001
        return None


def reason(snap: Optional[dict]) -> Optional[str]:
    """A "CARRY_GLOBAL_WEAK:..." reason or None. Needs GLOBAL_CUE_MIN_READINGS readings. Never raises."""
    try:
        if not config.GLOBAL_CUES_ENABLED or not snap or len(snap) < max(1, int(config.GLOBAL_CUE_MIN_READINGS)):
            return None
        fut = {x.strip().upper() for x in str(config.GLOBAL_CUE_US_FUTURES or "").split(",") if x.strip()}
        lim_f = float(config.CARRY_GLOBAL_US_FUT_MIN_PCT or 0)
        if lim_f < 0:
            for sym in sorted(fut):
                if sym in snap and snap[sym] <= lim_f:
                    return f"CARRY_GLOBAL_WEAK: US futures {sym} {snap[sym]:+.2f}% <= {lim_f:+.2f}% - weak overseas lead into tomorrow's open"
        lim_a = float(config.CARRY_GLOBAL_MIN_AVG_PCT or 0)
        avg = sum(snap.values()) / len(snap)
        if lim_a < 0 and avg <= lim_a:
            return f"CARRY_GLOBAL_WEAK: global cues average {avg:+.2f}% over {len(snap)} markets <= {lim_a:+.2f}% - weak overseas lead"
        return None
    except Exception:  # noqa: BLE001
        return None
