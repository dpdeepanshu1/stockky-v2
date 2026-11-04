"""
regime_adaptive.py - round 206: the regime constants adjust themselves from the market's own recent record.

WHY: ENTRY_REGIME_MIN_SCORE, ENTRY_MIN_REWARD_RISK, CANDIDATE_MIN_CONVICTION, CANDIDATE_DOWNTREND_6M_PCT and
CANDIDATE_MIN_BULLISH_TF were hand-set numbers that went stale every 30 days. A volatile market moves faster than a
monthly review. This module reads the index over seven horizons (yesterday, 7 days, 1, 2, 3, 6 months, 1 year), how
jumpy the market is now against its own year, and how the system's own closed REAL trades did lately, and moves each
constant inside a hard band around the owner's value.

(CANDIDATE_OVEREXTENDED_52W_TOP_PCT is not touched: the rule that read it was removed on 31 Oct 2026 and nothing
consumes it any more.)

THE RULES (all pure functions below, every number is a named constant):
  tone     -1..+1  weighted mean over the horizons of (index return / the return a normal day-to-day swing would give
                   over that many sessions), each clipped to +-2 and scaled to +-1. Up-trend = positive.
  stress    0..1   how much louder the last 20 sessions are than the whole year (vol ratio 1 -> 0, ratio 2 or more -> 1).
  feedback -1..+1  win rate of the last REGIME_ADAPTIVE_RESULT_DAYS days of closed REAL trades against 45 %; 0 until
                   REGIME_ADAPTIVE_MIN_TRADES trades exist.
  weak tone, high stress or poor results TIGHTEN; strong calm tone and good results LOOSEN (never the R:R floor).

BANDS (the owner's own governance guard-rails, scheduler/governance_check.py, are the outer limit; the inner limit is
tighter): R:R only goes UP from the owner's value (his 1:2 rule is a floor), by at most 0.5; conviction +-bounded
[base-5, base+15]; bullish timeframes base +-1; 6m downtrend inside [-20, -5]; regime score moved by [-6, +10].

SAFETY: REGIME_ADAPTIVE_MODE=off (or no fresh snapshot, or the index history cannot be read, or fewer than 70 sessions
exist) -> every function returns the owner's static value, byte for byte. Nothing here ever raises into trading.
"""
from __future__ import annotations

import logging
import math
import os
import threading
import time
from collections import deque
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

import config

logger = logging.getLogger("real-trade-regime-adaptive")

# (label, sessions back, weight). 7 calendar days = 5 sessions. Weights favour the 1-3 month window where regimes
# change; yesterday and 1 year count least (noise / too slow).
HORIZONS: Tuple[Tuple[str, int, float], ...] = (
    ("1d", 1, 0.5), ("7d", 5, 1.0), ("1m", 21, 1.5), ("2m", 42, 1.5), ("3m", 63, 1.5), ("6m", 126, 1.0), ("1y", 252, 0.5),
)
MIN_SESSIONS = 70            # need at least the 3-month horizon to say anything
VOL_SHORT = 20
GOOD_WIN_RATE = 0.45
WIN_RATE_SPAN = 0.25

ADAPTED = ("ENTRY_REGIME_MIN_SCORE", "ENTRY_MIN_REWARD_RISK", "CANDIDATE_MIN_CONVICTION",
           "CANDIDATE_DOWNTREND_6M_PCT", "CANDIDATE_MIN_BULLISH_TF")


# Retired: nothing reads it any more, so it is never warned about as "stale".
RETIRED = ("CANDIDATE_OVEREXTENDED_52W_TOP_PCT",)


def _clip(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


def _env_float(name: str, default: float) -> float:
    try:
        return float((os.getenv(name) or "").strip() or default)
    except ValueError:
        return float(default)


def mode() -> str:
    m = (os.getenv("REGIME_ADAPTIVE_MODE") or "").strip().lower()
    return m if m in ("live", "off") else "live"


# ── pure maths ───────────────────────────────────────────────────────────────────────────────────

def horizon_returns(closes: Sequence[float]) -> Dict[str, Optional[float]]:
    """Percent return of the last close against the close N sessions earlier; None where history is too short."""
    out: Dict[str, Optional[float]] = {}
    n = len(closes)
    for label, back, _w in HORIZONS:
        if n > back and closes[-1 - back] and closes[-1 - back] > 0 and closes[-1] > 0:
            out[label] = (closes[-1] / closes[-1 - back] - 1.0) * 100.0
        else:
            out[label] = None
    return out


def _daily_rets(closes: Sequence[float]) -> List[float]:
    return [(closes[i] / closes[i - 1] - 1.0) * 100.0 for i in range(1, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]


def _stdev(xs: Sequence[float]) -> Optional[float]:
    if len(xs) < 5:
        return None
    m = sum(xs) / len(xs)
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (len(xs) - 1))


def market_picture(closes: Sequence[float]) -> Optional[Dict[str, Any]]:
    """tone / stress / horizon returns from daily index closes (oldest first). None when there is not enough history."""
    closes = [float(c) for c in closes if c is not None and c == c]
    if len(closes) < MIN_SESSIONS:
        return None
    rets = _daily_rets(closes)
    vol_long = _stdev(rets[-252:])
    vol_short = _stdev(rets[-VOL_SHORT:])
    if not vol_long or vol_long <= 0 or vol_short is None:
        return None
    hr = horizon_returns(closes)
    num = den = 0.0
    for label, back, w in HORIZONS:
        r = hr.get(label)
        if r is None:
            continue
        z = r / (vol_long * math.sqrt(back))
        num += w * _clip(z / 2.0, -1.0, 1.0)
        den += w
    if den <= 0:
        return None
    ratio = vol_short / vol_long
    return {"tone": round(num / den, 3), "stress": round(_clip(ratio - 1.0, 0.0, 1.0), 3), "vol_ratio": round(ratio, 3),
            "returns": {k: (None if v is None else round(v, 2)) for k, v in hr.items()}, "sessions": len(closes)}


def result_feedback(pnls: Sequence[float], min_trades: int) -> Tuple[float, int, Optional[float]]:
    """(feedback -1..1, trades counted, win rate). Break-even trades (pnl == 0) are not counted."""
    vals = [float(p) for p in pnls if p is not None and p == p and abs(float(p)) > 1e-9]
    n = len(vals)
    if n < max(1, int(min_trades)):
        return 0.0, n, (None if n == 0 else sum(1 for v in vals if v > 0) / n)
    wr = sum(1 for v in vals if v > 0) / n
    return _clip((wr - GOOD_WIN_RATE) / WIN_RATE_SPAN, -1.0, 1.0), n, wr


def compute_values(base: Dict[str, float], tone: float, stress: float, feedback: float,
                   ret_6m_pct: Optional[float]) -> Dict[str, Any]:
    """The five adapted values plus the regime-score delta. `base` = the owner's static values. Pure."""
    weak = max(0.0, -tone)
    strong = max(0.0, tone)
    poor = max(0.0, -feedback)

    delta = int(round(_clip(-4.0 * tone + 6.0 * stress - 3.0 * feedback, -6.0, 10.0)))

    b_rr = float(base["ENTRY_MIN_REWARD_RISK"])
    rr = b_rr + 0.5 * stress + 0.3 * weak + 0.2 * poor
    rr = round(_clip(rr, b_rr, min(b_rr + 0.5, 3.0)) * 20.0) / 20.0

    b_cv = float(base["CANDIDATE_MIN_CONVICTION"])
    cv = b_cv + 6.0 * weak + 5.0 * stress + 4.0 * poor - (3.0 * strong if feedback >= 0 else 0.0)
    cv = float(round(_clip(cv, max(40.0, b_cv - 5.0), min(75.0, b_cv + 15.0))))

    b_dn = float(base["CANDIDATE_DOWNTREND_6M_PCT"])
    if ret_6m_pct is None:
        dn = b_dn
    else:
        lag = 5.0 + 3.0 * stress          # how far a stock may trail the index over 6 months before it is "broken"
        dn = 0.5 * b_dn + 0.5 * (ret_6m_pct - lag)
    dn = round(_clip(dn, -20.0, -5.0), 1)

    b_tf = int(base["CANDIDATE_MIN_BULLISH_TF"])
    tf = b_tf + (1 if tone < -0.3 else 0) - (1 if (tone > 0.4 and stress < 0.3) else 0)
    tf = int(_clip(tf, max(2, b_tf - 1), min(5, b_tf + 1)))

    return {"score_delta": delta, "ENTRY_MIN_REWARD_RISK": rr, "CANDIDATE_MIN_CONVICTION": cv,
            "CANDIDATE_DOWNTREND_6M_PCT": dn, "CANDIDATE_MIN_BULLISH_TF": tf}


# ── state ────────────────────────────────────────────────────────────────────────────────────────

_lock = threading.Lock()
_state: Dict[str, Any] = {"snapshot": None, "ts": None, "last_error": None, "last_attempt": None}
_history: "deque[Dict[str, Any]]" = deque(maxlen=60)


def _max_age_s() -> float:
    return max(0.0, _env_float("REGIME_ADAPTIVE_MAX_AGE_H", 36.0)) * 3600.0


def _fresh_snapshot() -> Optional[Dict[str, Any]]:
    if mode() != "live":
        return None
    with _lock:
        snap, ts = _state["snapshot"], _state["ts"]
    if not snap or ts is None or (time.time() - ts) > _max_age_s():
        return None
    return snap


def effective(name: str, base: Any) -> Any:
    """The value to trade with now: the adapted one when live and fresh, else `base` unchanged."""
    try:
        snap = _fresh_snapshot()
        if snap is None:
            return base
        v = snap["values"].get(name)
        return base if v is None else v
    except Exception:  # noqa: BLE001
        return base


def score_delta() -> int:
    """Shift to add to the history-based regime-score threshold (0 when off / no fresh snapshot)."""
    try:
        snap = _fresh_snapshot()
        return int(snap["values"].get("score_delta", 0)) if snap else 0
    except Exception:  # noqa: BLE001
        return 0


def is_managed(name: str) -> bool:
    """True while `name` is being set by this module (used to mute the monthly 'stale constant' warning)."""
    return name in ADAPTED and _fresh_snapshot() is not None


def _base_values() -> Dict[str, float]:
    return {"ENTRY_MIN_REWARD_RISK": float(config.ENTRY_MIN_REWARD_RISK),
            "CANDIDATE_MIN_CONVICTION": float(config.CANDIDATE_MIN_CONVICTION),
            "CANDIDATE_DOWNTREND_6M_PCT": float(config.CANDIDATE_DOWNTREND_6M_PCT),
            "CANDIDATE_MIN_BULLISH_TF": int(config.CANDIDATE_MIN_BULLISH_TF)}


# ── I/O (worker thread only) ─────────────────────────────────────────────────────────────────────

def fetch_index_closes() -> List[float]:
    """Daily index closes from market-data /history (raises on any failure)."""
    import httpx
    from market_feed.feed import MARKET_DATA_URL
    from stockky_ms import candles as _candles
    sym = (os.getenv("REGIME_ADAPTIVE_INDEX_SYMBOL") or "").strip() or str(getattr(config, "STRUCTURE_OBS_BENCH_SYMBOL", "") or "NIFTY")
    with httpx.Client(timeout=_env_float("REGIME_ADAPTIVE_HISTORY_TIMEOUT_S", 20.0)) as client:
        r = client.get(f"{MARKET_DATA_URL}/history/{sym}", params={"period": "2y", "interval": "1d"})
    if r.status_code != 200:
        raise RuntimeError(f"index history HTTP {r.status_code}")
    return [b[4] for b in _candles.candles_to_bars((r.json() or {}).get("candles") or [])]


def closed_real_pnls(db: Any, days: int) -> List[float]:
    """realized_pnl of REAL positions closed in the last `days` days."""
    import models
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=int(days))
    rows = (db.query(models.TradePosition.realized_pnl)
            .filter(models.TradePosition.mode == "REAL", models.TradePosition.status == "CLOSED",
                    models.TradePosition.closed_at >= cutoff).all())
    return [float(r[0]) for r in rows if r[0] is not None]


def refresh(session_factory: Optional[Callable[[], Any]] = None, fetch: Optional[Callable[[], List[float]]] = None,
            pnls: Optional[Callable[[Any, int], List[float]]] = None) -> Dict[str, Any]:
    """One pass: read the index and the closed trades, compute, store. Never raises; returns the snapshot or {error}."""
    with _lock:
        _state["last_attempt"] = time.time()
    try:
        if mode() != "live":
            return {"mode": "off"}
        closes = (fetch or fetch_index_closes)()
        pic = market_picture(closes)
        if pic is None:
            raise RuntimeError(f"not enough index history ({len(closes)} sessions, need {MIN_SESSIONS})")
        fb, n_tr, wr = 0.0, 0, None
        days = int(_env_float("REGIME_ADAPTIVE_RESULT_DAYS", 60))
        if session_factory is not None:
            db = session_factory()
            try:
                fb, n_tr, wr = result_feedback((pnls or closed_real_pnls)(db, days), int(_env_float("REGIME_ADAPTIVE_MIN_TRADES", 15)))
            except Exception as e:  # noqa: BLE001 - results are optional evidence; the market part still works
                logger.debug("regime adaptive: closed-trade read failed (market part only): %s", e)
            finally:
                try:
                    db.close()
                except Exception:  # noqa: BLE001
                    pass
        base = _base_values()
        vals = compute_values(base, pic["tone"], pic["stress"], fb, pic["returns"].get("6m"))
        snap = {"values": vals, "base": base, "picture": pic,
                "feedback": {"value": round(fb, 3), "trades": n_tr, "win_rate": None if wr is None else round(wr, 3), "days": days}}
        with _lock:
            prev = _state["snapshot"]
            _state.update(snapshot=snap, ts=time.time(), last_error=None)
            _history.append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "values": dict(vals),
                             "tone": pic["tone"], "stress": pic["stress"], "feedback": round(fb, 3)})
        if prev is None or prev["values"] != vals:
            logger.info("regime adaptive: tone %+.2f stress %.2f feedback %+.2f (%d trades) -> %s", pic["tone"], pic["stress"], fb, n_tr, vals)
        return snap
    except Exception as e:  # noqa: BLE001
        with _lock:
            _state["last_error"] = str(e)[:200]
        logger.warning("regime adaptive refresh failed (static values stay in force until it works): %s", e)
        return {"error": str(e)}


def status() -> Dict[str, Any]:
    with _lock:
        snap, ts, err, att = _state["snapshot"], _state["ts"], _state["last_error"], _state["last_attempt"]
        hist = list(_history)[-12:]
    fresh = _fresh_snapshot() is not None
    base = _base_values()
    eff = {k: effective(k, base.get(k)) for k in base}
    eff["ENTRY_REGIME_MIN_SCORE_shift"] = score_delta()
    return {"mode": mode(), "active": fresh, "updated_at": None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds"),
            "age_s": None if ts is None else round(time.time() - ts), "max_age_h": _max_age_s() / 3600.0, "last_error": err,
            "last_attempt_age_s": None if att is None else round(time.time() - att),
            "static": base, "effective": eff, "picture": (snap or {}).get("picture"), "feedback": (snap or {}).get("feedback"),
            "not_adapted": {"CANDIDATE_OVEREXTENDED_52W_TOP_PCT": "unused since the 52-week rule was removed on 2026-10-31"},
            "recent": hist}


async def run_loop(session_factory: Callable[[], Any]) -> None:
    """Background refresh: first pass 45 s after boot, then every REGIME_ADAPTIVE_REFRESH_S (default 900). Runs on a worker thread."""
    import asyncio
    await asyncio.sleep(45.0)
    while True:
        try:
            if mode() == "live":
                await asyncio.to_thread(refresh, session_factory)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            logger.warning("regime adaptive loop error (will retry): %s", e)
        await asyncio.sleep(max(60.0, _env_float("REGIME_ADAPTIVE_REFRESH_S", 900.0)))


def _reset_for_tests() -> None:
    with _lock:
        _state.update(snapshot=None, ts=None, last_error=None, last_attempt=None)
        _history.clear()
