"""backtest/rolling.py - Dhan's EXPIRED OPTIONS DATA ("rolling option") API: request builder and response parser. Pure (no network): scripts/fno_rolling_fetch.py does the HTTP.

Why it exists (round 236): the plan said the named strategies cannot get a real-price backtest. Dhan does publish expired option candles for the last ~5 years, indexed by distance from the
money (ATM, ATM+1, ATM-1 ... up to +/-10 for index options near expiry), per expiry code, so an intraday strategy CAN be tested against real premiums (included in the Data API plan, about Rs 499 a month).
Source of the shape: https://dhanhq.co/docs/v2/expired-options-data/ (read 8 Nov 2026).

    POST https://api.dhan.co/v2/charts/rollingoption        headers: access-token, client-id, Content-Type: application/json
    body: exchangeSegment (NSE_FNO / BSE_FNO), interval (1, 5, 15, 25, 60), securityId (underlying index id), instrument OPTIDX, expiryFlag WEEK | MONTH, expiryCode (1 = nearest; 0 is refused),
          strike ATM | ATM+n | ATM-n, drvOptionType CALL | PUT, requiredData [open high low close iv volume strike oi spot], fromDate, toDate (see LIVE PROBE below: the page says non-inclusive and 30 days; the real API is inclusive and 1 day)
    reply: {"data": {"ce": {open: [], high: [], ..., timestamp: []}, "pe": null | {...}}}   (parallel arrays; timestamp = epoch seconds)

LIVE PROBE 9 Nov 2026 (round 255). The page above is wrong on three points; the real API behaves like this:
  * toDate - fromDate may be at most 1 day. 2 days or more answers HTTP 400 DH-905 (tried 2, 3, 4, 5, 6, 7 and 28 days, recent and old, 5 / 15 / 60 minute bars: all refused). The page says 30.
  * toDate is INCLUSIVE: 5 Nov to 6 Nov returned both days (a Saturday toDate adds nothing). The page says non-inclusive.
  * One day = 77 five-minute candles stamped 09:15 ... 15:35 (the 15:30 and 15:35 rows come after the close; the sim only looks up bars it has an index bar for).
  * Accepted: CALL and PUT, ATM and ATM+1, WEEK, expiryCode 1, securityId 13 as a number, interval 5, the full requiredData list, old (21 Sep) and recent days.
  So `windows()` sends a pair of days per call (from = a day, to = the next day) and steps two days, a pair of only Saturday / Sunday is skipped, and `in_range()` trims the rows to the days asked for.

NOT verified by a live call: SENSEX id 51 (check it in the scrip master) and the exact rate limit.
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

SEGMENT = {"NIFTY": "NSE_FNO", "SENSEX": "BSE_FNO"}
UNDERLYING_ID = {"NIFTY": 13, "SENSEX": 51}          # VERIFY SENSEX in api-scrip-master-detailed.csv (the docs' sample uses 13 for Nifty)
REQUIRED = ["open", "high", "low", "close", "iv", "volume", "strike", "oi", "spot"]
MAX_DAYS = 1                       # round 255: toDate - fromDate, measured live (the docs say 30; 2 or more is refused with DH-905). Raise only after scripts/fno_rolling_probe.py shows Dhan accepts more.
MAX_SPAN_CLI = 30                  # the largest value `--span-days` may be given
IST = timezone(timedelta(hours=5, minutes=30))
INTERVALS = (1, 5, 15, 25, 60)
STRIKE_RE = re.compile(r"^ATM(?:[+-](?:10|[1-9]))?$")      # round 16: ATM, ATM+1 ... ATM+10, ATM-1 ... ATM-10 (the docs' range for index options near expiry); anything else would only come back as an API error


def windows(start: date, end: date, max_days: int = MAX_DAYS) -> List[tuple]:
    """Round 255: [(fromDate, toDate)] calls that cover every WEEKDAY in [start, end) (`end` is not asked for). Dhan's toDate is INCLUSIVE and toDate - fromDate may be at most `max_days`,
    so one call holds at most max_days + 1 calendar days and the next call starts after it. A call that would hold only Saturday / Sunday is skipped. A lone last day is sent as
    (day before, day) - never from == to, never a date at or after `end`; the extra day is cut off again by in_range()."""
    out: List[tuple] = []
    last = end - timedelta(days=1)
    d = start
    while d <= last:
        e = min(d + timedelta(days=max_days), last)
        if any((d + timedelta(days=i)).weekday() < 5 for i in range((e - d).days + 1)):
            out.append((d, e) if e > d else (d - timedelta(days=1), d))
        d = e + timedelta(days=1)
    return out


def in_range(rows: List[Dict[str, Any]], start: date, end: date) -> List[Dict[str, Any]]:
    """Round 255: keep the candles whose IST day is in [start, end). A call can carry one day more than was asked for (a lone last day is sent with the day before it)."""
    return [r for r in rows if start <= datetime.fromtimestamp(r["t"], IST).date() < end]


def build_request(underlying: str, option_type: str, strike: str, start: date, end: date, interval: int = 5, expiry_flag: str = "WEEK", expiry_code: int = 1, max_days: int = MAX_DAYS) -> Dict[str, Any]:
    """`start` / `end` are the fromDate / toDate that go on the wire (toDate is inclusive, see the LIVE PROBE note); end - start must be 1 to `max_days` days."""
    u = underlying.upper()
    if u not in SEGMENT:
        raise ValueError("underlying must be NIFTY or SENSEX")
    if option_type not in ("CALL", "PUT"):
        raise ValueError("option_type must be CALL or PUT")
    if not isinstance(strike, str) or not STRIKE_RE.match(strike):
        raise ValueError("strike must be ATM, ATM+n or ATM-n with n from 1 to 10")
    if interval not in INTERVALS:
        raise ValueError(f"interval must be one of {INTERVALS}")
    if (end - start).days > max_days or end <= start:
        raise ValueError(f"toDate - fromDate must be 1 to {max_days} days")
    if expiry_flag not in ("WEEK", "MONTH") or expiry_code < 1:
        raise ValueError("expiry_flag WEEK|MONTH and expiry_code >= 1")
    return {"exchangeSegment": SEGMENT[u], "interval": interval, "securityId": UNDERLYING_ID[u], "instrument": "OPTIDX", "expiryFlag": expiry_flag, "expiryCode": expiry_code,
            "strike": strike, "drvOptionType": option_type, "requiredData": list(REQUIRED), "fromDate": start.isoformat(), "toDate": end.isoformat()}


def parse_response(body: Any, option_type: str) -> List[Dict[str, Any]]:
    """Reply -> candle rows [{t, o, h, l, c, v, oi, iv, spot, strike}] in time order. Rows with no (or an unreadable) timestamp, or no / zero close, are dropped; a missing array just leaves that field None."""
    data = (body or {}).get("data") if isinstance(body, dict) else None
    side = (data or {}).get("ce" if option_type == "CALL" else "pe") if isinstance(data, dict) else None
    if not isinstance(side, dict):
        return []
    ts = side.get("timestamp")
    if not isinstance(ts, list):
        return []

    def col(name: str) -> List[Any]:
        v = side.get(name)
        return v if isinstance(v, list) else []

    cols = {k: col(k) for k in ("open", "high", "low", "close", "volume", "oi", "iv", "spot", "strike")}
    rows: List[Dict[str, Any]] = []
    for i, t in enumerate(ts):
        def at(k: str) -> Optional[float]:
            c = cols[k]
            try:
                return float(c[i]) if i < len(c) and c[i] is not None else None
            except (TypeError, ValueError):
                return None
        close = at("close")
        try:
            tf = float(t)
        except (TypeError, ValueError):
            continue                                            # round 16: one unreadable timestamp used to raise and lose the whole window
        if tf != tf or close is None or close != close or close <= 0:
            continue                                            # a zero / NaN close is "no trade that bar", not a price of 0 (a fill at 0 would break sizing)
        rows.append({"t": tf, "o": at("open"), "h": at("high"), "l": at("low"), "c": close, "v": at("volume"), "oi": at("oi"), "iv": at("iv"), "spot": at("spot"), "strike": at("strike")})
    rows.sort(key=lambda r: r["t"])
    return rows


def merge_rows(chunks: List[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    """Round 16: rows of several windows -> one time-ordered list with each timestamp once (the last window's row wins). Since round 255 the windows DO overlap (toDate is inclusive and a lone last day is sent with the day before it),
    so a candle seen twice must be counted once or every backtest metric on it would be skewed."""
    by_t: Dict[float, Dict[str, Any]] = {}
    for rows in chunks:
        for r in rows:
            by_t[r["t"]] = r
    return [by_t[t] for t in sorted(by_t)]
