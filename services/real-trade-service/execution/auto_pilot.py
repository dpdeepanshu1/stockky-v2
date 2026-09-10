"""
execution/auto_pilot.py — Auto-Pilot with decoupled fast exit cadence.

IMPROVEMENTS (per improvement plan):
═════════════════════════════════════
1. DECOUPLED EXIT CADENCE (plan section 1.3): two independent async timers:
   - Fast exit-only loop: every EXIT_CHECK_INTERVAL_SECONDS (default 45s)
     runs exit_engine + reconcile only. Catches stops/targets between full cycles.
   - Full cycle loop: every AUTO_PILOT_INTERVAL_SECONDS (default 180s) runs
     candidates → entry → fills → expire → exit → reconcile.
   Both loops share a per-mode lock — no concurrent cycles for same mode.

2. IDEMPOTENCY GUARD (plan section 1.7): a lock per mode prevents the
   fast-exit, full-cycle, and schedule loops from racing each other for the
   same mode. The lock is module-level and checked before any cycle work
   begins.

3. All original safety properties preserved:
   - Never bypasses arming (re-reads gate fresh from DB every tick)
   - A bad tick is caught, logged, Telegram-notified, never kills the loop
   - Market-hours guard (IST 09:15-15:30)

4. EVENT-LOOP ISOLATION (2026-09-10 fix — see decision #28's original
   diagnosis and the prior "NOT fixed" note this replaces): every tick's
   body — Session() through db.close(), including its own awaited network
   calls — now runs on a dedicated worker thread with its own fresh event
   loop (`asyncio.run` inside `asyncio.to_thread`), instead of directly on
   the shared main loop that also serves /health and every other HTTP
   route. A slow cycle (remote-Oracle round trips across many open
   positions) can no longer block that shared loop long enough to fail the
   healthcheck window while the process is demonstrably alive — the exact
   symptom decision #28 diagnosed and only mitigated (via a widened
   healthcheck window) at the time.

   The per-mode mutual-exclusion lock changed from asyncio.Lock to
   threading.Lock for this: asyncio.Lock is bound to the loop that created
   it and is not safe to acquire from a different loop/thread, which is
   exactly why moving the tick body off the main loop was flagged as
   needing live-stack verification before attempting. threading.Lock has
   no such binding and is the standard primitive for guarding a resource
   shared across OS threads, so it's used here instead — each tick's wait/
   skip-if-busy semantics from before this fix are preserved exactly (see
   _run_full_tick_sync/_run_exit_tick_sync/_run_schedule_tick_sync), just
   with the wait itself now happening on the worker thread, never on the
   main loop.

   What was checked before making this change (no live stack access from
   this sandbox, so this is code-audit confidence, not live verification):
     - This file's asyncio.Lock was the ONLY module-level asyncio.Lock/
       Event/Queue anywhere in real-trade-service (grepped the whole
       service) — nothing else could be broken by a lock-type change.
     - No module in this service holds a persistent, shared
       httpx.AsyncClient at module scope — every call site uses
       `async with httpx.AsyncClient() as client:` fresh per call, so
       there's no loop-bound HTTP client that a worker thread's own event
       loop could collide with.
     - db.py's engine is a standard SQLAlchemy create_engine() connection
       pool, which is explicitly designed to be thread-safe for concurrent
       checkout from multiple threads — each tick already creates its own
       fresh Session() per call (never a shared one), so running that on a
       different OS thread each time is the same access pattern SQLAlchemy
       is built for, not a new one.
     - _fast_exit_loop/_full_cycle_loop/_schedule_loop each still iterate
       `for mode in ("DEMO","REAL"): await <tick>(mode)` sequentially, and
       `await asyncio.to_thread(...)` still blocks that coroutine until the
       thread finishes — so DEMO and REAL still never run concurrently
       WITHIN the same loop, exactly as before this change.
   Known residual, NOT changed by this fix: the three loops themselves
   (fast-exit / full-cycle / schedule) already ran concurrently with each
   other pre-existing (that's the whole point of the decoupled-cadence
   design), cooperatively interleaved on one loop before this change. With
   this fix, that pre-existing cross-loop concurrency becomes real OS-
   thread concurrency instead. The one place this matters:
   entry_engine/entry.py's module-level `_regime_cache` dict (2-minute TTL
   cache for the market-regime score) is read/written without its own lock
   and is shared across modes — two full-cycle ticks for different modes
   landing in the exact same instant on different threads could each see
   it as stale and both re-fetch, then both write, last-writer-wins. This
   is a benign, pre-existing race (duplicate fetch, not a correctness bug
   for order placement or sizing) that async interleaving already made
   possible in principle; it is not being fixed here as part of this
   change, and does not affect risk_engine, order placement, or position
   sizing, which all go through the per-mode lock.

   NOT LIVE-TESTED — this is a bigger, higher-stakes change than a
   config/weight tweak. Run it in DEMO first: watch that
   "Auto-pilot FULL CYCLE loop running" ticks still fire normally, that
   /health stays responsive (curl it) during/after a busy cycle, and that
   no cycle silently double-runs for a mode (check TradeOrder rows for
   duplicates) before trusting this with REAL capital.
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Optional

import config
import models
from db import get_session_factory
from notifier import notify_async
from tz_utils import (
    is_market_open_ist,
    ist_now,
    ist_today_str,
    parse_hhmm,
    ist_time_at_or_after,
    is_ist_weekday,
)

logger = logging.getLogger("real-trade-autopilot")

_full_task: Optional[asyncio.Task] = None
_exit_task: Optional[asyncio.Task] = None
_schedule_task: Optional[asyncio.Task] = None
_totp_task: Optional[asyncio.Task] = None
_STARTUP_DELAY_SECONDS = 20

# Per-mode locks — prevent concurrent cycles (manual + auto-pilot race).
# threading.Lock, not asyncio.Lock — see point 4 in the module docstring
# above for why: each tick's body (and its wait/skip-if-busy check for this
# lock) now runs on its own worker thread via asyncio.to_thread, and
# asyncio.Lock is bound to the loop that created it, so it can't safely be
# acquired from a different thread/loop the way threading.Lock can.
# threading.Lock has no "must be created inside a running loop" restriction
# either, so — unlike the asyncio.Lock it replaces — it's safe to create
# eagerly instead of lazily; kept lazy anyway to avoid touching every call
# site's assumption that _get_lock(mode) is idempotent per mode.
_mode_locks: dict = {}
# BUG FIX (2026-09-10): _get_lock's lazy-init check was not itself thread-safe.
# Two worker threads arriving at `if mode not in _mode_locks` at the same
# instant (e.g. the DEMO full-cycle and the DEMO fast-exit worker threads both
# starting for the first time) could each find the key absent, then each insert
# a DIFFERENT threading.Lock — the second write overwrites the first, so the
# two threads hold references to two different lock objects and the mutual
# exclusion is void. A lightweight meta-lock serialises the dict writes
# without touching the per-mode lock lifetime at all.
_mode_locks_meta: threading.Lock = threading.Lock()

import os as _os
EXIT_CHECK_INTERVAL_SECONDS = max(
    20, int(_os.getenv("EXIT_CHECK_INTERVAL_SECONDS", "45"))
)


def _get_lock(mode: str) -> threading.Lock:
    """Return (creating if needed) the per-mode threading.Lock.

    BUG FIX (2026-09-10): guarded by _mode_locks_meta so two threads can
    never simultaneously insert different Lock objects for the same mode.
    """
    with _mode_locks_meta:
        if mode not in _mode_locks:
            _mode_locks[mode] = threading.Lock()
        return _mode_locks[mode]


def _run_coro_in_new_loop(coro_func, *args) -> None:
    """Run coro_func(*args) to completion on a brand-new event loop, scoped
    entirely to the CURRENT thread. Must only ever be invoked via
    `await asyncio.to_thread(_run_coro_in_new_loop, coro_func, *args)` from
    the main loop — see module docstring point 4. `asyncio.run` creates and
    tears down its own loop, so this is safe to call repeatedly from a
    fresh worker thread each time (which is exactly what asyncio.to_thread
    gives us — it does not reuse threads across calls in a way that would
    leave a stale loop lying around)."""
    asyncio.run(coro_func(*args))


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
    """Fast exit-only tick: exit_engine + reconcile, no candidates/entry.

    2026-09-10: the tick body itself (_exit_only_tick_body below) is
    unchanged from before this fix — every db.query/commit inside it is
    still plain synchronous SQLAlchemy. What changed is WHERE it runs: this
    thin wrapper hands the whole body off to a worker thread with its own
    event loop instead of running it directly on the shared main loop. See
    the module docstring's point 4 for the full reasoning and what was
    checked before making this change. Skip-if-busy semantics (this was
    always a "don't wait, just skip this tick" cadence, never a "wait for
    the lock" one) are preserved by _run_exit_tick_sync below.
    """
    await asyncio.to_thread(_run_exit_tick_sync, mode)


def _run_exit_tick_sync(mode: str) -> None:
    """Runs on a worker thread. Non-blocking lock attempt — mirrors the
    original `if lock.locked(): return` skip-if-busy behavior, but as a
    single atomic acquire(blocking=False) instead of a separate check-then-
    acquire (the original two-step form had a narrow TOCTOU gap between the
    peek and the `async with lock:` acquire; this closes it as a side
    effect of the rewrite, not a separately-scoped fix)."""
    lock = _get_lock(mode)
    if not lock.acquire(blocking=False):
        return  # full cycle (or schedule tick) is running — skip, it'll cover exits
    try:
        _run_coro_in_new_loop(_exit_only_tick_body, mode)
    finally:
        lock.release()


async def _exit_only_tick_body(mode: str) -> None:
    """The actual fast exit-only tick. Caller (_run_exit_tick_sync) already
    holds the per-mode lock — this function does no locking itself."""
    Session = get_session_factory()
    db = Session()
    try:
        gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
        if gate is None or not gate.armed or not getattr(gate, "auto_pilot_enabled", False):
            # BUG FIX (2026-09-01): direct attribute read on a
            # migration-added column (see main.py's /status/{mode} fix
            # for the same class of bug) — getattr keeps this safe on
            # first boot against an existing DB before the additive
            # migration in init_schema() has run.
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
    """Full cycle tick: candidates → entry → fills → expire → exit → reconcile.

    2026-09-10: same event-loop-isolation fix as _exit_only_tick — see the
    module docstring's point 4. Unlike the fast exit tick, this one always
    WAITS for the lock rather than skipping (the original code used
    `async with lock:`, which blocks until available, not a peek-and-skip).
    _run_full_tick_sync below preserves that — the wait now happens with a
    blocking threading.Lock.acquire() inside the worker thread, which is
    safe there (blocking a worker thread doesn't block the main loop),
    unlike blocking the main loop directly would be.
    """
    await asyncio.to_thread(_run_full_tick_sync, mode)


def _run_full_tick_sync(mode: str) -> None:
    """Runs on a worker thread. Blocking acquire — mirrors the original
    `async with lock:` wait-don't-skip behavior for the full cycle, with
    the wait itself now happening on this thread instead of the main loop."""
    lock = _get_lock(mode)
    with lock:
        _run_coro_in_new_loop(_full_tick_body, mode)


async def _full_tick_body(mode: str) -> None:
    """The actual full cycle tick. Caller (_run_full_tick_sync) already
    holds the per-mode lock — this function does no locking itself."""
    Session = get_session_factory()
    db = Session()
    try:
        gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
        if gate is None or not gate.armed or not getattr(gate, "auto_pilot_enabled", False):
            # BUG FIX (2026-09-01): direct attribute read on a
            # migration-added column (see main.py's /status/{mode} fix
            # for the same class of bug) — getattr keeps this safe on
            # first boot against an existing DB before the additive
            # migration in init_schema() has run.
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


# ══════════════════════════════════════════════════════════════════════════
# Scheduled automation (2026-08-31; env-gate removed 2026-09-01): 9am
# pre-pick, enter-at-open, EOD square-off.
#
# All three are DEFAULT OFF and gated by:
#   1. per-mode UI toggle  (gate.<feature>_enabled) — sole authority now;
#      there used to also be a process-level env kill-switch
#      (config.PREPICK_ENABLED etc.) but it's been removed so the dashboard
#      toggle alone gives full control, with no Render env var / redeploy
#      needed to activate a feature already switched on in the UI.
#   2. the mode must be armed
# and each fires AT MOST ONCE PER IST TRADING DAY, tracked by the gate's
# <feature>_last_run date column so a Render restart can't cause a re-fire.
# ══════════════════════════════════════════════════════════════════════════

async def _prepick(db, mode: str) -> None:
    """~09:00 pre-open: warm the candidate queue so the strongest names are
    ready to enter the instant the market opens. Deliberately does NOT run the
    entry evaluator (that would consume candidates while the market is shut) —
    it only refreshes/queues them; enter-at-open does the actual entering."""
    from candidate_engine.candidates import refresh_candidates
    n = await refresh_candidates(db, mode)
    logger.info("[schedule] pre-pick %s: refreshed %s candidates", mode, n)
    # ENRICHMENT (2026-09-02): previously just a bare count. Now lists the
    # actual queued symbols + signal price (top 10) so the Telegram alert
    # is useful on its own, matching the SELL-notification enrichment.
    lines = [f"🌅 *Pre-pick — {mode}*\nQueued {n} candidate(s) before the open."]
    if n:
        top = (
            db.query(models.TradeCandidate)
            .filter_by(mode=mode, consumed=False)
            .order_by(models.TradeCandidate.received_at.desc())
            .limit(10)
            .all()
        )
        for c in top:
            price_txt = f"₹{c.signal_price:.2f}" if c.signal_price is not None else "—"
            lines.append(f"  • {c.symbol} @ {price_txt}")
        if n > len(top):
            lines.append(f"  ...and {n - len(top)} more")
    lines.append("They'll be evaluated for entry when the market opens.")
    await notify_async("\n".join(lines))


async def _enter_at_open(db, mode: str, gate_armed: bool) -> None:
    """~09:20 just after open: run one full entry cycle so the pre-picked names
    get entered at the early price instead of waiting for the next auto-pilot
    tick (which could be minutes away)."""
    from cycle_runner import run_cycle_core
    result = await run_cycle_core(db, mode, gate_armed, trigger="enter_at_open")
    if result.get("auto_disarmed"):
        await notify_async(
            f"🔴 *Enter-at-open disarmed — {mode}*\n{result['auto_disarmed']}"
        )
        return
    entry = result.get("entry") or {}
    await notify_async(
        f"🚀 *Enter-at-open — {mode}*\nEntries sent: {entry.get('entered', 0)} "
        f"({entry.get('rejected', 0)} risk-rejected), waited: {entry.get('waited', 0)}."
    )


async def _eod_squareoff(db, mode: str) -> None:
    """~15:15 before the close: flatten every open position for this mode so
    nothing is carried overnight (intraday square-off). Reuses the exact manual-
    close paths: DEMO closes at the live tick; REAL sends a MARKET sell to Dhan."""
    from portfolio.portfolio import open_positions as _pf_open_positions, close_position as _pf_close_position
    positions = list(_pf_open_positions(db, mode))
    if not positions:
        logger.info("[schedule] EOD square-off %s: no open positions", mode)
        return
    closed, sent, failed = 0, 0, 0
    if mode == "DEMO":
        from market_feed.feed import get_quotes
        syms = list({p.symbol for p in positions})
        ticks = await get_quotes(syms)
        for p in positions:
            tick = ticks.get(p.symbol)
            if tick is None:
                failed += 1
                continue
            try:
                _pf_close_position(db, p, tick, p.qty_open, "eod_squareoff")
                closed += 1
            except Exception:
                logger.exception("[schedule] EOD close failed for %s %s", mode, p.symbol)
                failed += 1
    else:  # REAL
        from exit_engine.exit import _send_real_sell
        for p in positions:
            try:
                if _send_real_sell(db, p, p.qty_open, "eod_squareoff", full=True):
                    sent += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("[schedule] EOD real-sell failed for %s %s", mode, p.symbol)
                failed += 1
    logger.info("[schedule] EOD square-off %s: closed=%s sent=%s failed=%s", mode, closed, sent, failed)
    await notify_async(
        f"🌆 *EOD square-off — {mode}*\n"
        + (f"Closed {closed} position(s)." if mode == "DEMO" else f"Sent {sent} sell order(s) to Dhan.")
        + (f" {failed} could not be closed (no price / broker reject) — check manually." if failed else "")
    )


async def _schedule_tick(mode: str) -> None:
    """One pass of the time-trigger loop for a mode. Each feature is gated by
    its per-mode toggle + armed, fires once/day, and is time-of-day bound.

    2026-09-10: same event-loop-isolation fix as the other two ticks — see
    module docstring point 4. This one shares the same per-mode lock as
    _full_tick/_exit_only_tick (it can invoke _enter_at_open, which runs a
    full entry cycle, and _eod_squareoff, which sends real sell orders —
    both need the same mutual exclusion), and originally used the same
    skip-if-busy `if lock.locked(): return` pattern as the fast-exit tick,
    which _run_schedule_tick_sync below preserves as a single atomic
    acquire(blocking=False)."""
    if not is_ist_weekday():
        return
    await asyncio.to_thread(_run_schedule_tick_sync, mode)


def _run_schedule_tick_sync(mode: str) -> None:
    """Runs on a worker thread. Non-blocking lock attempt, same rationale
    as _run_exit_tick_sync."""
    lock = _get_lock(mode)
    if not lock.acquire(blocking=False):
        return  # a full/exit cycle is mid-flight — try again next check
    try:
        _run_coro_in_new_loop(_schedule_tick_body, mode)
    finally:
        lock.release()


async def _schedule_tick_body(mode: str) -> None:
    """The actual schedule tick. Caller (_run_schedule_tick_sync) already
    holds the per-mode lock — this function does no locking itself."""
    Session = get_session_factory()
    db = Session()
    try:
        gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
        if gate is None or not gate.armed:
            return
        today = ist_today_str()

        # ── Pre-pick (pre-open; market need not be open) ──────────────────
        if (
            getattr(gate, "prepick_enabled", False)
            and getattr(gate, "prepick_last_run", None) != today
            and ist_time_at_or_after(parse_hhmm(config.PREPICK_TIME_IST, 9, 0))
        ):
            gate.prepick_last_run = today
            db.commit()
            try:
                await _prepick(db, mode)
            except Exception:
                logger.exception("[schedule] pre-pick failed for %s", mode)
                await notify_async(f"⚠️ *Pre-pick error — {mode}* — see server logs.")

        # ── Enter-at-open (market must be open) ───────────────────────────
        if (
            getattr(gate, "enter_at_open_enabled", False)
            and getattr(gate, "enter_at_open_last_run", None) != today
            and is_market_open_ist()
            and ist_time_at_or_after(parse_hhmm(config.ENTER_AT_OPEN_TIME_IST, 9, 20))
        ):
            gate.enter_at_open_last_run = today
            db.commit()
            try:
                await _enter_at_open(db, mode, gate.armed)
            except Exception:
                logger.exception("[schedule] enter-at-open failed for %s", mode)
                await notify_async(f"⚠️ *Enter-at-open error — {mode}* — see server logs.")

        # ── EOD square-off (during hours, near the close) ─────────────────
        if (
            getattr(gate, "eod_squareoff_enabled", False)
            and getattr(gate, "eod_squareoff_last_run", None) != today
            and is_market_open_ist()
            and ist_time_at_or_after(parse_hhmm(config.EOD_SQUAREOFF_TIME_IST, 15, 15))
        ):
            gate.eod_squareoff_last_run = today
            db.commit()
            try:
                await _eod_squareoff(db, mode)
            except Exception:
                logger.exception("[schedule] EOD square-off failed for %s", mode)
                await notify_async(f"⚠️ *EOD square-off error — {mode}* — see server logs.")
    except Exception:
        logger.exception("[schedule] tick failed for %s", mode)
    finally:
        db.close()


async def _schedule_loop() -> None:
    """Time-of-day trigger loop for the three scheduled-automation features.
    Runs regardless of auto_pilot_enabled (these are their own toggles), but
    every action re-checks the gate/once-per-day guards at fire time."""
    await asyncio.sleep(_STARTUP_DELAY_SECONDS + 10)
    logger.info(
        "Auto-pilot SCHEDULE loop running (check=%ss); each feature's own "
        "per-mode dashboard toggle is the sole on/off authority now.",
        config.SCHEDULE_CHECK_INTERVAL_SECONDS,
    )
    while True:
        for mode in ("DEMO", "REAL"):
            try:
                await _schedule_tick(mode)
            except Exception:
                logger.exception("schedule loop: unexpected error for %s", mode)
        await asyncio.sleep(config.SCHEDULE_CHECK_INTERVAL_SECONDS)


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


async def _totp_refresh_loop() -> None:
    """Standalone proactive Dhan TOTP refresh loop (2026-09-01).

    Independent of market hours, auto_pilot_enabled, and armed state — it
    has to be, since its whole job is to keep a fresh token ready BEFORE
    the existing one expires, including on a quiet day with no cycles, or
    while REAL is sitting disarmed waiting for exactly this to happen.

    Each tick is a cheap DB read (token_needs_refresh) that only does real
    work — hitting Dhan for a new token — when a refresh is actually due;
    the whole loop is a no-op whenever DHAN_TOTP_ENABLED=false (the
    default), so it's safe to always start.

    On a successful refresh, refresh_if_totp_enabled() also restores
    gate.dhan_connected for REAL (see auth/dhan_credentials.py), which is
    what heals the disarmed + dhan_connected=False deadlock automatically —
    no dashboard visit required before /arm works again.
    """
    await asyncio.sleep(_STARTUP_DELAY_SECONDS + 15)
    logger.info(
        "Auto-pilot TOTP refresh loop running (check=%ss, margin=%sh, totp_enabled=%s)",
        config.DHAN_TOTP_REFRESH_CHECK_INTERVAL_SECONDS,
        config.DHAN_TOTP_REFRESH_MARGIN_HOURS,
        config.DHAN_TOTP_ENABLED,
    )
    while True:
        if config.DHAN_TOTP_ENABLED:
            Session = get_session_factory()
            db = Session()
            try:
                from auth.dhan_credentials import token_needs_refresh, refresh_if_totp_enabled
                if token_needs_refresh(db):
                    logger.info("Dhan token within refresh margin — refreshing proactively.")
                    ok = refresh_if_totp_enabled(db)
                    if not ok:
                        logger.warning("Proactive Dhan TOTP refresh attempt failed — will retry next check.")
            except Exception:
                logger.exception("TOTP refresh loop: unexpected error")
            finally:
                db.close()
        await asyncio.sleep(config.DHAN_TOTP_REFRESH_CHECK_INTERVAL_SECONDS)


def start() -> None:
    """Idempotent — safe to call from startup() even if hot-reloaded."""
    global _full_task, _exit_task, _schedule_task, _totp_task
    if _full_task is None or _full_task.done():
        _full_task = asyncio.create_task(_full_cycle_loop())
        logger.info("Auto-pilot FULL CYCLE background task created.")
    if _exit_task is None or _exit_task.done():
        _exit_task = asyncio.create_task(_fast_exit_loop())
        logger.info("Auto-pilot FAST EXIT background task created (interval=%ss).",
                    EXIT_CHECK_INTERVAL_SECONDS)
    # Scheduled-automation loop (pre-pick / enter-at-open / EOD square-off).
    # Always started; every action inside is per-mode-toggle + armed gated,
    # so with all three toggles OFF (the default) this loop wakes on its
    # interval, finds nothing enabled, and goes straight back to sleep.
    if _schedule_task is None or _schedule_task.done():
        _schedule_task = asyncio.create_task(_schedule_loop())
        logger.info("Auto-pilot SCHEDULE background task created (check=%ss).",
                    config.SCHEDULE_CHECK_INTERVAL_SECONDS)
    # Proactive Dhan TOTP refresh loop — always started, same posture as the
    # schedule loop above: harmless no-op while DHAN_TOTP_ENABLED=false, and
    # no main.py change needed since start() is already called unconditionally
    # at startup.
    if _totp_task is None or _totp_task.done():
        _totp_task = asyncio.create_task(_totp_refresh_loop())
        logger.info("Auto-pilot TOTP refresh background task created (check=%ss).",
                    config.DHAN_TOTP_REFRESH_CHECK_INTERVAL_SECONDS)
