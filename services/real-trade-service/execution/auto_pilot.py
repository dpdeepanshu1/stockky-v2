"""
execution/auto_pilot.py — Auto-Pilot with decoupled fast exit cadence.

IMPROVEMENTS (per improvement plan):
═════════════════════════════════════
1. DECOUPLED EXIT CADENCE (plan section 1.3): two independent async timers:
   - Fast exit-only loop: every EXIT_CHECK_INTERVAL_SECONDS (default 45s)
     runs exit_engine + reconcile only. Catches stops/targets between full cycles.
   - Full cycle loop: every AUTO_PILOT_INTERVAL_SECONDS (default 180s) runs
     candidates → entry → fills → expire → exit → reconcile.
   Both loops share a per-mode asyncio.Lock — no concurrent cycles for same mode.

2. IDEMPOTENCY GUARD (plan section 1.7): asyncio.Lock per mode prevents the
   manual "Run Cycle" button + auto-pilot timer from racing each other. The
   lock is module-level and checked before any cycle work begins.

3. All original safety properties preserved:
   - Never bypasses arming (re-reads gate fresh from DB every tick)
   - A bad tick is caught, logged, Telegram-notified, never kills the loop
   - Market-hours guard (IST 09:15-15:30)
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

import config
import models
from db import get_session_factory
from notifier import notify_async
from tz_utils import is_market_open_ist

logger = logging.getLogger("real-trade-autopilot")

_full_task: Optional[asyncio.Task] = None
_exit_task: Optional[asyncio.Task] = None
_STARTUP_DELAY_SECONDS = 20

# Per-mode locks — prevent concurrent cycles (manual + auto-pilot race)
# Lazily initialised inside the event loop to avoid the Python 3.10+ restriction
# that asyncio.Lock() must be created inside a running event loop.
_mode_locks: dict = {}

import os as _os
EXIT_CHECK_INTERVAL_SECONDS = max(
    20, int(_os.getenv("EXIT_CHECK_INTERVAL_SECONDS", "45"))
)


def _get_lock(mode: str) -> asyncio.Lock:
    """Return (creating if needed) the per-mode asyncio.Lock.
    Safe to call from inside a coroutine — never at module import time."""
    if mode not in _mode_locks:
        _mode_locks[mode] = asyncio.Lock()
    return _mode_locks[mode]


def _summarize(mode: str, result: dict) -> tuple[str, bool]:
    entry  = result.get("entry") or {}
    exit_  = result.get("exit") or {}
    entered    = entry.get("entered", 0)
    fills      = result.get("fills", 0) or 0
    rejected   = entry.get("rejected", 0)
    partial    = exit_.get("partial_exits", 0)
    full       = exit_.get("full_exits", 0)
    time_stops = exit_.get("time_stops", 0)
    trailed    = exit_.get("trailed", 0)
    emergency  = exit_.get("emergency_exits", 0)
    new_cands  = result.get("new_candidates", 0) or 0
    activity   = bool(entered or fills or partial or full or time_stops or rejected or emergency)
    lines = [f"🤖 *Auto-Pilot — {mode}*"]
    lines.append(
        f"Candidates: {new_cands} new · Entries sent: {entered} "
        f"({rejected} risk-rejected) · Fills: {fills}"
    )
    if partial or full or time_stops or trailed or emergency:
        lines.append(
            f"Exits — partial: {partial}, full: {full}, time-stop: {time_stops}, "
            f"trailed: {trailed}, emergency: {emergency}"
        )
    if not activity:
        lines.append("Nothing actionable this cycle.")
    regime = result.get("entry", {}).get("regime") or {}
    if regime.get("score"):
        lines.append(
            f"Market score: {regime['score']} (gate={regime.get('gate')},{regime.get('source','static')})"
        )
    return "\n".join(lines), activity


async def _exit_only_tick(mode: str) -> None:
    """Fast exit-only tick: exit_engine + reconcile, no candidates/entry."""
    lock = _get_lock(mode)
    if lock.locked():
        return  # full cycle is running — skip this fast tick, it'll cover exits
    async with lock:
        Session = get_session_factory()
        db = Session()
        try:
            gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
            if gate is None or not gate.armed or not gate.auto_pilot_enabled:
                return
            if not is_market_open_ist():
                return
            from exit_engine.exit import evaluate_mode as exit_evaluate
            exit_result = await exit_evaluate(db, mode)
            if mode == "REAL":
                from execution.reconcile import reconcile_real_orders
                await reconcile_real_orders(db)
            # Notify only if something actually happened (no heartbeat on fast tick)
            exit_ = exit_result or {}
            if any([
                exit_.get("full_exits"), exit_.get("partial_exits"),
                exit_.get("time_stops"), exit_.get("emergency_exits"),
            ]):
                await notify_async(
                    f"⚡ *Fast exit tick — {mode}*\n"
                    f"Full: {exit_.get('full_exits',0)} | Partial: {exit_.get('partial_exits',0)} | "
                    f"Time-stop: {exit_.get('time_stops',0)} | Emergency: {exit_.get('emergency_exits',0)}"
                )
        except Exception as e:
            logger.exception("exit-only tick failed for %s", mode)
            await notify_async(f"⚠️ *Fast exit tick error — {mode}*\n{str(e)[:200]}")
        finally:
            db.close()


async def _full_tick(mode: str) -> None:
    """Full cycle tick: candidates → entry → fills → expire → exit → reconcile."""
    lock = _get_lock(mode)
    async with lock:
        Session = get_session_factory()
        db = Session()
        try:
            gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
            if gate is None or not gate.armed or not gate.auto_pilot_enabled:
                return
            if not is_market_open_ist():
                return
            from cycle_runner import run_cycle_core
            result = await run_cycle_core(db, mode, gate.armed, trigger="autopilot")
            if result.get("auto_disarmed"):
                await notify_async(
                    f"🔴 *Auto-Pilot disarmed — {mode}*\n{result['auto_disarmed']}\n"
                    "Re-authenticate with a fresh Dhan token to resume."
                )
                return
            message, activity = _summarize(mode, result)
            if activity or config.AUTO_PILOT_NOTIFY_HEARTBEAT:
                await notify_async(message)
        except Exception as e:
            logger.exception("auto-pilot full tick failed for %s", mode)
            await notify_async(f"⚠️ *Auto-Pilot error — {mode}*\n{str(e)[:300]}")
        finally:
            db.close()


async def _fast_exit_loop() -> None:
    """Fast exit-only loop — tighter cadence than the full cycle."""
    await asyncio.sleep(_STARTUP_DELAY_SECONDS + 5)
    logger.info(
        "Auto-pilot FAST EXIT loop running (interval=%ss)", EXIT_CHECK_INTERVAL_SECONDS
    )
    while True:
        for mode in ("DEMO", "REAL"):
            try:
                await _exit_only_tick(mode)
            except Exception:
                logger.exception("fast exit loop: unexpected error for %s", mode)
        await asyncio.sleep(EXIT_CHECK_INTERVAL_SECONDS)


async def _full_cycle_loop() -> None:
    """Full cycle loop — candidates, entry, exit, reconcile."""
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    logger.info(
        "Auto-pilot FULL CYCLE loop running (interval=%ss, market-hours only, IST)",
        config.AUTO_PILOT_INTERVAL_SECONDS,
    )
    while True:
        for mode in ("DEMO", "REAL"):
            try:
                await _full_tick(mode)
            except Exception:
                logger.exception("full cycle loop: unexpected error for %s", mode)
        await asyncio.sleep(config.AUTO_PILOT_INTERVAL_SECONDS)


def start() -> None:
    """Idempotent — safe to call from startup() even if hot-reloaded."""
    global _full_task, _exit_task
    if _full_task is None or _full_task.done():
        _full_task = asyncio.create_task(_full_cycle_loop())
        logger.info("Auto-pilot FULL CYCLE background task created.")
    if _exit_task is None or _exit_task.done():
        _exit_task = asyncio.create_task(_fast_exit_loop())
        logger.info("Auto-pilot FAST EXIT background task created (interval=%ss).",
                    EXIT_CHECK_INTERVAL_SECONDS)
