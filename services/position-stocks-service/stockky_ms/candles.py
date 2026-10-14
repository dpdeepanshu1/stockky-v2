"""stockky_ms.candles - turn daily candles as the market-data service returns them ({date, open, high, low, close, volume}) into engine bars.

    candles_to_bars(candles) -> [(t, o, h, l, c, v)]     ascending, bad rows dropped, t = epoch seconds of the session date (00:00 UTC)
    day_key(ts)             -> int IST calendar-day number of an epoch second (the same key for 00:00 UTC and 00:00 IST stamps)
    ist_date(ts)            -> 'YYYY-MM-DD' IST date of an epoch second
Pure; never raises on bad rows (they are skipped).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .ms_types import IST_OFFSET_S, finite

Bar = Tuple[float, float, float, float, float, float]


def day_key(ts: float) -> int:
    return int((float(ts) + IST_OFFSET_S) // 86400)


def ist_date(ts: float) -> str:
    return (datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(days=day_key(ts))).strftime("%Y-%m-%d")


def parse_ts(v: Any) -> Optional[float]:
    """Epoch seconds from an epoch (s or ms) or an ISO date / datetime string; None when unparseable."""
    try:
        if v is None or isinstance(v, bool):
            return None
        if isinstance(v, (int, float)):
            x = float(v)
            return x / 1000.0 if x > 1e11 else x
        s = str(v).strip()
        if not s:
            return None
        if len(s) == 10:
            return datetime.strptime(s, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # a daily candle stamped at its session open (00:00 IST) belongs to that IST date
        return dt.timestamp()
    except Exception:  # noqa: BLE001
        return None


def candles_to_bars(candles: Optional[Iterable[Dict[str, Any]]]) -> List[Bar]:
    out: List[Bar] = []
    for c in candles or []:
        if not isinstance(c, dict):
            continue
        ts = parse_ts(c.get("date") if c.get("date") is not None else (c.get("t") if c.get("t") is not None else c.get("timestamp")))
        o, h, l, cl = c.get("open"), c.get("high"), c.get("low"), c.get("close")
        if ts is None or not all(finite(x) for x in (h, l, cl)):
            continue
        cl_f = float(cl)
        o_f = float(o) if finite(o) else cl_f
        v = c.get("volume")
        out.append((ts, o_f, float(h), float(l), cl_f, float(v) if finite(v) and float(v) >= 0 else 0.0))
    out.sort(key=lambda b: b[0])
    return out
