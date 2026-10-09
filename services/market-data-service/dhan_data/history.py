"""dhan_data/history.py - candles from Dhan's historical API in this service's candle shape (group 270).

  POST /charts/historical  daily OHLCV back to listing         (interval "1d"; weekly is built from daily)
  POST /charts/intraday    1/5/15/25/60-minute candles          (interval "1h" -> 60)

Output candle: {"date": "YYYY-MM-DD HH:MM", open, high, low, close, volume} ascending, IST. Daily candles carry
"00:00". That is exactly what the AngelOne and yfinance paths return, so no caller changes.

Review risk R2: yfinance history is split/dividend adjusted (auto_adjust=True); Dhan daily candles may not be.
Compare a stock with a recent split or bonus before feeding Dhan candles to ML training.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

from . import client, config, scrip_master
from .errors import DhanNoDataError

logger = logging.getLogger("dhan-data.history")
IST = ZoneInfo("Asia/Kolkata")

_INTRADAY_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "25m": 25, "1h": 60, "60m": 60}


def supports(interval: str) -> bool:
    return (interval or "").lower() in ("1d", "1wk") or (interval or "").lower() in _INTRADAY_MINUTES


def _to_ist(ts) -> Optional[datetime]:
    try:
        v = float(ts)
    except (TypeError, ValueError):
        return None
    if v > 1e11:        # milliseconds
        v /= 1000.0
    if v < 1e8:         # not a plausible epoch: Dhan v1 used a 1980-based epoch, which this code does not accept
        return None
    return datetime.fromtimestamp(v, tz=timezone.utc).astimezone(IST)


def parse_candles(body: dict, *, intraday: bool) -> List[dict]:
    """Dhan's parallel arrays -> candle dicts. Rows with a missing/non-positive close, or a bad timestamp, are dropped;
    duplicates keep the last value; result is ascending. Pure function (tested offline)."""
    if not isinstance(body, dict):
        return []
    src = body.get("data") if isinstance(body.get("data"), dict) and "close" not in body else body
    if not isinstance(src, dict):
        return []
    ts, o, h, l, c, v = (src.get(k) or [] for k in ("timestamp", "open", "high", "low", "close", "volume"))
    n = min(len(ts), len(o), len(h), len(l), len(c))
    out: dict = {}
    for i in range(n):
        dt = _to_ist(ts[i])
        try:
            close = float(c[i])
        except (TypeError, ValueError):
            continue
        if dt is None or close <= 0 or close != close:
            continue
        try:
            vol = int(float(v[i])) if i < len(v) and v[i] is not None else 0
        except (TypeError, ValueError):
            vol = 0
        label = f"{dt.date().isoformat()} 00:00" if not intraday else dt.strftime("%Y-%m-%d %H:%M")
        out[label] = {"date": label, "open": _num(o[i], close), "high": _num(h[i], close),
                      "low": _num(l[i], close), "close": close, "volume": vol}
    return [out[k] for k in sorted(out)]


def _num(x, fallback: float) -> float:
    try:
        f = float(x)
        return f if f == f and f > 0 else fallback
    except (TypeError, ValueError):
        return fallback


def to_weekly(candles: List[dict]) -> List[dict]:
    """Daily -> weekly bars (Monday-labelled, like yfinance's 1wk). First open, max high, min low, last close, summed volume."""
    weeks: dict = {}
    for c in candles:
        try:
            d = date.fromisoformat(str(c["date"])[:10])
        except (ValueError, KeyError):
            continue
        monday = d - timedelta(days=d.weekday())
        w = weeks.get(monday)
        if w is None:
            weeks[monday] = {"date": f"{monday.isoformat()} 00:00", "open": c["open"], "high": c["high"],
                             "low": c["low"], "close": c["close"], "volume": int(c.get("volume") or 0)}
        else:
            w["high"] = max(w["high"], c["high"])
            w["low"] = min(w["low"], c["low"])
            w["close"] = c["close"]
            w["volume"] += int(c.get("volume") or 0)
    return [weeks[k] for k in sorted(weeks)]


def _instrument(segment: str) -> str:
    return "INDEX" if segment == "IDX_I" else "EQUITY"


def fetch_daily(symbol: str, from_date: date, to_date: date) -> List[dict]:
    """Daily candles for [from_date, to_date) (Dhan's toDate is not inclusive). Raises DhanError on failure,
    DhanNoDataError when Dhan has no row for the symbol."""
    ident = scrip_master.security_id(symbol)
    if not ident:
        raise DhanNoDataError(f"{symbol} is not in the Dhan scrip master")
    seg, sid = ident
    payload = {"securityId": str(sid), "exchangeSegment": seg, "instrument": _instrument(seg),
               "expiryCode": 0, "oi": False, "fromDate": from_date.isoformat(), "toDate": to_date.isoformat()}
    body = client.post("charts/historical", payload, limiter=client.hist_limiter)
    return parse_candles(body, intraday=False)


def fetch_intraday(symbol: str, interval: str, from_dt: datetime, to_dt: datetime) -> List[dict]:
    """Intraday candles between two IST datetimes, requested in DHAN_INTRADAY_CHUNK_DAYS windows."""
    minutes = _INTRADAY_MINUTES.get((interval or "").lower())
    if minutes is None:
        raise DhanNoDataError(f"unsupported intraday interval {interval}")
    ident = scrip_master.security_id(symbol)
    if not ident:
        raise DhanNoDataError(f"{symbol} is not in the Dhan scrip master")
    seg, sid = ident
    step = timedelta(days=config.intraday_chunk_days())
    out: dict = {}
    cur = from_dt
    while cur < to_dt:
        nxt = min(cur + step, to_dt)
        payload = {"securityId": str(sid), "exchangeSegment": seg, "instrument": _instrument(seg),
                   "interval": str(minutes), "oi": False,
                   "fromDate": cur.strftime("%Y-%m-%d %H:%M:%S"), "toDate": nxt.strftime("%Y-%m-%d %H:%M:%S")}
        body = client.post("charts/intraday", payload, limiter=client.hist_limiter)
        for cd in parse_candles(body, intraday=True):
            out[cd["date"]] = cd
        cur = nxt
    return [out[k] for k in sorted(out)]


def fetch_candles(symbol: str, interval: str, from_date: date, to_date: date) -> List[dict]:
    """One entry point for 1d / 1wk / 1h|60m|... Dates are IST calendar dates; to_date is exclusive."""
    iv = (interval or "").lower()
    if iv == "1d":
        return fetch_daily(symbol, from_date, to_date)
    if iv == "1wk":
        return to_weekly(fetch_daily(symbol, from_date, to_date))
    if iv in _INTRADAY_MINUTES:
        start = datetime.combine(from_date, datetime.min.time().replace(hour=9, minute=15))
        end = datetime.combine(to_date, datetime.min.time().replace(hour=15, minute=30))
        return fetch_intraday(symbol, iv, start, end)
    raise DhanNoDataError(f"unsupported interval {interval}")


def today_ist() -> date:
    return datetime.now(IST).date()
