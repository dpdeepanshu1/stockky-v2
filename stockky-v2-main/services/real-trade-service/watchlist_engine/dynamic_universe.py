"""
watchlist_engine/dynamic_universe.py — 2026-09-03 dynamic watchlist upgrade.

Resolves the open item from the last audit: Tier 2's coverage
(`/events/raw-feed`) was bounded by a static notification watchlist, so it
could only re-surface catalysts for names already being tracked, not
discover new ones.

This module periodically (every REFRESH_INTERVAL_MIN, market hours only)
re-computes a "desired auto-subscribe set" from the same volume-shock
scanner already used for Tier 3 (candidate_engine._fetch_volume_shock_
universe — proven, already running), diffs it against what's currently
auto-subscribed on the event tracker, and syncs the difference:
  - newly-active symbols get /subscribe'd with source="auto"
  - symbols that fell out of activity get /unsubscribe'd, but ONLY if
    they were tagged source="auto" — a symbol the user manually added to
    their notification watchlist (source="user") is never touched here.

This makes the watchlist genuinely dynamic across the trading day instead
of a fixed list decided once — stocks get added and dropped as the
situation changes, same as the entry/exit logic already does for
positions.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import Optional

import httpx

import config
from tz_utils import is_market_open_ist

logger = logging.getLogger("real-trade-dynamic-universe")

# How often this runs, in minutes — deliberately not every cycle (the
# entry trigger pass runs every 1-2 min; re-syncing the universe that
# often would just churn subscribe/unsubscribe calls for no benefit).
REFRESH_INTERVAL_MIN = 20

# Cap on how many symbols this module will keep auto-subscribed at once,
# so a noisy market day can't make the event tracker's per-cycle scan
# (`/check`, `/events/raw-feed`) unboundedly slow.
MAX_AUTO_SYMBOLS = 60

_last_run_ts: Optional[float] = None


def _due() -> bool:
    global _last_run_ts
    if _last_run_ts is None:
        return True
    return (time.monotonic() - _last_run_ts) >= REFRESH_INTERVAL_MIN * 60


async def refresh_dynamic_universe(db=None) -> Optional[dict]:
    """
    Call once per cycle from cycle_runner — this function no-ops (cheaply)
    unless it's actually due and the market is open, so it's safe to call
    unconditionally every cycle.
    """
    global _last_run_ts

    if not is_market_open_ist():
        return None
    if not _due():
        return None
    _last_run_ts = time.monotonic()

    try:
        desired = await _compute_desired_universe()
    except Exception as e:
        logger.warning("dynamic_universe: failed to compute desired set (%s), skipping this cycle", e)
        return None

    if not desired:
        logger.info("dynamic_universe: no active symbols found this cycle, nothing to sync")
        return None

    desired = desired[:MAX_AUTO_SYMBOLS]

    try:
        current_auto = await _get_current_auto_subscriptions()
    except Exception as e:
        logger.warning("dynamic_universe: failed to read current subscriptions (%s), skipping sync", e)
        return None

    to_add = sorted(set(desired) - current_auto)
    to_remove = sorted(current_auto - set(desired))

    result = {"added": [], "removed": [], "kept": len(set(desired) & current_auto)}

    if to_add:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{config.EVENT_URL}/subscribe",
                    json={"symbols": to_add, "source": "auto"},
                )
                r.raise_for_status()
            result["added"] = to_add
            logger.info("dynamic_universe: added %d symbols: %s", len(to_add), to_add)
        except Exception as e:
            logger.warning("dynamic_universe: subscribe call failed (%s)", e)

    if to_remove:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                r = await client.post(
                    f"{config.EVENT_URL}/unsubscribe",
                    json={"symbols": to_remove, "only_source": "auto"},
                )
                r.raise_for_status()
            result["removed"] = to_remove
            logger.info("dynamic_universe: dropped %d inactive symbols: %s", len(to_remove), to_remove)
        except Exception as e:
            logger.warning("dynamic_universe: unsubscribe call failed (%s)", e)

    # 2026-09-03 — trigger the event tracker's own /check pass so the newly
    # (un)subscribed symbols' cache actually gets populated. Found via audit
    # that nothing anywhere called /check on a schedule — /events/raw-feed
    # only ever reads the cache, never populates it, so Tier 2 could stay
    # permanently cold for any symbol added here. /check iterates every
    # subscription with a 1s stagger (rate-limit friendly by design), so
    # this can take a while with a full 60-symbol universe — generous
    # timeout, and a failure here is logged but never fails the cycle.
    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.get(f"{config.EVENT_URL}/check")
            r.raise_for_status()
        logger.info("dynamic_universe: triggered /check to warm the event cache")
    except Exception as e:
        logger.warning("dynamic_universe: /check trigger failed (%s) — Tier 2 cache may be stale", e)

    return result


async def _compute_desired_universe() -> list[str]:
    """
    Broad, cheap "what's actually moving right now" source.

    2026-09-03 widened: previously volume-shock only (candidate_engine's
    scanner, still the primary source — proven, already running for
    Tier 3). Now unions in api-gateway's full-market NSE gainers/losers/
    volume-gainers board too (/market/momentum-movers, a thin wrapper
    around the existing _get_momentum_movers used internally by hot-picks)
    — this catches broader momentum than pure volume-shock alone, without
    inventing a third detection mechanism. Either source failing is
    non-fatal; the other still contributes.
    """
    from candidate_engine.candidates import _fetch_volume_shock_universe

    symbols: list[str] = []

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            vol_shock = await _fetch_volume_shock_universe(client)
        symbols.extend(vol_shock)
    except Exception as e:
        logger.warning("dynamic_universe: volume-shock source failed (%s), continuing with momentum-movers only", e)

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            r = await client.get(f"{config.API_GATEWAY_URL}/market/momentum-movers")
            r.raise_for_status()
            movers = r.json().get("symbols", [])
        symbols.extend(movers)
    except Exception as e:
        logger.warning("dynamic_universe: momentum-movers source failed (%s), continuing with volume-shock only", e)

    return list(dict.fromkeys(s.upper() for s in symbols))  # de-dup, preserve order


async def _get_current_auto_subscriptions() -> set[str]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{config.EVENT_URL}/subscriptions", params={"source": "auto"})
        r.raise_for_status()
        data = r.json()
    return set(data.get("subscriptions", []))
