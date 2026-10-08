"""
services/market-data-service/angelone_budget.py  (group 211)

One shared AngelOne budget with priority lanes and a global 403 cooldown.

Why this exists (item 1 of the 2026-10-06 open list): every AngelOne caller in this service -- the 500-symbol
feed poll, the 2,500-symbol movers sweep, per-symbol /quote, /history candles, /quotes/bulk -- shared the same
quote/candle buckets with no notion of who matters most, and each endpoint kept its OWN cooldown. After a 403
"exceeding access rate" on (say) candles, quote callers carried on, each making its own call, and the
open-position quotes real-trade-service needs were queued behind background sweeps.

What this module adds (nothing here talks to AngelOne itself; angelone_client.py calls it):

  * LANES. POSITION (symbols held in an open position), CANDIDATE (request-driven lookups of anything else:
    /quote, /quotes/bulk, /history), BACKGROUND (sweeps: movers, the cold part of the feed poll). A lane may
    only take a token from a shared bucket while the bucket still holds a reserve for the lanes above it:
    POSITION needs 0 spare tokens, CANDIDATE needs 25 % of the bucket's burst capacity spare, BACKGROUND 50 %.
    So a sweep can never drain the bucket and leave a held position waiting. A lane that cannot get in within
    its max_wait is SHED (the caller falls back, same as the existing fail-closed candle path).
  * ONE GLOBAL COOLDOWN. A rate-limit 403/429 from ANY AngelOne endpoint trips a cooldown that every caller
    checks before sending anything (30 s, doubling to at most 60 s if it trips again within 5 minutes).
    In-flight calls that come back 403 while the cooldown is already running do not escalate it.
  * LANE LOOKUP. POSITION symbols come from the shared DB (trade_positions + scalp_positions open rows),
    refreshed on a background thread every ANGELONE_POSITION_LANE_REFRESH_S (15; 0 = off) so no request ever
    waits on the DB. Symbols recently looked up through single-symbol /quote form the "hot" set the feed poll
    keeps fresh every cycle.

group227 (item 1 of the 2026-10-07 10:33 IST log review): the cooldown is now kept PER FAMILY, "quote" (ltpData / quote
batches / the feed poll / gainers) and "candle" (getCandleData). AngelOne limits each endpoint on its own, and the log
showed quotes answering normally right up to a candle 403; yet that 403 paused every quote caller for 30-60 s, which
sent the whole feed to a saturated yfinance (18 s timeouts, 347 unpriced symbols, ReadTimeouts in real-trade). A candle
403 now pauses only candle callers, a quote 403 only quote callers. ANGELONE_SPLIT_COOLDOWN=0 restores the single
shared cooldown.

Callers that pass no lane (lane=None) keep the old behaviour exactly, except that they too honour the global
cooldown. ANGELONE_BUDGET=0 turns the whole module off (no lanes, no global cooldown).
All env reads are blank-safe: ((os.getenv(X) or "").strip() or default).
"""
from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
from typing import Iterable, List, Optional

logger = logging.getLogger("angelone-budget")

POSITION = "position"
CANDIDATE = "candidate"
BACKGROUND = "background"
LANES = (POSITION, CANDIDATE, BACKGROUND)


# ── env helpers (blank / invalid -> default) ────────────────────────────────
def _env_str(name: str, default: str) -> str:
    return (os.getenv(name) or "").strip() or default


def _env_float(name: str, default: float, lo: Optional[float] = None, hi: Optional[float] = None) -> float:
    try:
        v = float(_env_str(name, str(default)))
    except (TypeError, ValueError):
        return default
    if v != v:  # NaN
        return default
    if lo is not None and v < lo:
        return default
    if hi is not None and v > hi:
        return default
    return v


def enabled() -> bool:
    return _env_str("ANGELONE_BUDGET", "1").lower() not in ("0", "false", "off", "no")


def _cooldown_base_s() -> float:
    return _env_float("ANGELONE_GLOBAL_COOLDOWN_S", 30.0, 1.0, 600.0)


def _cooldown_max_s() -> float:
    return max(_cooldown_base_s(), _env_float("ANGELONE_GLOBAL_COOLDOWN_MAX_S", 60.0, 1.0, 1800.0))


_ESCALATE_WINDOW_S = 300.0


def reserve_fraction(lane: Optional[str]) -> float:
    """Share of a bucket's burst capacity that must stay free for the lanes above `lane`."""
    if lane == CANDIDATE:
        return _env_float("ANGELONE_LANE_RESERVE_CANDIDATE", 0.25, 0.0, 0.9)
    if lane == BACKGROUND:
        return _env_float("ANGELONE_LANE_RESERVE_BACKGROUND", 0.5, 0.0, 0.9)
    return 0.0


# ── global cooldown ─────────────────────────────────────────────────────────
_lock = threading.Lock()
QUOTE = "quote"
CANDLE = "candle"
FAMILIES = (QUOTE, CANDLE)


def _new_cool() -> dict:
    return {"until": 0.0, "dur": 0.0, "last_trip": 0.0, "trips": 0}


_cool = {QUOTE: _new_cool(), CANDLE: _new_cool()}
_suppressed = 0
_last_trip_endpoint: Optional[str] = None
_probe_next = 0.0                       # group231: earliest time of the next held-position probe
_probes = {"allowed": 0, "denied": 0}
_counts = {lane: {"admitted": 0, "shed": 0, "skipped_cooldown": 0} for lane in LANES}
_counts["unclassified"] = {"admitted": 0, "shed": 0, "skipped_cooldown": 0}


def _bucket_name(lane: Optional[str]) -> str:
    return lane if lane in LANES else "unclassified"


def split_enabled() -> bool:
    """Per-family cooldowns (default). ANGELONE_SPLIT_COOLDOWN=0: one shared cooldown for every endpoint (group 211)."""
    return _env_str("ANGELONE_SPLIT_COOLDOWN", "1").lower() not in ("0", "false", "off", "no")


def family_for(endpoint: Optional[str]) -> str:
    """Cooldown family of an endpoint / rate-limiter provider name: anything with "candle" in it is CANDLE, the rest
    (quote, quote(batch), gainers, angelone_quote ...) is QUOTE. With the split off everything is QUOTE (one state)."""
    if split_enabled() and "candle" in (endpoint or "").lower():
        return CANDLE
    return QUOTE


def _families(family: Optional[str]) -> tuple:
    """The family states a check covers: one named family, or every family when none is named (status, old callers)."""
    if family is None:
        return FAMILIES
    return (family if family in FAMILIES else QUOTE,)


def in_global_cooldown(family: Optional[str] = None) -> bool:
    """True while the cooldown of `family` runs (family None: while ANY family's cooldown runs)."""
    if not enabled():
        return False
    now = time.time()
    return any(now < _cool[f]["until"] for f in _families(family))


def cooldown_remaining(family: Optional[str] = None) -> float:
    if not enabled():
        return 0.0
    now = time.time()
    return max(0.0, max(_cool[f]["until"] for f in _families(family)) - now)


def trip(endpoint: str) -> float:
    """Record a rate-limit answer from `endpoint`. Starts the cooldown of that endpoint's family only. Returns the
    cooldown seconds just started, or 0.0 when nothing new started (budget off, or that family's cooldown is already
    running -- late answers to calls that were already in flight must not escalate it)."""
    global _suppressed, _last_trip_endpoint
    if not enabled():
        return 0.0
    fam = family_for(endpoint)
    now = time.time()
    with _lock:
        st = _cool[fam]
        if now < st["until"]:
            _suppressed += 1
            return 0.0
        base, cap = _cooldown_base_s(), _cooldown_max_s()
        if st["last_trip"] and (now - st["last_trip"]) <= _ESCALATE_WINDOW_S and st["dur"] > 0:
            dur = min(cap, max(base, st["dur"] * 2.0))
        else:
            dur = base
        st["dur"] = dur
        st["until"] = now + dur
        st["last_trip"] = now
        st["trips"] += 1
        _last_trip_endpoint = endpoint
        trips_now = sum(v["trips"] for v in _cool.values())
    if fam == CANDLE:
        _slow_candle_bucket(dur)
    who = "ALL AngelOne callers" if not split_enabled() else ("AngelOne %s callers" % fam)
    logger.warning(
        "AngelOne budget: rate-limit answer from %s - %s skip AngelOne for %.0fs "
        "(trip #%d; escalates to at most %.0fs if it trips again within %.0fs)",
        endpoint, who, dur, trips_now, cap, _ESCALATE_WINDOW_S,
    )
    return dur


# group251: after a candle trip the candle bucket is emptied and held empty for the cooldown, then refilled at
# ANGELONE_CANDLE_SLOWDOWN_FACTOR (default 0.5) of its normal rate for ANGELONE_CANDLE_SLOWDOWN_S (default 600) more
# seconds. Quote buckets are untouched. ANGELONE_CANDLE_SLOWDOWN_S=0 or factor>=1 turns it off.
def _slow_candle_bucket(cooldown_s: float) -> None:
    try:
        factor = _env_float("ANGELONE_CANDLE_SLOWDOWN_FACTOR", 0.5, 0.05, 1.0)
        slow_s = _env_float("ANGELONE_CANDLE_SLOWDOWN_S", 600.0, 0.0, 86400.0)
        if slow_s <= 0.0 or factor >= 1.0:
            return
        import rate_limiter as _rl
        _rl.slow_down("angelone_candle", cooldown_s, factor, slow_s)
    except Exception as e:  # noqa: BLE001
        logger.debug("angelone_budget: candle slowdown skipped: %s", e)


def skip(lane: Optional[str] = None, family: Optional[str] = None) -> bool:
    """True when the cooldown of `family` is running (the caller must not send anything). Counts the skip.
    family None: any running cooldown counts (the old shared behaviour)."""
    if not in_global_cooldown(family):
        return False
    with _lock:
        _counts[_bucket_name(lane)]["skipped_cooldown"] += 1
    return True


# ── group231: held-position probe during a QUOTE cooldown ───────────────────
# A quote 403 made EVERY quote caller skip AngelOne for 30-60 s, including the priced-every-8-s held positions,
# which then went to the saturated Yahoo path (ReadTimeouts) with real money at stake. During a quote-family
# cooldown a POSITION-lane call may send ONE request per ANGELONE_POSITION_PROBE_INTERVAL_S (default 1; 0 = off),
# starting ANGELONE_POSITION_PROBE_MIN_AGE_S (default 2) after the trip. A probe that is itself answered 403 does
# not extend any cooldown and pauses probing for ANGELONE_POSITION_PROBE_BACKOFF_S (default 10).
# Candle cooldowns and every other lane are unchanged.
def position_probe_allowed(lane: Optional[str], family: str = QUOTE) -> bool:
    """True when this POSITION-lane quote call may be sent although the quote cooldown runs. Takes the probe slot."""
    global _probe_next
    if lane != POSITION or family != QUOTE or not enabled():
        return False
    interval = _env_float("ANGELONE_POSITION_PROBE_INTERVAL_S", 1.0, 0.0, 600.0)
    if interval <= 0.0:
        return False
    min_age = _env_float("ANGELONE_POSITION_PROBE_MIN_AGE_S", 2.0, 0.0, 600.0)
    now = time.time()
    with _lock:
        if now - _cool[QUOTE]["last_trip"] < min_age or now < _probe_next:
            return False
        _probe_next = now + interval
        _probes["allowed"] += 1
    return True


def position_probe_denied() -> None:
    """A probe was answered with a rate-limit response: stop probing for a while."""
    global _probe_next
    backoff = _env_float("ANGELONE_POSITION_PROBE_BACKOFF_S", 10.0, 0.0, 3600.0)
    with _lock:
        _probe_next = time.time() + backoff
        _probes["denied"] += 1


# ── lane admission ──────────────────────────────────────────────────────────
def _count(lane: Optional[str], key: str) -> None:
    with _lock:
        _counts[_bucket_name(lane)][key] += 1


async def admit(lane: Optional[str], provider: str, weight: float = 1.0, max_wait: float = 20.0) -> bool:
    """Wait (up to max_wait seconds) until `provider`'s bucket holds `weight` tokens PLUS this lane's reserve.
    Does not consume the tokens (the caller's normal rate_limiter call does that next). False = shed the call;
    also False if the global cooldown starts while waiting. lane None / POSITION / budget off: True at once."""
    if not enabled() or lane not in (CANDIDATE, BACKGROUND):
        _count(lane, "admitted")
        return True
    frac = reserve_fraction(lane)
    if frac <= 0.0:
        _count(lane, "admitted")
        return True
    try:
        import rate_limiter as _rl
    except Exception:  # noqa: BLE001
        return True
    deadline = time.time() + max(0.0, max_wait)
    while True:
        if in_global_cooldown(family_for(provider)):
            _count(lane, "skipped_cooldown")
            return False
        try:
            tokens, cap = _rl.bucket_level(provider)
        except Exception:  # noqa: BLE001
            return True
        need = min(weight + frac * cap, cap)
        if tokens >= need:
            _count(lane, "admitted")
            return True
        left = deadline - time.time()
        if left <= 0:
            _count(lane, "shed")
            return False
        await asyncio.sleep(min(0.25, left))


# ── symbol -> lane ──────────────────────────────────────────────────────────
def _clean(sym: str) -> str:
    return (sym or "").upper().replace(".NS", "").replace(".BO", "").strip()


_POSITION_QUERIES = (
    "SELECT DISTINCT symbol FROM trade_positions WHERE status IN ('OPEN', 'PARTIALLY_CLOSED', 'PENDING_EXIT')",
    "SELECT DISTINCT symbol FROM scalp_positions WHERE status IN ('OPEN', 'EXIT_LEGS_REJECTED')",
)
_pos_lock = threading.Lock()
_pos_symbols: frozenset = frozenset()
_pos_loaded_at = 0.0
_pos_refreshing = False


def _load_position_symbols() -> Optional[set]:
    """Held symbols from the shared DB, or None when the DB is unreachable. A missing table (one service not
    deployed / not migrated yet) just contributes nothing. Never raises."""
    try:
        from sqlalchemy import text
        from kv_cache import _get_neon
        engine = _get_neon()
    except Exception as e:  # noqa: BLE001
        logger.debug("angelone_budget: no DB engine for position lane: %s", e)
        return None
    if engine is None:
        return None
    out: set = set()
    ok = False
    for sql in _POSITION_QUERIES:
        try:
            with engine.connect() as conn:
                for row in conn.execute(text(sql)):
                    c = _clean(str(row[0] or ""))
                    if c:
                        out.add(c)
            ok = True
        except Exception as e:  # noqa: BLE001
            logger.debug("angelone_budget: position query failed (%s): %s", sql.split(" FROM ")[1].split(" ")[0], e)
    return out if ok else None


def _refresh_positions() -> None:
    global _pos_symbols, _pos_loaded_at, _pos_refreshing
    try:
        got = _load_position_symbols()
        with _pos_lock:
            if got is not None:
                _pos_symbols = frozenset(got)
            _pos_loaded_at = time.time()   # also after a failure: do not hammer an unreachable DB
    except Exception:  # noqa: BLE001
        with _pos_lock:
            _pos_loaded_at = time.time()
    finally:
        with _pos_lock:
            _pos_refreshing = False


def position_symbols() -> frozenset:
    """Cached set of held symbols (clean spelling). Returns at once; a stale cache is refreshed on a daemon
    thread. Empty until the first refresh lands, and always empty when the lane lookup is off."""
    global _pos_refreshing
    interval = _env_float("ANGELONE_POSITION_LANE_REFRESH_S", 15.0, 0.0, 3600.0)
    if interval <= 0.0 or not enabled():
        return frozenset()
    start = False
    with _pos_lock:
        if (time.time() - _pos_loaded_at) >= interval and not _pos_refreshing:
            _pos_refreshing = True
            start = True
        cur = _pos_symbols
    if start:
        try:
            threading.Thread(target=_refresh_positions, daemon=True, name="angelone-budget-positions").start()
        except Exception:  # noqa: BLE001
            with _pos_lock:
                _pos_refreshing = False
    return cur


_demand: dict = {}
_demand_lock = threading.Lock()
_DEMAND_MAX = 3000


def note_demand(symbol: str) -> None:
    c = _clean(symbol)
    if not c:
        return
    now = time.time()
    with _demand_lock:
        _demand[c] = now
        if len(_demand) > _DEMAND_MAX:
            cutoff = now - _env_float("ANGELONE_HOT_DEMAND_WINDOW_S", 120.0, 1.0, 3600.0)
            for k in [k for k, t in _demand.items() if t < cutoff]:
                _demand.pop(k, None)


def hot_symbols(limit: int = 100) -> List[str]:
    """Symbols looked up within ANGELONE_HOT_DEMAND_WINDOW_S (120), newest first, at most `limit`."""
    cutoff = time.time() - _env_float("ANGELONE_HOT_DEMAND_WINDOW_S", 120.0, 1.0, 3600.0)
    with _demand_lock:
        items = sorted(((t, s) for s, t in _demand.items() if t >= cutoff), reverse=True)
    return [s for _t, s in items[:max(0, limit)]]


def lane_for(symbol: str, demand: bool = True) -> Optional[str]:
    """Lane for a request-driven lookup of one symbol: POSITION if held, else CANDIDATE. With demand=True
    (single-symbol /quote lookups) the symbol also joins the "hot" set the feed poll refreshes every cycle;
    bulk and candle lookups pass demand=False so a market-wide scan cannot flood that set. None when the
    budget is off (caller keeps the legacy path)."""
    if not enabled():
        return None
    if demand:
        note_demand(symbol)
    return POSITION if _clean(symbol) in position_symbols() else CANDIDATE


def lane_for_symbols(symbols: Iterable[str], demand: bool = False) -> Optional[str]:
    """POSITION if any symbol is held, else CANDIDATE. demand=True also records every symbol as hot.
    None when the budget is off."""
    if not enabled():
        return None
    held = position_symbols()
    lane = CANDIDATE
    for s in symbols:
        if demand:
            note_demand(s)
        if _clean(s) in held:
            lane = POSITION
    return lane


# ── introspection / tests ───────────────────────────────────────────────────
def stats() -> dict:
    with _lock:
        counts = {k: dict(v) for k, v in _counts.items()}
        out = {
            "enabled": enabled(),
            "global_cooldown_active": any(time.time() < v["until"] for v in _cool.values()),
            "global_cooldown_remaining_s": round(max(0.0, max(v["until"] for v in _cool.values()) - time.time()), 1),
            "split_cooldown": split_enabled(),
            "cooldowns": {
                f: {"active": time.time() < v["until"],
                    "remaining_s": round(max(0.0, v["until"] - time.time()), 1),
                    "trips": v["trips"]}
                for f, v in _cool.items()
            },
            "trips": sum(v["trips"] for v in _cool.values()),
            "suppressed_late_403s": _suppressed,
            "last_trip_endpoint": _last_trip_endpoint,
            "lanes": counts,
            "position_probes": dict(_probes),
        }
    with _pos_lock:
        out["position_symbols"] = len(_pos_symbols)
        out["position_symbols_age_s"] = round(time.time() - _pos_loaded_at, 1) if _pos_loaded_at else None
    with _demand_lock:
        out["hot_symbols"] = len(_demand)
    try:
        import rate_limiter
        out["buckets"] = {k: v for k, v in rate_limiter.stats().items() if k.startswith("angelone_")}
    except Exception:  # noqa: BLE001
        pass
    return out


def _reset() -> None:
    """Test helper: forget every cooldown, count, cached position and demand."""
    global _suppressed, _last_trip_endpoint, _probe_next
    global _pos_symbols, _pos_loaded_at, _pos_refreshing
    with _lock:
        for _f in FAMILIES:
            _cool[_f] = _new_cool()
        _suppressed = 0
        _probe_next = 0.0
        _probes["allowed"] = 0
        _probes["denied"] = 0
        _last_trip_endpoint = None
        for v in _counts.values():
            for k in v:
                v[k] = 0
    with _pos_lock:
        _pos_symbols = frozenset()
        _pos_loaded_at = 0.0
        _pos_refreshing = False
    with _demand_lock:
        _demand.clear()
