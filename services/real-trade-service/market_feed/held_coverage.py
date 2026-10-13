"""market_feed/held_coverage.py - group344 (item 4 of the 13 Oct review).

BIRET, HEG and BMISL have no AngelOne token and Yahoo calls them "possibly delisted"; AAKASH 404s. If such a symbol is an open
position, its price can be stale while a stop depends on it. This asks market-data (POST /internal/feed-coverage) about every held
symbol, at most once per HELD_COVERAGE_CHECK_INTERVAL_S, and warns (log, and Telegram once per symbol per IST day) when a held symbol
has neither an AngelOne token nor a Dhan security id, or is on the known-delisted list. It only reports; it never trades.
HELD_COVERAGE_CHECK_INTERVAL_S=0 turns it off. Never raises.
"""
from __future__ import annotations

import logging
import os
import time as _time
from typing import Iterable, Optional

import httpx

logger = logging.getLogger("real-trade-held-coverage")

_last_run = [-1e12]
_alerted: set = set()          # (ist day, symbol)
_last_result: dict = {"checked_at": None, "weak": [], "symbols": 0}


def _interval_s() -> float:
    try:
        return max(0.0, float((os.getenv("HELD_COVERAGE_CHECK_INTERVAL_S") or "").strip() or 600.0))
    except ValueError:
        return 600.0


def weak_symbols(rows: Iterable[dict]) -> list:
    """Rows of the coverage answer that have no primary feed (pure; unknown answers are not reported)."""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        if r.get("known_delisted") or r.get("primary_feed") is False:
            out.append(r)
    return out


def status() -> dict:
    return dict(_last_result)


async def check(symbols: Iterable[str], *, now_mono: Optional[float] = None, client: Optional[httpx.AsyncClient] = None) -> list:
    """Check `symbols` (the open positions). Returns the weak rows ([] when fine, throttled, off or failed)."""
    try:
        interval = _interval_s()
        syms = sorted({str(s).upper().replace(".NS", "").replace(".BO", "").strip() for s in symbols or [] if s})
        if interval <= 0 or not syms:
            return []
        now = _time.monotonic() if now_mono is None else now_mono
        if now - _last_run[0] < interval:
            return []
        _last_run[0] = now
        import config
        from tz_utils import ist_today_str
        url = f"{config.MARKET_DATA_URL.rstrip('/')}/internal/feed-coverage" if hasattr(config, "MARKET_DATA_URL") else None
        if url is None:
            from market_feed.feed import MARKET_DATA_URL
            url = f"{MARKET_DATA_URL.rstrip('/')}/internal/feed-coverage"
        own = client is None
        c = client or httpx.AsyncClient()
        try:
            r = await c.post(url, json={"symbols": syms}, timeout=8.0)
        finally:
            if own:
                await c.aclose()
        if r.status_code != 200:
            return []
        rows = (r.json() or {}).get("symbols") or []
        weak = weak_symbols(rows)
        _last_result.update({"checked_at": _time.time(), "weak": [w.get("symbol") for w in weak], "symbols": len(syms)})
        if weak:
            names = ", ".join(str(w.get("symbol")) for w in weak)
            logger.warning("held-symbol feed check: %s has no AngelOne token / Dhan security id (or is on the delisted list) - "
                           "its price may be stale or come only from Yahoo; a stop on it cannot be trusted", names)
            day = ist_today_str()
            fresh = [w for w in weak if (day, w.get("symbol")) not in _alerted]
            for w in fresh:
                _alerted.add((day, w.get("symbol")))
            if fresh:
                try:
                    import notifier
                    notifier.notify_fire_and_forget(
                        "⚠️ <b>Held symbol without a primary price feed</b> — " + ", ".join(str(w.get("symbol")) for w in fresh)
                        + "\nNo AngelOne token / Dhan id, or delisted. Check the price and the stop by hand.")
                except Exception:  # noqa: BLE001
                    pass
        return weak
    except Exception as e:  # noqa: BLE001
        logger.debug("held coverage check failed (ignored): %s", e)
        return []
