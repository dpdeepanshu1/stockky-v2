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
import os
import threading
import time
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
            logger.warning("dynamic_universe: subscribe call failed (%s)", _err_text(e))

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
            logger.warning("dynamic_universe: unsubscribe call failed (%s)", _err_text(e))

    # 2026-09-03 — trigger the event tracker's own /check pass so the newly
    # (un)subscribed symbols' cache actually gets populated. Found via audit
    # that nothing anywhere called /check on a schedule — /events/raw-feed
    # only ever reads the cache, never populates it, so Tier 2 could stay
    # permanently cold for any symbol added here. /check iterates every
    # subscription with a 1s stagger (rate-limit friendly by design), so
    # this can take a while with a full 60-symbol universe — generous
    # timeout, and a failure here is logged but never fails the cycle.
    # group253: /check walks every subscription one by one (1 s stagger + a fresh news/event fetch per symbol), which for
    # 60+ symbols takes minutes - the 90 s wait below timed out on every sync ("/check trigger failed (ReadTimeout)")
    # and, worse, held the whole dynamic-universe -> watchlist stage of the cycle for those 90 s. It is a cache warm-up
    # the cycle does not read an answer from (Tier 2 just sees the cache fill a little later), so it now runs on its own daemon thread (cycles run on throw-away event loops in
    # worker threads, which would cancel a background asyncio task) and only one runs at a time.
    # DYNAMIC_UNIVERSE_CHECK_BACKGROUND=0 restores the old wait-for-it behaviour.
    if _check_background_enabled():
        if _start_check_background(_check_background_timeout_s()):
            logger.info("dynamic_universe: /check started in the background to warm the event cache")
        else:
            logger.info("dynamic_universe: /check from an earlier sync is still running - not starting another")
        return result

    try:
        async with httpx.AsyncClient(timeout=90.0) as client:
            r = await client.get(f"{config.EVENT_URL}/check")
            r.raise_for_status()
        logger.info("dynamic_universe: triggered /check to warm the event cache")
    except Exception as e:
        # group170: httpx timeouts stringify to "", which logged "failed ()" - name the type as well
        logger.warning("dynamic_universe: /check trigger failed (%s) — Tier 2 cache may be stale",
                       _err_text(e))

    return result


# group250 (2026-10-08 log): "momentum-movers source failed ()" - an httpx timeout has an empty str(), so the cause was
# invisible, and the 15 s client timeout was shorter than the gateway's cold computation (it lets a follower wait up to
# MOMENTUM_MOVERS_JOIN_WAIT_S = 45 s). The timeout is now DYNAMIC_UNIVERSE_MOVERS_TIMEOUT_S (default 45, blank-safe).
def _err_text(e: BaseException) -> str:
    """'ReadTimeout' for an exception with no message, 'Type: message' otherwise."""
    msg = str(e).strip()
    return f"{type(e).__name__}: {msg}" if msg else type(e).__name__


def _movers_timeout_s() -> float:
    raw = (os.getenv("DYNAMIC_UNIVERSE_MOVERS_TIMEOUT_S") or "").strip()
    try:
        v = float(raw) if raw else 45.0
    except ValueError:
        return 45.0
    return v if v == v and v > 0 else 45.0


# ── group253: /check runs in the background ─────────────────────────────────
_CHECK_LOCK = threading.Lock()
_CHECK_RUNNING = [False]
_CHECK_LAST: dict = {"started": 0, "finished": 0, "ok": None, "elapsed_s": None, "skipped_busy": 0}


def _check_background_enabled() -> bool:
    raw = (os.getenv("DYNAMIC_UNIVERSE_CHECK_BACKGROUND") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _check_background_timeout_s() -> float:
    raw = (os.getenv("DYNAMIC_UNIVERSE_CHECK_TIMEOUT_S") or "").strip()
    try:
        v = float(raw) if raw else 600.0
    except ValueError:
        return 600.0
    return v if v == v and v > 0 else 600.0


def _check_worker(timeout_s: float) -> None:
    """Thread body: GET {EVENT_URL}/check and log the real outcome. Always clears the running flag."""
    t0 = time.monotonic()
    ok = False
    try:
        with httpx.Client(timeout=timeout_s) as client:
            r = client.get(f"{config.EVENT_URL}/check")
            r.raise_for_status()
        ok = True
        logger.info("dynamic_universe: /check finished in %.0fs - event cache warmed", time.monotonic() - t0)
    except Exception as e:  # noqa: BLE001 - a warm-up must never raise out of its thread
        logger.warning("dynamic_universe: /check trigger failed (%s) after %.0fs — Tier 2 cache may be stale",
                       _err_text(e), time.monotonic() - t0)
    finally:
        with _CHECK_LOCK:
            _CHECK_RUNNING[0] = False
            _CHECK_LAST["finished"] += 1
            _CHECK_LAST["ok"] = ok
            _CHECK_LAST["elapsed_s"] = round(time.monotonic() - t0, 1)


def _start_check_background(timeout_s: float) -> bool:
    """Start one /check thread. False when one is already running (nothing started) or the thread could not start."""
    with _CHECK_LOCK:
        if _CHECK_RUNNING[0]:
            _CHECK_LAST["skipped_busy"] += 1
            return False
        _CHECK_RUNNING[0] = True
        _CHECK_LAST["started"] += 1
    try:
        threading.Thread(target=_check_worker, args=(timeout_s,), name="dynamic-universe-check", daemon=True).start()
    except Exception as e:  # noqa: BLE001
        with _CHECK_LOCK:
            _CHECK_RUNNING[0] = False
        logger.warning("dynamic_universe: could not start the /check thread (%s)", _err_text(e))
        return False
    return True


def check_status() -> dict:
    """Snapshot of the background /check (for tests and any status endpoint)."""
    with _CHECK_LOCK:
        return {"running": _CHECK_RUNNING[0], **_CHECK_LAST}


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
        logger.warning("dynamic_universe: volume-shock source failed (%s), continuing with momentum-movers only", _err_text(e))

    _t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=_movers_timeout_s()) as client:
            r = await client.get(f"{config.API_GATEWAY_URL}/market/momentum-movers")
            r.raise_for_status()
            movers = r.json().get("symbols", [])
        symbols.extend(movers)
    except Exception as e:
        logger.warning("dynamic_universe: momentum-movers source failed after %.0fs (%s), continuing with volume-shock only",
                       time.monotonic() - _t0, _err_text(e))

    return list(dict.fromkeys(s.upper() for s in symbols))  # de-dup, preserve order


async def _get_current_auto_subscriptions() -> set[str]:
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(f"{config.EVENT_URL}/subscriptions", params={"source": "auto"})
        r.raise_for_status()
        data = r.json()
    return set(data.get("subscriptions", []))
