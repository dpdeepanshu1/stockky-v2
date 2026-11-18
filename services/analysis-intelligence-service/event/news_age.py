"""Round 600: ONE rule for how old a news item may be, shared by the event tracker and the news service.

Rule: a news item older than NEWS_MAX_AGE_DAYS (default 3 = 72 h) is not recorded, not cached, not scored and not served. An item with no
readable date is dropped too (we cannot show it is recent). The setting can only LOWER the window: a value outside 1..3 (or a typo / blank)
is ignored and the default 3 is used, so nobody can raise it past three days by accident.

Why this file exists: before round 600 each fetcher had its own cut-off (30, 14, 10 and 7 days), the Yahoo items were never age-checked at all
(their date arrives as an epoch number or an ISO string), a missing date was kept, and the 30-day "fallback" copy was served as if it were fresh.

Standard library only. This file is copied byte for byte into event/ and news/ (the mounted sub-apps hide each other's folders from sys.path,
the same reason env_util.py is copied); tests/test_round600_news_age.py fails when the two copies differ.

Public: news_max_age_days(), news_max_age_seconds(), news_cutoff(now), parse_news_time(value), news_age_seconds(value, now),
is_fresh_news(value, now), filter_fresh_news(items, key, now, source), age_drop_stats(), maybe_log_age_drops(logger).
All naive datetimes are read as UTC, the convention of both services.
"""
from __future__ import annotations

import math
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Dict, Iterable, List, Optional, Tuple

DEFAULT_DAYS = 3.0
MIN_DAYS = 1.0
MAX_DAYS = 3.0          # the hard ceiling: three days, never more
FUTURE_SLACK_S = 6 * 3600.0   # a stamp this far ahead is clock skew; further ahead it is bad data and is dropped


def news_max_age_days() -> float:
    """NEWS_MAX_AGE_DAYS, 1..3 (default 3). Blank, typo, NaN, below 1 or above 3 -> 3. Read at call time, so a changed .env needs no code change."""
    raw = (os.getenv("NEWS_MAX_AGE_DAYS") or "").strip()
    if not raw:
        return DEFAULT_DAYS
    try:
        v = float(raw)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    if not math.isfinite(v) or v < MIN_DAYS or v > MAX_DAYS:
        return DEFAULT_DAYS
    return v


def news_max_age_seconds() -> float:
    return news_max_age_days() * 86400.0


def _naive_utc(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc).replace(tzinfo=None)
    if now.tzinfo is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None)
    return now


def news_cutoff(now: Optional[datetime] = None) -> datetime:
    """The oldest publish time still allowed, as a naive-UTC datetime."""
    return _naive_utc(now) - timedelta(seconds=news_max_age_seconds())


def parse_news_time(value: Any) -> Optional[datetime]:
    """A publish time as a naive-UTC datetime, or None when it cannot be read.

    Reads: datetime; epoch seconds or milliseconds (number or digit string, which is how Yahoo's providerPublishTime arrives); ISO-8601 with
    Z / an offset / none; RFC-822 ("Tue, 17 Nov 2026 10:00:00 GMT", the RSS form)."""
    if value is None or isinstance(value, bool):
        return None
    try:
        if isinstance(value, datetime):
            return _naive_utc(value)
        if isinstance(value, (int, float)):
            return _from_epoch(float(value))
        s = str(value).strip()
        if not s or s.lower() in ("none", "null", "nan"):
            return None
        if s.replace(".", "", 1).isdigit():
            return _from_epoch(float(s))
        iso = s[:-1] + "+00:00" if s.endswith(("Z", "z")) else s
        try:
            return _naive_utc(datetime.fromisoformat(iso))
        except ValueError:
            pass
        return _naive_utc(parsedate_to_datetime(s))
    except Exception:  # noqa: BLE001 - an unreadable stamp is "unknown", never an exception
        return None


def _from_epoch(x: float) -> Optional[datetime]:
    if not math.isfinite(x) or x <= 0:
        return None
    if x > 1e11:            # milliseconds
        x /= 1000.0
    if x < 1e8:             # before 1973: not a news time
        return None
    return datetime.fromtimestamp(x, tz=timezone.utc).replace(tzinfo=None)


def news_age_seconds(value: Any, now: Optional[datetime] = None) -> Optional[float]:
    d = parse_news_time(value)
    if d is None:
        return None
    return (_naive_utc(now) - d).total_seconds()


def is_fresh_news(value: Any, now: Optional[datetime] = None, max_age_days: Optional[float] = None) -> bool:
    """True only when the stamp is readable and no older than the window. Missing / unreadable -> False (the 3-day rule cannot be shown to hold)."""
    age = news_age_seconds(value, now)
    if age is None:
        return False
    limit = (news_max_age_seconds() if max_age_days is None else float(max_age_days) * 86400.0)
    return -FUTURE_SLACK_S <= age <= limit


# ── drop counters: how many items each source lost to the age rule since the process started ─────────────────────────────────────────────
_LOCK = threading.Lock()
_STATS: Dict[str, Dict[str, int]] = {}
_LAST_LOG = [0.0]


def _note(source: str, kept: int, dropped: int, undated: int) -> None:
    with _LOCK:
        st = _STATS.setdefault(source or "unknown", {"kept": 0, "dropped_old": 0, "dropped_undated": 0})
        st["kept"] += kept
        st["dropped_old"] += dropped - undated
        st["dropped_undated"] += undated


def filter_fresh_news(items: Optional[Iterable[Dict[str, Any]]], key: str = "published", now: Optional[datetime] = None,
                      source: str = "") -> List[Dict[str, Any]]:
    """The items whose `key` stamp is within the window, in their original order. Anything that is not a dict is dropped. Counts what it dropped."""
    kept: List[Dict[str, Any]] = []
    dropped = undated = 0
    for it in (items or []):
        if not isinstance(it, dict):
            dropped += 1
            continue
        if is_fresh_news(it.get(key), now):
            kept.append(it)
        else:
            dropped += 1
            if parse_news_time(it.get(key)) is None:
                undated += 1
    if kept or dropped:
        _note(source, len(kept), dropped, undated)
    return kept


def age_drop_stats() -> Dict[str, Any]:
    with _LOCK:
        per = {k: dict(v) for k, v in _STATS.items()}
    return {"max_age_days": news_max_age_days(), "sources": per}


def maybe_log_age_drops(logger: Any, every_s: float = 3600.0) -> None:
    """One INFO line per hour per process: what each source lost to the 3-day rule. A source that drops everything is a dead or frozen feed."""
    now = time.time()
    with _LOCK:
        if now - _LAST_LOG[0] < every_s or not _STATS:
            return
        _LAST_LOG[0] = now
        parts = [f"{k}: kept {v['kept']}, old {v['dropped_old']}, undated {v['dropped_undated']}" for k, v in sorted(_STATS.items())]
    try:
        logger.info("news age rule (%.0f d) since start - %s", news_max_age_days(), "; ".join(parts))
    except Exception:  # noqa: BLE001
        pass
