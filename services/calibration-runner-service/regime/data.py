"""Candle cleaning for the market-state layer. Stdlib only. Accepts what market-data /history returns:
{"candles": [{"date"|"time"|"timestamp": ..., "open", "high", "low", "close", "volume"}, ...]} or a bare list of such dicts.
Bad rows (missing / non-positive prices, high < low, duplicate dates) are dropped, never repaired; rows are sorted by date."""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from datetime import date, datetime
from typing import Any, Dict, List, Optional

NIFTY = "^NSEI"
VIX = "^INDIAVIX"
_DATE_KEYS = ("date", "time", "timestamp", "datetime", "t")


def _f(v: Any) -> Optional[float]:
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float("inf"), float("-inf")) else None


def _day(v: Any) -> Optional[str]:
    """YYYY-MM-DD of a date-like value (ISO text, epoch seconds or milliseconds), else None."""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        ts = float(v) / 1000.0 if float(v) > 1e11 else float(v)
        try:
            return datetime.utcfromtimestamp(ts).date().isoformat()
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    if isinstance(v, date):
        return v.isoformat()
    s = str(v or "").strip()
    if len(s) >= 10 and s[4] == "-" and s[7] == "-":
        try:
            return date.fromisoformat(s[:10]).isoformat()
        except ValueError:
            return None
    return None


def candles_of(doc: Any) -> list:
    c = doc.get("candles") if isinstance(doc, dict) else doc
    return c if isinstance(c, list) else []


def clean_daily(doc: Any) -> List[Dict[str, Any]]:
    """Sorted, de-duplicated daily bars [{"d","o","h","l","c","v"}]. The LAST row for a date wins (a re-published partial day is replaced)."""
    by_day: Dict[str, Dict[str, Any]] = {}
    for r in candles_of(doc):
        if not isinstance(r, dict):
            continue
        d = None
        for k in _DATE_KEYS:
            if k in r:
                d = _day(r[k])
                if d:
                    break
        o, h, l, c = (_f(r.get(k)) for k in ("open", "high", "low", "close"))
        if not d or None in (o, h, l, c) or min(o, h, l, c) <= 0 or h < l or h < max(o, c) - 1e-9 or l > min(o, c) + 1e-9:
            continue
        by_day[d] = {"d": d, "o": o, "h": h, "l": l, "c": c, "v": _f(r.get("volume")) or 0.0}
    return [by_day[k] for k in sorted(by_day)]


def upto(bars: List[Dict[str, Any]], day: str, include_day: bool = False) -> List[Dict[str, Any]]:
    """Bars strictly BEFORE `day` (or up to and including it). Every feature is built from this, so nothing can see the future."""
    return [b for b in bars if (b["d"] <= day if include_day else b["d"] < day)]


def clean_intraday(doc: Any) -> List[Dict[str, Any]]:
    """Intraday bars [{"t","o","h","l","c","v"}] in time order; `t` is the raw timestamp text. Same drop rules as clean_daily."""
    out: List[Dict[str, Any]] = []
    for r in candles_of(doc):
        if not isinstance(r, dict):
            continue
        t = next((r[k] for k in _DATE_KEYS if k in r and r[k] not in (None, "")), None)
        o, h, l, c = (_f(r.get(k)) for k in ("open", "high", "low", "close"))
        if t is None or None in (o, h, l, c) or min(o, h, l, c) <= 0 or h < l:
            continue
        out.append({"t": str(t), "o": o, "h": h, "l": l, "c": c, "v": _f(r.get("volume")) or 0.0})
    out.sort(key=lambda b: b["t"])
    return out


def fetch_history(base: str, symbol: str, period: str = "5y", interval: str = "1d", timeout: float = 60.0, retries: int = 3) -> Any:
    """GET <market-data>/history/<symbol>. Raises the last error after `retries` tries. Used by scripts, never at import time."""
    import time
    url = f"{base.rstrip('/')}/history/{urllib.parse.quote(symbol, safe='^')}?period={period}&interval={interval}"
    last: Optional[Exception] = None
    for k in range(max(1, retries)):
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json", "User-Agent": "stockky-regime/1"})
            with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - operator-supplied URL
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2.0 * (k + 1))
    raise last if last else RuntimeError("fetch failed")
