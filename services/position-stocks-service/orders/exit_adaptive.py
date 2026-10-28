"""orders/exit_adaptive.py - E252 (2026-10-28): exit numbers that scale with the trade's own target and the stock's own noise.

SYNGENE 28 Oct: bought 389.45, trailing stop exited at 391.15 (+0.44 %) and the stock then ran to 412.7 - the trail started at a fixed +1.0 % and sat a fixed
0.78 % under the peak, smaller than that stock's normal 5-minute swing. HEGAM: closed by the 20-minute no-follow-through rule; it was fading, so that was right,
but the rule cannot tell a fading stock from a coiling one, and fires even when nobody is waiting for the slot.

  trail_activation_pct / trail_distance_pct  the trailing stop (orders/trailing.py)
  nft_min_gain_pct / flat_band_pct           the no-follow-through gain and the flat band (orders/eod_squareoff.py::run_stagnation_exit)
  is_fading / slot_pressure / stagnation_decision   hold-or-close for a flat trade

Everything is pure or read-only and fails to the OLD behaviour (fixed numbers) on missing data. TRAIL_ADAPTIVE_ENABLED / EXIT_ADAPTIVE_ENABLED = false are the rollbacks.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional, Sequence, Tuple

import config

logger = logging.getLogger("position-stocks-exit-adaptive")


def _cfg(name: str, default):
    try:
        v = getattr(config, name)
        return default if v is None else v
    except Exception:  # noqa: BLE001
        return default


def enabled() -> bool:
    return bool(_cfg("EXIT_ADAPTIVE_ENABLED", True))


def trail_enabled() -> bool:
    return bool(_cfg("TRAIL_ADAPTIVE_ENABLED", True))


def target_pct_of(entry: Any, target: Any) -> Optional[float]:
    """The trade's own target as a % of entry, or None when either price is missing."""
    try:
        e, t = float(entry), float(target)
        if e <= 0 or t <= 0:
            return None
        return abs(t - e) / e * 100.0
    except (TypeError, ValueError):
        return None


def _clamp(v: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, v))


# ── trailing stop ───────────────────────────────────────────────────────────────────────────────────────────────────────────────────────

def trail_activation_pct(target_pct: Optional[float]) -> float:
    """Peak gain needed before the trail starts: the later of the fixed TRAIL_ACTIVATE_PCT and a share of this trade's target, capped."""
    base = float(_cfg("TRAIL_ACTIVATE_PCT", 1.0))
    if not trail_enabled() or not target_pct or target_pct <= 0:
        return base
    cap = float(_cfg("TRAIL_ACTIVATE_MAX_PCT", 2.0))
    return min(max(base, float(_cfg("TRAIL_ACTIVATE_TARGET_FRAC", 0.5)) * target_pct), max(cap, base))


def trail_distance_pct(stop_pct: Optional[float], bar_range_pct: Optional[float]) -> float:
    """Distance below the peak: the old fixed distance, widened to the stock's average 5-minute bar range, never wider than the stop."""
    sp = float(stop_pct or 0.0)
    old = max(float(_cfg("TRAIL_MIN_DISTANCE_PCT", 0.4)), sp * float(_cfg("TRAIL_DISTANCE_STOP_FRACTION", 0.6)))
    if not trail_enabled() or not bar_range_pct or bar_range_pct <= 0:
        return old
    dist = max(old, float(_cfg("TRAIL_BAR_RANGE_MULT", 1.0)) * float(bar_range_pct))
    if sp > 0:
        dist = min(dist, max(sp * float(_cfg("TRAIL_MAX_DIST_STOP_FRAC", 1.0)), old))
    return dist


def _avg_range_from_points(points: Sequence[Tuple[float, float, float, float]], bucket_s: int = 300, min_buckets: int = 3, max_buckets: int = 6) -> Optional[float]:
    """points = (ts, high, low, close). Average (high-low)/close % of the last complete `bucket_s` buckets; None with fewer than `min_buckets`."""
    if not points:
        return None
    buckets: dict = {}
    for ts, hi, lo, cl in points:
        k = int(ts // bucket_s)
        b = buckets.get(k)
        if b is None:
            buckets[k] = [hi, lo, cl]
        else:
            b[0] = max(b[0], hi)
            b[1] = min(b[1], lo)
            b[2] = cl
    keys = sorted(buckets)
    if len(keys) > 1:
        keys = keys[:-1]            # the newest bucket is still forming
    keys = keys[-max_buckets:]
    ranges = [(buckets[k][0] - buckets[k][1]) / buckets[k][2] * 100.0 for k in keys if buckets[k][2] > 0]
    if len(ranges) < min_buckets:
        return None
    return sum(ranges) / len(ranges)


def avg_5m_range_pct(symbol: str) -> Optional[float]:
    """The stock's average 5-minute bar range % today (1-minute bar store, tick buffer as fallback), or None. Never raises."""
    try:
        sym = str(symbol or "").strip().upper()
        try:
            from feed import bar_store
            bars = bar_store.get_bars(sym)
            if bars:
                v = _avg_range_from_points([(float(b[0]), float(b[2]), float(b[3]), float(b[4])) for b in bars[-40:]])
                if v:
                    return v
        except Exception as e:  # noqa: BLE001
            logger.debug("exit-adaptive: bar store range for %s failed: %s", sym, e)
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        if buf:
            return _avg_range_from_points([(float(t), float(p), float(p), float(p)) for t, p in buf])
    except Exception as e:  # noqa: BLE001
        logger.debug("exit-adaptive: range for %s failed: %s", symbol, e)
    return None


# ── stagnation / no-follow-through ─────────────────────────────────────────────────────────────────────────────────────────────────────────

def nft_min_gain_pct(target_pct: Optional[float]) -> float:
    """Best gain a trade must have shown by the no-follow-through age. Fixed NO_FOLLOWTHROUGH_MIN_GAIN_PCT when adaptive is off / the target is unknown."""
    fixed = float(_cfg("NO_FOLLOWTHROUGH_MIN_GAIN_PCT", 0.5))
    if not enabled() or not target_pct or target_pct <= 0:
        return fixed
    return _clamp(float(_cfg("NFT_TARGET_FRAC", 0.2)) * target_pct, float(_cfg("NFT_GAIN_MIN_PCT", 0.4)), float(_cfg("NFT_GAIN_MAX_PCT", 0.8)))


def flat_band_pct(target_pct: Optional[float]) -> float:
    """The +- band around entry still counted as flat."""
    fixed = float(_cfg("STAGNATION_EXIT_BAND_PCT", 0.35))
    if not enabled() or not target_pct or target_pct <= 0:
        return fixed
    return _clamp(float(_cfg("STAGNATION_BAND_TARGET_FRAC", 0.15)) * target_pct, float(_cfg("STAGNATION_BAND_MIN_PCT", 0.25)), float(_cfg("STAGNATION_BAND_MAX_PCT", 0.6)))


def is_fading(symbol: str, ltp: float, short: bool = False, now_ts: Optional[float] = None) -> Optional[bool]:
    """True when the trade is on the wrong side of the day VWAP or has made lower highs (a short: higher lows) over the last EXIT_FADE_WINDOW_MIN minutes.
    False when neither shows; None when there is no data to say."""
    try:
        verdict_known = False
        vw = None
        try:
            from feed import ws_client
            vw = ws_client.get_exchange_vwap(symbol)
        except Exception:  # noqa: BLE001
            vw = None
        if vw:
            verdict_known = True
            if (ltp > vw) if short else (ltp < vw):
                return True
        from feed import ws_client
        buf = ws_client.get_tick_buffer(symbol)
        if buf:
            win = float(_cfg("EXIT_FADE_WINDOW_MIN", 10.0)) * 60.0
            end = float(now_ts) if now_ts else float(buf[-1][0])
            half = win / 2.0
            recent = [float(p) for t, p in buf if end - half <= float(t) <= end]
            prior = [float(p) for t, p in buf if end - win <= float(t) < end - half]
            if len(recent) >= 5 and len(prior) >= 5:
                verdict_known = True
                if short:
                    return min(recent) > min(prior)      # higher lows
                return max(recent) < max(prior)          # lower highs
        return False if verdict_known else None
    except Exception as e:  # noqa: BLE001
        logger.debug("exit-adaptive: fade check for %s failed: %s", symbol, e)
        return None


def slot_pressure(db: Any, now: Optional[datetime] = None) -> Optional[str]:
    """The skip reason of a candidate refused for capital or slots within EXIT_SLOT_PRESSURE_WINDOW_MIN minutes, else None. Read-only; None on any error."""
    try:
        from models import ScalpCandidateLog
        now = now or datetime.now(timezone.utc)
        since = now - timedelta(minutes=float(_cfg("EXIT_SLOT_PRESSURE_WINDOW_MIN", 10.0)))
        since_naive = since.replace(tzinfo=None) if since.tzinfo else since
        rows = (db.query(ScalpCandidateLog).filter(ScalpCandidateLog.decision == "SKIPPED").filter(ScalpCandidateLog.created_at >= since_naive)
                .order_by(ScalpCandidateLog.created_at.desc()).limit(200).all())
        for r in rows:
            reason = str(r.reason or "")
            if reason.startswith(("INSUFFICIENT_CAPITAL", "MAX_POSITIONS")):
                return f"{r.symbol}: {reason.split(':')[0]}"
    except Exception as e:  # noqa: BLE001
        logger.debug("exit-adaptive: slot pressure check failed: %s", e)
    return None


def stagnation_decision(*, age_min: float, fading: Optional[bool], pressure: Optional[str]) -> Tuple[bool, str]:
    """(close_now, why) for a trade that one of the two rules has already flagged. Adaptive off -> always close (old behaviour)."""
    if not enabled():
        return True, "adaptive exits off"
    if age_min >= float(_cfg("EXIT_HARD_MAX_MINUTES", 90.0)):
        return True, f"hard maximum {age_min:.0f}m"
    if pressure:
        return True, f"slot wanted ({pressure})"
    if fading:
        return True, "fading (wrong side of VWAP / lower highs)"
    return False, "not fading and no candidate waiting"
