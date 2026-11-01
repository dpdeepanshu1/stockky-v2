"""entry_engine/event_risk.py - results / earnings day check before a BUY or an overnight carry (group 329, 2026-10-12).

WHY: a stock reporting results TODAY can move 5-10 % either way within minutes of the announcement, in either direction and with
no relation to the technical or news score that selected it. A stop-loss does not protect against that (the price can gap through
it). The event service already knows the date (yfinance earnings calendar) but only the prediction model read it; neither trading
service used it as a risk check.

RULES
  * entry:  EARNINGS_DAY_BLOCK (default on) - no BUY when the stock's results are dated today, except (E285 round 101, EARNINGS_SPLIT_ENABLED) results published before 09:00
            (a known event) or more than EARNINGS_REACTION_WAIT_MIN minutes ago; unknown publication time stays blocked.
  * carry:  EARNINGS_CARRY_BLOCK (default on) - a position that would be held overnight is NOT carried when the results fall today
            or on the next trading day (an overnight gap on the announcement is the one risk the carry rules do not price).

SOURCE: analysis-intelligence-service GET {EVENT_URL}/events/{symbol}/categorized. The earnings date is in `upcoming` or, for a
date that is already "today" (the endpoint compares a bare date against the current instant), in `recent` - both lists are
read, items with type == "earnings_date".

FAIL OPEN, ALWAYS: an unreachable event service, a timeout, a bad body, no earnings date, an unparseable date or any exception
means "no opinion" and never blocks. Results are cached per symbol for the IST day (a failure for FAILURE_RETRY_S only), so
the network is hit at most once per symbol per day. lookup() blocks up to EARNINGS_FETCH_TIMEOUT_S - call it through
asyncio.to_thread from async code.
"""
from __future__ import annotations

import logging
import threading
import time
from datetime import date, datetime, timedelta, timezone
from typing import Optional

import config
from tz_utils import IST, ist_now, ist_today_str, is_nse_holiday

logger = logging.getLogger("real-trade-event-risk")

FAILURE_RETRY_S = 120.0
RESULT_OUT_RETRY_S = 60.0     # E285 round 101: a "not published yet" answer is asked again after this long (results can land any minute of the session)

_lock = threading.Lock()
_cache: dict = {}        # symbol -> (ist_date_str, earnings_date | None)
_news_cache: dict = {}   # symbol -> (ist_date_str, negative-news reason | None)   (group330, filled from the same body)
_failed_at: dict = {}    # symbol -> monotonic time of the last failed fetch
_out_cache: dict = {}    # symbol -> (ist_date_str, published ISO | None, monotonic time of the fetch)   (E285 round 101, results-day split)


def reset() -> None:
    with _lock:
        _cache.clear()
        _news_cache.clear()
        _failed_at.clear()
        _out_cache.clear()


def parse_earnings_date(body) -> Optional[date]:
    """The earnings date in a /events/{symbol}/categorized body, or None. Never raises."""
    try:
        if not isinstance(body, dict):
            return None
        found = []
        for key in ("upcoming", "recent"):
            for item in (body.get(key) or []):
                if isinstance(item, dict) and item.get("type") == "earnings_date":
                    raw = str(item.get("date") or "")[:10]
                    if len(raw) == 10:
                        found.append(date.fromisoformat(raw))
        return min(found) if found else None
    except Exception:  # noqa: BLE001
        return None


# group330: event types that are a hard negative on their own (a regulatory action / delisting risk, a FRESH earnings miss).
_HARD_NEGATIVE_TYPES = {"regulatory_action": -8.0, "delisting_risk": -8.0, "earnings_miss": -15.0}


def negative_news_reason(body) -> Optional[str]:
    """A "NEWS_NEGATIVE:..." reason from a /events/{symbol}/categorized body, or None. Uses event_score (0-100, 50 neutral) against
    config.NEWS_NEGATIVE_BLOCK_SCORE and the hard-negative event types in event_score_breakdown. Missing score / breakdown = None. Never raises."""
    try:
        if not config.NEWS_NEGATIVE_BLOCK_ENABLED or not isinstance(body, dict):
            return None
        for b in (body.get("event_score_breakdown") or []):
            if not isinstance(b, dict):
                continue
            limit = _HARD_NEGATIVE_TYPES.get(b.get("type"))
            if limit is not None and float(b.get("decayed_impact") or 0.0) <= limit:
                return f"NEWS_NEGATIVE: {b.get('type')} on record (decayed impact {float(b['decayed_impact']):+.1f}) - not a long"
        floor = float(config.NEWS_NEGATIVE_BLOCK_SCORE or 0.0)
        raw = body.get("event_score")
        if floor > 0 and raw is not None:
            score = float(raw)
            if score == score and score <= floor:
                return f"NEWS_NEGATIVE: event score {score:.0f} <= {floor:.0f} (negative news / events) - not a long"
        return None
    except Exception:  # noqa: BLE001
        return None


def _fetch(symbol: str):
    import httpx
    from urllib.parse import quote
    sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    with httpx.Client(timeout=float(config.EARNINGS_FETCH_TIMEOUT_S)) as client:
        r = client.get(f"{config.EVENT_URL}/events/{quote(sym, safe='&')}/categorized")
    if r.status_code != 200:
        raise RuntimeError(f"events HTTP {r.status_code}")
    return r.json()


def lookup(symbol: str) -> Optional[date]:
    """The symbol's earnings date (cached for the IST day), or None when unknown / none dated. Blocks on a cache miss. Never raises."""
    try:
        today = ist_today_str()
        with _lock:
            hit = _cache.get(symbol)
            if hit and hit[0] == today:
                return hit[1]
            if time.monotonic() - _failed_at.get(symbol, -1e9) < FAILURE_RETRY_S:
                return None
        try:
            _body = _fetch(symbol)
            d = parse_earnings_date(_body)
            _news = negative_news_reason(_body)
        except Exception as e:  # noqa: BLE001
            with _lock:
                _failed_at[symbol] = time.monotonic()
            logger.info("event_risk: earnings lookup for %s failed (%s: %s) - no opinion", symbol, type(e).__name__, e)
            return None
        with _lock:
            _cache[symbol] = (today, d)
            _news_cache[symbol] = (today, _news)
        return d
    except Exception:  # noqa: BLE001
        return None


def next_trading_day(today: date) -> date:
    """The next NSE session after `today`: skips weekends and the NSE holidays in tz_utils."""
    d = today + timedelta(days=1)
    for _ in range(14):
        if d.weekday() < 5 and not is_nse_holiday(datetime(d.year, d.month, d.day, 12, 0, tzinfo=IST)):
            return d
        d += timedelta(days=1)
    return today + timedelta(days=1)


def _parse_utc(raw) -> Optional[datetime]:
    try:
        if not raw:
            return None
        d = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        return d if d.tzinfo else d.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def result_out_from(body) -> Optional[str]:
    """Earliest published time (ISO) of a type "results" event in a GET /events/{symbol}/active body, or None. Never raises."""
    try:
        times = []
        for e in ((body or {}).get("events") or []) if isinstance(body, dict) else []:
            if isinstance(e, dict) and e.get("type") == "results" and _parse_utc(e.get("published")) is not None:
                times.append(_parse_utc(e.get("published")))
        return min(times).isoformat() if times else None
    except Exception:  # noqa: BLE001
        return None


def _fetch_active(symbol: str):
    import httpx
    from urllib.parse import quote
    sym = (symbol or "").upper().replace(".NS", "").replace(".BO", "").strip()
    with httpx.Client(timeout=float(config.EARNINGS_FETCH_TIMEOUT_S)) as client:
        r = client.get(f"{config.EVENT_URL}/events/{quote(sym, safe='&')}/active")
    if r.status_code != 200:
        raise RuntimeError(f"events/active HTTP {r.status_code}")
    return r.json()


def result_out_at(symbol: str) -> Optional[str]:
    """When today's results were first published (typed event feed, type "results"), or None when not found / the feed failed. A found time is cached for the IST day; None is
    asked again after RESULT_OUT_RETRY_S. Blocks on a cache miss. Never raises."""
    try:
        today = ist_today_str()
        with _lock:
            hit = _out_cache.get(symbol)
            if hit and hit[0] == today and (hit[1] is not None or time.monotonic() - hit[2] < RESULT_OUT_RETRY_S):
                return hit[1]
        try:
            out = result_out_from(_fetch_active(symbol))
        except Exception as e:  # noqa: BLE001
            logger.info("event_risk: results-time lookup for %s failed (%s: %s) - results day stays blocked", symbol, type(e).__name__, e)
            out = None
        with _lock:
            _out_cache[symbol] = (today, out, time.monotonic())
        return out
    except Exception:  # noqa: BLE001
        return None


def split_reason(out_at, now: Optional[datetime] = None) -> Optional[str]:
    """E285 round 101 results-day split for a stock whose results are dated TODAY (same rule as position-stocks screening/quality_gate.earnings_split_reason).
    None = do not block; otherwise the reason text. Split off or no publication time known -> blocked; published before EARNINGS_KNOWN_BEFORE_IST -> a known event, not
    blocked; published later -> blocked until EARNINGS_REACTION_WAIT_MIN minutes after publication. Never raises (an error blocks)."""
    base = "gap risk the stop cannot cover"
    try:
        if not getattr(config, "EARNINGS_SPLIT_ENABLED", True):
            return base
        out = _parse_utc(out_at)
        if out is None:
            return base + " (publication time unknown)"
        from tz_utils import parse_hhmm
        out_ist = out.astimezone(IST)
        n = ist_now(now)
        if out_ist.date() != n.date():
            return base + " (publication time not from today)"
        kt = parse_hhmm(getattr(config, "EARNINGS_KNOWN_BEFORE_IST", "09:00"), 9, 0)
        if (out_ist.hour, out_ist.minute) < (kt.hour, kt.minute):
            return None
        left = out_ist.timestamp() + float(getattr(config, "EARNINGS_REACTION_WAIT_MIN", 5.0) or 0.0) * 60.0 - n.timestamp()
        if left > 0:
            return f"results published {out_ist.strftime('%H:%M')} IST, waiting {left / 60.0:.1f} more min for the first reaction"
        return None
    except Exception:  # noqa: BLE001
        return base


def reason_for_date(d: Optional[date], *, carry: bool = False, today: Optional[date] = None) -> Optional[str]:
    """Pure decision from an earnings date: a reason string or None. carry=True also covers the next trading day."""
    try:
        if d is None:
            return None
        today = today or ist_now().date()
        if d == today and (config.EARNINGS_DAY_BLOCK or (carry and config.EARNINGS_CARRY_BLOCK)):
            return f"EARNINGS_TODAY: results dated {d.isoformat()} (today) - gap risk the stop cannot cover"
        if carry and config.EARNINGS_CARRY_BLOCK and today < d <= next_trading_day(today):
            return (f"EARNINGS_NEXT_SESSION: results dated {d.isoformat()} (next trading day) - "
                    "not carried overnight into the announcement")
        return None
    except Exception:  # noqa: BLE001
        return None


def reject_reason(symbol: str, *, carry: bool = False) -> Optional[str]:
    """Blocking: look the date up and decide. None = no objection / unknown. Never raises."""
    try:
        if not (config.EARNINGS_DAY_BLOCK or (carry and config.EARNINGS_CARRY_BLOCK)):
            return None
        d = lookup(symbol)
        base = reason_for_date(d, carry=carry)
        if base and not carry and d == ist_now().date() and getattr(config, "EARNINGS_SPLIT_ENABLED", True):
            sr = split_reason(result_out_at(symbol))                  # E285 round 101: known / waited-out results no longer block an entry
            return None if sr is None else f"EARNINGS_TODAY: results dated {d.isoformat()} (today) - {sr}"
        return base
    except Exception:  # noqa: BLE001
        return None


def news_reason(symbol: str) -> Optional[str]:
    """Blocking (group330): the stock's negative-news reason (cached for the IST day with the earnings lookup) or None when none / unknown.
    Never raises."""
    try:
        if not config.NEWS_NEGATIVE_BLOCK_ENABLED:
            return None
        lookup(symbol)                      # fills _news_cache from the same event-service answer
        with _lock:
            hit = _news_cache.get(symbol)
        return hit[1] if hit and hit[0] == ist_today_str() else None
    except Exception:  # noqa: BLE001
        return None
