"""engine.py - one symbol end to end with the backend injected (group 434). No FastAPI, no torch.

record = what the nightly batch stores per symbol: the close fan, the volume fan (optional), trailing stats and provenance.
features = the trading parameters derived from a stored record for a given stop / target (cheap: no model call).
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Dict, List, Optional, Sequence

import tsfm_features as tf

MIN_BARS = 60
CONTEXT = 512


class BadHistory(ValueError):
    pass


def prepare_bars(candles: Any, today: Optional[date] = None, context: int = CONTEXT, min_bars: int = MIN_BARS) -> List[Dict[str, Any]]:
    """Clean daily bars: lower-case keys, dedupe by date, sort, drop a bar dated `today` (incomplete), keep the last `context`."""
    rows: Dict[str, Dict[str, Any]] = {}
    for r in candles or []:
        if not isinstance(r, dict):
            continue
        lo = {str(k).lower(): v for k, v in r.items()}
        d = str(lo.get("date") or lo.get("timestamp") or "")[:10]
        try:
            bar = {"date": d, "open": float(lo["open"]), "high": float(lo["high"]), "low": float(lo["low"]), "close": float(lo["close"]),
                   "volume": float(lo.get("volume") or 0.0)}
        except (KeyError, TypeError, ValueError):
            continue
        if len(d) != 10 or bar["close"] <= 0 or bar["high"] < bar["low"]:
            continue
        rows[d] = bar
    bars = [rows[k] for k in sorted(rows)]
    if today is not None and bars and bars[-1]["date"] == today.isoformat():
        bars = bars[:-1]
    if len(bars) < min_bars:
        raise BadHistory(f"only {len(bars)} usable daily bars (need {min_bars})")
    return bars[-context:]


def build_record(backend: Any, symbol: str, candles: Any, *, horizon: int = 5, today: Optional[date] = None, trade_mode: str = "") -> Dict[str, Any]:
    horizon = max(1, min(int(horizon), 20))
    bars = prepare_bars(candles, today=today)
    closes = [b["close"] for b in bars]
    vols = [b["volume"] for b in bars]
    use_vol = sum(1 for v in vols[-20:] if v > 0) >= 15            # an index or a stale feed has no volume: skip that forecast
    series: List[Sequence[float]] = [closes] + ([vols] if use_vol else [])
    fans = backend.forecast(series, horizon)
    ts = tf.trailing_stats(bars)
    rec: Dict[str, Any] = {
        "symbol": symbol.upper(), "as_of": bars[-1]["date"], "last_close": bars[-1]["close"], "horizon": horizon,
        "fan_close": fans[0], "fan_volume": fans[1] if use_vol else None,
        "trailing_vol_pct": ts["trailing_vol_pct"], "trailing_volume": ts["trailing_volume"],
        "provenance": {"backend": getattr(backend, "name", "?"), "checkpoint": getattr(backend, "checkpoint", None),
                       "package_version": backend.package_version() if hasattr(backend, "package_version") else None,
                       "context_len": len(bars), "horizon": horizon, "as_of_bar": bars[-1]["date"], "trade_mode": trade_mode or None,
                       "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds")},
    }
    tf.derive(rec["last_close"], rec["fan_close"], trailing_vol_pct=rec["trailing_vol_pct"], horizon=horizon)      # validate the fan now, not at read time
    return rec


def features_from_record(rec: Dict[str, Any], stop_pct: Optional[float] = None, target_pct: Optional[float] = None) -> Dict[str, Any]:
    horizon = int(rec.get("horizon") or 5)
    pace = None
    if rec.get("fan_volume") is not None:
        try:
            pace = tf.volume_pace(rec["fan_volume"], rec.get("trailing_volume"), horizon)
        except tf.FeatureError:
            pace = None
    out = tf.derive(rec["last_close"], rec["fan_close"], trailing_vol_pct=rec.get("trailing_vol_pct"), stop_pct=stop_pct, target_pct=target_pct,
                    horizon=horizon, pace=pace)
    tvol = rec.get("trailing_volume")
    out["volume_forecast"] = round(pace * tvol, 2) if pace is not None and tvol else None        # mean forecast daily volume over the horizon (shares)
    out.update(symbol=rec["symbol"], as_of=rec["as_of"], provenance=rec.get("provenance"), stop_pct=stop_pct, target_pct=target_pct)
    return out
