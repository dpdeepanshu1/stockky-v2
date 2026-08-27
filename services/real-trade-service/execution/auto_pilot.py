"""
execution/auto_pilot.py — Auto-Pilot: runs the exact same cycle
POST /cycle/run/{mode} runs (via cycle_runner.run_cycle_core), on a
server-side timer, so a mode that is ARMED and has auto_pilot_enabled=True
keeps trading (or paper-trading) without anyone having the dashboard
open. Every tick's outcome — and any auto-pilot-specific error — goes out
over Telegram (see notifier.py) since that's the whole point: "set it
once, then get told what happened," not "silently do things unattended."

This is a single asyncio task started once at app startup (same pattern
as notification-scheduler-service/scheduler/main.py's Neon keep-alive
loop) — it runs inside this FastAPI process's own event loop, so it only
runs while the process is up. On Render/most PaaS free tiers a service
can sleep after inactivity; if REAL auto-pilot must survive that, keep
the service on a plan/host that doesn't idle it out (the existing
scheduler's Neon keep-alive comment already flags the same constraint
for this codebase).

Safety properties, by construction:
  - Never bypasses arming. Every tick re-reads `armed` AND
    `auto_pilot_enabled` fresh from the DB — an auto-disarm (session
    expiry, daily-loss cap trip, emergency pause) silently stops this
    loop from doing anything on its very next tick, same as it stops a
    manual Run Cycle click.
  - Never overlaps itself for the same mode — each tick awaits its own
    DB session close before the next `asyncio.sleep`, and the two modes
    (DEMO/REAL) are ticked sequentially within one loop iteration, not
    as concurrent tasks, so there's no risk of two cycles for the same
    mode racing each other.
  - A cycle exception is caught, logged, and reported over Telegram —
    it never kills the loop itself (a bad cycle should not mean silence
    forever after).
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

_task: Optional[asyncio.Task] = None
_STARTUP_DELAY_SECONDS = 20  # let schema init / seeding finish first


def _summarize(mode: str, result: dict) -> tuple[str, bool]:
    """Returns (message, had_activity). had_activity gates whether a
    quiet tick still sends a heartbeat (config.AUTO_PILOT_NOTIFY_HEARTBEAT)."""
    entry = result.get("entry") or {}
    exit_ = result.get("exit") or {}
    entered = entry.get("entered", 0)
    fills = result.get("fills", 0) or 0
    rejected = entry.get("rejected", 0)
    partial = exit_.get("partial_exits", 0)
    full = exit_.get("full_exits", 0)
    time_stops = exit_.get("time_stops", 0)
    trailed = exit_.get("trailed", 0)
    new_candidates = result.get("new_candidates", 0) or 0

    activity = bool(entered or fills or partial or full or time_stops or rejected)

    lines = [f"🤖 *Auto-Pilot — {mode}*"]
    lines.append(
        f"Candidates: {new_candidates} new · Entries sent: {entered} "
        f"({rejected} risk-rejected) · Fills: {fills}"
    )
    if partial or full or time_stops or trailed:
        lines.append(
            f"Exits — partial: {partial}, full: {full}, time-stop: {time_stops}, trailed: {trailed}"
        )
    if not activity:
        lines.append("Nothing actionable this cycle.")
    return "\n".join(lines), activity


async def _tick(mode: str) -> None:
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
                f"Re-authenticate with a fresh Dhan token to resume."
            )
            return
        message, activity = _summarize(mode, result)
        if activity or config.AUTO_PILOT_NOTIFY_HEARTBEAT:
            await notify_async(message)
    except Exception as e:  # noqa: BLE001 — one bad tick must never kill the loop
        logger.exception("auto-pilot tick failed for %s", mode)
        await notify_async(f"⚠️ *Auto-Pilot error — {mode}*\n{str(e)[:300]}")
    finally:
        db.close()


async def _loop() -> None:
    await asyncio.sleep(_STARTUP_DELAY_SECONDS)
    logger.info(
        "Auto-pilot loop running (interval=%ss, market-hours only, IST)",
        config.AUTO_PILOT_INTERVAL_SECONDS,
    )
    while True:
        for mode in ("DEMO", "REAL"):
            try:
                await _tick(mode)
            except Exception:  # belt-and-braces — _tick already catches its own errors
                logger.exception("auto-pilot: unexpected error ticking %s", mode)
        await asyncio.sleep(config.AUTO_PILOT_INTERVAL_SECONDS)


def start() -> None:
    """Idempotent — safe to call from startup() even if hot-reloaded."""
    global _task
    if _task is not None and not _task.done():
        return
    _task = asyncio.create_task(_loop())
    logger.info("Auto-pilot background task created.")
