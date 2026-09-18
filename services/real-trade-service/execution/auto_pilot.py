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
import json
import logging
import os
import threading
from datetime import datetime, timedelta, timezone
from typing import Optional

import config
import models
from db import get_session_factory
from notifier import notify_async
from tz_utils import (
    is_market_open_ist,
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
_afterhours_task: Optional[asyncio.Task] = None
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

# 2026-09-12 fix (audit finding — MEDICAPQ stop never fired): both tick
# bodies below silently `return` when the gate isn't armed or
# auto_pilot_enabled is False, WITHOUT sending an alert — so a position that
# genuinely needs exit_engine to run every cycle (a stop breach, a gap-down)
# can sit unattended indefinitely with no visible signal that autopilot
# stopped covering it, until someone happens to check the dashboard. This
# does not change when/whether autopilot runs (arming is never bypassed —
# see point 3 in the module docstring); it only makes the "gate is off AND
# there is open exposure that needed evaluating" state observable. Throttled
# per mode (same idea as exit.py's CDSL_ALERT_COOLDOWN_MIN) so a gate left
# off on purpose overnight/over a weekend doesn't spam Telegram every tick.
GATE_OFF_ALERT_COOLDOWN_MIN = int(os.getenv("AUTO_PILOT_GATE_OFF_ALERT_COOLDOWN_MIN", "30"))
_gate_off_alert_last_sent: dict = {}  # mode -> aware datetime of last alert


async def _alert_if_open_positions_while_gate_off(db, mode: str) -> None:
    """Best-effort, non-blocking. Called from the tick bodies' early-return
    branch (gate not armed / auto_pilot_enabled False) — checks whether this
    mode has open exposure that exit_engine is NOT currently evaluating as a
    result, and sends a throttled Telegram alert if so. Never raises —
    a failure here must never turn a skipped tick into a crashed one."""
    try:
        has_open = (
            db.query(models.TradePosition.id)
            .filter(
                models.TradePosition.mode == mode,
                models.TradePosition.status.in_(("OPEN", "PARTIALLY_CLOSED")),
            )
            .first()
            is not None
        )
        if not has_open:
            return
        now = datetime.now(timezone.utc)
        last = _gate_off_alert_last_sent.get(mode)
        if last is not None and (now - last) < timedelta(minutes=GATE_OFF_ALERT_COOLDOWN_MIN):
            return
        _gate_off_alert_last_sent[mode] = now
        # 2026-09-12 fix: DEMO's gate/auto_pilot can ONLY go off via an
        # explicit action — manual /disarm, /emergency-pause, or
        # /autopilot/DEMO/disable (see main.py's _check_and_expire_gates
        # and auth/dhan_credentials.py: the Dhan token-expiry / invalid-IP
        # auto-disarm paths are all hard-gated to mode == "REAL" and never
        # touch DEMO). "Re-authenticate" is a Dhan/REAL-only remedy and was
        # confusingly appearing in the DEMO alert too, where there is
        # nothing to re-authenticate. REAL keeps the original wording since
        # a Dhan token/session re-auth genuinely can be the fix there.
        re_enable_hint = (
            "Re-authenticate and re-arm" if mode == "REAL" else "Re-arm and re-enable Auto-Pilot"
        )
        await notify_async(
            f"⚠️ *Auto-Pilot not evaluating exits — {mode}*\n"
            f"Gate is disarmed or auto_pilot_enabled is off, but there are open "
            f"{mode} positions. Stops/targets will NOT be checked until this is "
            f"re-armed/re-enabled. {re_enable_hint}, or close positions "
            f"manually if this is unexpected."
        )
    except Exception:
        logger.exception("gate-off-with-open-positions alert failed for %s", mode)

import os as _os
# BUG FIX (session47): floor was 20s (default 45s) and, more importantly,
# the fast-exit tick shared _get_lock (below) with the full cycle — so
# whenever a full cycle's slow candidate-screening phase was running (user-
# reported 3-10 min, driven by quality_gate.py's sequential per-candidate
# fundamental/technical/event checks), the fast-exit tick's non-blocking
# acquire failed and it skipped ENTIRELY for the whole cycle duration, not
# just this one 45s tick. A stock could move well past its stop/target with
# nothing checking it for minutes — see _get_exit_lock below for the fix.
# Floor lowered to 5s and default to 8s per explicit request (stops/targets
# checked every 5-10s during market hours, independent of full-cycle timing).
EXIT_CHECK_INTERVAL_SECONDS = max(
    5, int(_os.getenv("EXIT_CHECK_INTERVAL_SECONDS", "8"))
)


def _get_lock(mode: str) -> threading.Lock:
    """Return (creating if needed) the per-mode ENTRY lock — guards
    candidate screening + entry (BUY) placement only. See _get_exit_lock
    below for the independent lock that now guards the exit side.

    BUG FIX (2026-09-10): guarded by _mode_locks_meta so two threads can
    never simultaneously insert different Lock objects for the same mode.
    """
    with _mode_locks_meta:
        if mode not in _mode_locks:
            _mode_locks[mode] = threading.Lock()
        return _mode_locks[mode]


# BUG FIX (session47 — "manual reconcile/close never work, adaptive exits
# never run during a cycle"): previously there was exactly ONE per-mode lock
# shared by literally everything — the full cycle's slow candidate-screening/
# entry phase, its own exit/reconcile phase, the fast-exit background tick,
# and every manual button (Close Position, Cancel Order, Reconcile, Holdings
# Sync). A full cycle legitimately takes 3-10 minutes (multiple candidates
# each going through quality_gate.py's sequential multi-second fundamental/
# technical/event checks before an entry decision) and held that ONE lock
# for its entire duration — so for that whole window: (1) the fast-exit tick
# silently skipped every single time (see EXIT_CHECK_INTERVAL_SECONDS above),
# meaning stop-loss/target were effectively NOT being checked independent of
# the slow cycle at all, defeating the whole point of an "adaptive" system;
# (2) every manual Close Position / Reconcile / Cancel / Holdings-Sync click
# got an immediate 409 "cycle already in progress" — exactly what the
# dashboard screenshot showed.
#
# Fix: split into two independent locks.
#   - _get_lock (entry_lock, above): candidates + entry (BUY) only — this is
#     the genuinely slow part, and manual BUY confirm still serializes
#     against it (a manual BUY racing the screening loop for the same
#     candidate is the real risk there).
#   - _get_exit_lock (below): exit evaluation (stop/target/time-stop/
#     trailing) + reconcile + manual close/cancel/reconcile/holdings-sync.
#     This critical section is fast (seconds: an order placement + a few DB
#     writes), so contention on it is brief even under concurrent access —
#     nothing here ever waits out a multi-minute screening phase again.
# cycle_runner.py's exit+reconcile stage now acquires _get_exit_lock only
# around that stage (not the whole cycle), so a full cycle's slow entry
# phase never blocks exits, and a full cycle's own exit stage still can't
# race a concurrent manual close/fast-exit-tick for the same position.
#
# This reopens a narrow, real race that the single lock used to close for
# free: entry_lock's BUY-side cash_available -= cost and exit_lock's SELL-
# side cash_available += proceeds can now commit on two different threads at
# the same instant (previously impossible — one lock serialized everything).
# Closed at the DB layer instead: every read-modify-write of cash_available
# in portfolio/portfolio.py now goes through get_account(..., for_update=True),
# which takes a SELECT...FOR UPDATE row lock (see that function's docstring)
# — correct regardless of which of these two Python-level locks either side
# holds, or whether they hold none at all.
_exit_mode_locks: dict = {}
_exit_mode_locks_meta: threading.Lock = threading.Lock()


def _get_exit_lock(mode: str) -> threading.Lock:
    """Return (creating if needed) the per-mode EXIT lock — guards exit
    evaluation (stop/target/time-stop checks + SELL placement), broker
    reconcile, and every manual exit-side action (close position, cancel
    order, reconcile, holdings-sync). Independent of _get_lock (entry lock)
    — see the module-level comment above this function for the full
    reasoning. Same lazy-init-race protection pattern as _get_lock."""
    with _exit_mode_locks_meta:
        if mode not in _exit_mode_locks:
            _exit_mode_locks[mode] = threading.Lock()
        return _exit_mode_locks[mode]


# BUG FIX (session47 follow-up — "Positions tab permanently shows 'exit-side
# operation already in progress'"): splitting the lock (above) assumed the
# exit-side critical section was uniformly brief ("seconds"). That's true
# for exit_evaluate() itself (checks live price against stop/target using
# data already in the DB/tick cache — no broker round trip needed to
# DECIDE), but reconcile_real_orders() is NOT brief: unconditionally, every
# single call, it does import_broker_holdings() + holdings_sync_reconcile()
# (both real Dhan API calls) plus, whenever there's a pending order,
# dhan_client.get_order_list() — 2-3 real network round trips, easily
# several seconds with a non-trivial order book (the reported account had
# 118 orders / 7 open positions). The fast-exit tick now calls
# reconcile_real_orders() every EXIT_CHECK_INTERVAL_SECONDS (5-10s) *and*
# main.py's self-heal path tries to call it on every single GET
# /positions|/orders poll — so the exit lock ended up busy almost
# continuously, starving the manual Reconcile/Close/Cancel buttons exactly
# as reported (they got 409 "already in progress" nearly every click).
#
# Fix: reconcile_real_orders() itself doesn't need to run on the same tight
# cadence as exit_evaluate() — fills don't need sub-10-second confirmation,
# only "prompt". Throttle it to at most once per
# REAL_RECONCILE_MIN_INTERVAL_SECONDS across ALL automatic callers (the fast
# tick and the self-heal path share this same throttle), while leaving
# exit_evaluate() itself running on the full fast cadence, and leaving the
# manual Reconcile button and every full cycle's own reconcile stage
# UNTHROTTLED (explicit user action / the authoritative periodic pass must
# always actually run) — they just also update the shared timestamp so nothing
# double-fires right after.
REAL_RECONCILE_MIN_INTERVAL_SECONDS = max(
    10, int(_os.getenv("REAL_RECONCILE_MIN_INTERVAL_SECONDS", "20"))
)
_last_real_reconcile_at: dict = {}  # mode -> aware datetime of last reconcile_real_orders() call (any caller)


def _reconcile_due(mode: str) -> bool:
    """True if an automatic caller (fast tick / self-heal) should run
    reconcile_real_orders() now. Manual/full-cycle callers should NOT gate
    on this — they always run, then call _mark_reconciled()."""
    last = _last_real_reconcile_at.get(mode)
    if last is None:
        return True
    return (datetime.now(timezone.utc) - last) >= timedelta(seconds=REAL_RECONCILE_MIN_INTERVAL_SECONDS)


def _mark_reconciled(mode: str) -> None:
    """Call after ANY caller (automatic or manual) actually runs
    reconcile_real_orders(), so the throttle above reflects reality
    regardless of who triggered it."""
    _last_real_reconcile_at[mode] = datetime.now(timezone.utc)


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
    effect of the rewrite, not a separately-scoped fix).

    BUG FIX (session47): now acquires the EXIT lock (_get_exit_lock), not
    the entry lock (_get_lock) — see _get_exit_lock's docstring. Previously
    this used the SAME lock as the full cycle's entire (3-10 min) candidate-
    screening + entry phase, so this tick skipped for the full duration of
    every full cycle, not just when an exit/reconcile was actually in
    flight. The exit lock is only ever held briefly (an order placement + a
    few DB writes), so skips here are now rare and short-lived."""
    lock = _get_exit_lock(mode)
    if not lock.acquire(blocking=False):
        return  # another exit-side operation (reconcile, manual close, or the full cycle's own exit stage) is running — skip, next tick is 5-10s away
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
            # 2026-09-12 fix (audit finding — MEDICAPQ stop never fired):
            # this early-return used to be completely silent even when
            # positions were open and needed evaluating. See
            # _alert_if_open_positions_while_gate_off's docstring above.
            await _alert_if_open_positions_while_gate_off(db, mode)
            return
        if not is_market_open_ist():
            return
        from exit_engine.exit import evaluate_mode as exit_evaluate
        exit_result = await exit_evaluate(db, mode)
        if mode == "REAL" and _reconcile_due(mode):
            from execution.reconcile import reconcile_real_orders
            await reconcile_real_orders(db)
            _mark_reconciled(mode)
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
            # 2026-09-12 fix (audit finding — MEDICAPQ stop never fired):
            # same silent-early-return gap as _exit_only_tick_body above —
            # the full-cycle tick early-returns here too, so it needs the
            # same alert.
            await _alert_if_open_positions_while_gate_off(db, mode)
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

_OVERNIGHT_SNAPSHOT_KEY_PREFIX = "overnight_priority"


async def _prepick(db, mode: str) -> None:
    """~09:00 pre-open: warm the candidate queue so the strongest names are
    ready to enter the instant the market opens. Deliberately does NOT run the
    entry evaluator (that would consume candidates while the market is shut) —
    it only refreshes/queues them; enter-at-open does the actual entering.

    2026-09-10 (session22, user request): also re-queues yesterday's EOD
    signal-scan picks (see _eod_signal_scan below) as fresh, high-priority
    TradeCandidate rows so they get bought first thing at today's open
    instead of waiting to be rediscovered by the normal intraday scan."""
    from candidate_engine.candidates import refresh_candidates
    n = await refresh_candidates(db, mode)
    logger.info("[schedule] pre-pick %s: refreshed %s candidates", mode, n)

    overnight_added = await _requeue_overnight_priority_candidates(db, mode)

    # 2026-09-17 (session56, user request): also inject any NextDayWatchlistEntry
    # rows for today's trading date that cleared the minimum score bar and haven't
    # been consumed yet. These come from the after-hours RSS news scan
    # (_afterhours_scan_loop below). Best-effort — a failure here never blocks
    # the normal pre-pick.
    news_injected = await _inject_nextday_watchlist_candidates(db, mode)
    overnight_added += news_injected

    # 2026-09-11 (session23, user request): apply the overnight US-sector
    # signal to every still-unconsumed candidate for this mode (this
    # morning's fresh ones plus anything just re-queued above). Best-effort
    # and entirely additive — see market_context/sector_signal.py's
    # docstring. Off by default (config.US_SECTOR_SIGNAL_ENABLED).
    if config.US_SECTOR_SIGNAL_ENABLED:
        try:
            from market_context.sector_signal import (
                refresh_us_sector_snapshot, sector_bonus_for_symbol,
            )
            sector_returns = refresh_us_sector_snapshot(db)
            if sector_returns:
                pending = (
                    db.query(models.TradeCandidate)
                    .filter_by(mode=mode, consumed=False)
                    .all()
                )
                for c in pending:
                    c.us_sector_bonus = sector_bonus_for_symbol(c.symbol, sector_returns)
                db.commit()
                logger.info(
                    "[schedule] pre-pick %s: applied US sector signal to %d candidate(s)",
                    mode, len(pending),
                )
        except Exception:
            logger.exception("[schedule] pre-pick %s: US sector signal failed (non-fatal)", mode)

    # ENRICHMENT (2026-09-02): previously just a bare count. Now lists the
    # actual queued symbols + signal price (top 10) so the Telegram alert
    # is useful on its own, matching the SELL-notification enrichment.
    total = n + overnight_added
    lines = [f"🌅 *Pre-pick — {mode}*\nQueued {total} candidate(s) before the open."]
    if overnight_added:
        lines.append(f"  🌙 {overnight_added} carried over from last evening's EOD signal scan (priority).")
    if total:
        top = (
            db.query(models.TradeCandidate)
            .filter_by(mode=mode, consumed=False)
            .order_by(models.TradeCandidate.overnight_priority.desc(), models.TradeCandidate.received_at.desc())
            .limit(10)
            .all()
        )
        for c in top:
            price_txt = f"₹{c.signal_price:.2f}" if c.signal_price is not None else "—"
            tag = " 🌙" if getattr(c, "overnight_priority", False) else ""
            lines.append(f"  • {c.symbol} @ {price_txt}{tag}")
        if total > len(top):
            lines.append(f"  ...and {total - len(top)} more")
    lines.append("They'll be evaluated for entry when the market opens.")
    await notify_async("\n".join(lines))


async def _requeue_overnight_priority_candidates(db, mode: str) -> int:
    """Reads the snapshot _eod_signal_scan saved last evening (if any, and
    if not already consumed / not stale) and inserts a fresh TradeCandidate
    row per symbol, flagged overnight_priority=True. Returns how many were
    added. Never raises — a missing/corrupt/stale snapshot is just treated
    as "nothing to carry over", same posture as every other resilience-cache
    read in this codebase (best-effort, never blocks the real pre-pick)."""
    from resilience.local_cache import load_snapshot, save_snapshot

    key = f"{_OVERNIGHT_SNAPSHOT_KEY_PREFIX}:{mode}"
    try:
        snap = load_snapshot(db, key)
    except Exception:
        logger.exception("[schedule] pre-pick %s: overnight-priority snapshot read failed", mode)
        return 0
    if not snap:
        return 0

    today = ist_today_str()
    if snap.get("consumed"):
        return 0
    # Only carry over a snapshot from a PRIOR trading day — never today's own
    # (defends against a same-day double pre-pick somehow re-reading it, and
    # against an old, never-cleared snapshot with no trading_date at all).
    trading_date = snap.get("trading_date")
    if not trading_date or trading_date >= today:
        return 0

    picks = snap.get("candidates") or []
    if not picks:
        return 0

    already_queued = {
        c.symbol for c in
        db.query(models.TradeCandidate).filter_by(mode=mode, consumed=False).all()
    }

    added = 0
    for p in picks:
        symbol = p.get("symbol")
        if not symbol or symbol in already_queued:
            continue
        db.add(models.TradeCandidate(
            mode=mode,
            symbol=symbol,
            source_tab="eod_signal_scan",
            decision_label=p.get("decision_label"),
            conviction_score=p.get("conviction_score"),
            signal_price=p.get("signal_price"),
            raw_payload=p.get("raw_payload"),
            overnight_priority=True,
        ))
        already_queued.add(symbol)
        added += 1

    if added:
        db.commit()
        logger.info(
            "[schedule] pre-pick %s: re-queued %d overnight-priority candidate(s) "
            "from %s's EOD signal scan", mode, added, trading_date,
        )
    # Mark the snapshot consumed either way (even 0-added, e.g. every symbol
    # was already open/queued) so a later pre-pick run today can't re-apply
    # it a second time.
    try:
        snap["consumed"] = True
        save_snapshot(db, key, snap)
    except Exception:
        logger.exception("[schedule] pre-pick %s: failed to mark overnight-priority snapshot consumed", mode)
    return added


async def _inject_nextday_watchlist_candidates(db, mode: str) -> int:
    """At pre-pick time: read today's NextDayWatchlistEntry rows (from the
    after-hours RSS news scan), inject each as an overnight-priority
    TradeCandidate (if it cleared config.AFTERHOURS_SCAN_MIN_INJECT_SCORE),
    and mark the row consumed. Returns count injected. Never raises."""
    try:
        today = ist_today_str()
        rows = (
            db.query(models.NextDayWatchlistEntry)
            .filter_by(mode=mode, market_date=today, consumed=False)
            .all()
        )
        if not rows:
            return 0
        already_queued = {
            c.symbol for c in
            db.query(models.TradeCandidate).filter_by(mode=mode, consumed=False).all()
        }

        # 2026-09-17 fix (session56 audit): best-effort preview-price lookup so
        # injected candidates get a real signal_price instead of None. Without
        # this, entry_engine/entry.py's _entry_drift_ok() treats a None
        # signal_price as "no drift to check" and skips the anti-chasing gate
        # entirely — a news-sourced candidate could then be bought however far
        # the price has already run since the headline broke, with zero band
        # protection. get_preview_quotes() is explicitly documented as
        # non-tradeable/display-only (never used to size or place an order),
        # which is exactly the right posture here too: we only want it as a
        # drift-gate reference point, not to size the trade.
        injectable_symbols = [
            row.symbol for row in rows
            if row.priority_score >= config.AFTERHOURS_SCAN_MIN_INJECT_SCORE
            and row.symbol not in already_queued
        ]
        preview_prices: dict = {}
        if injectable_symbols:
            try:
                from market_feed.feed import get_preview_quotes
                previews = await get_preview_quotes(injectable_symbols)
                preview_prices = {sym: t.price for sym, t in previews.items() if t and t.price}
            except Exception:
                logger.exception(
                    "[schedule] pre-pick %s: preview-price lookup for after-hours candidates "
                    "failed (non-fatal — signal_price will stay None for these)", mode,
                )

        now = datetime.now(timezone.utc)
        injected = 0
        for row in rows:
            # Rows that don't meet the bar are still marked consumed so they
            # don't re-appear on a hypothetical second pre-pick run today.
            if row.priority_score < config.AFTERHOURS_SCAN_MIN_INJECT_SCORE:
                row.consumed = True
                row.consumed_at = now
                db.commit()
                continue
            if row.symbol in already_queued:
                row.consumed = True
                row.consumed_at = now
                db.commit()
                continue
            # 2026-09-17 fix (session56 audit): commit PER ROW instead of once
            # after the whole loop. Previously a single bad symbol's flush
            # error triggered db.rollback(), which — because nothing earlier
            # in the loop had been committed yet — silently wiped out every
            # already-processed symbol from this same pre-pick tick, not just
            # the failing one. Committing immediately after each success means
            # a later failure can only ever roll back its own row.
            try:
                db.add(models.TradeCandidate(
                    mode=mode,
                    symbol=row.symbol,
                    source_tab="afterhours_news_scan",
                    decision_label="BUY NOW",
                    conviction_score=row.priority_score,
                    signal_price=preview_prices.get(row.symbol),
                    raw_payload=json.dumps({
                        "catalyst_type": row.catalyst_type,
                        "source": row.catalyst_source,
                        "headline": row.headline,
                    }),
                    overnight_priority=True,
                ))
                row.consumed = True
                row.consumed_at = now
                db.commit()
                already_queued.add(row.symbol)
                injected += 1
            except Exception as add_err:
                db.rollback()
                logger.warning(
                    "[schedule] pre-pick %s: could not inject NextDayWatchlist candidate %s — skipping: %s",
                    mode, row.symbol, add_err,
                )
        if injected:
            logger.info(
                "[schedule] pre-pick %s: injected %d after-hours news candidate(s) "
                "from NextDayWatchlistEntry (market_date=%s)", mode, injected, today,
            )
        return injected
    except Exception:
        db.rollback()
        logger.exception(
            "[schedule] pre-pick %s: NextDayWatchlistEntry injection failed (non-fatal)", mode
        )
        return 0


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


def _overnight_hold_enabled(db, mode: str) -> bool:
    """2026-09-18 fix (user report: "no new toggle shows" for selective
    overnight holding). Reads the new TradeGateState.overnight_hold_enabled
    switch — same dashboard-toggle mechanism (POST /features/{mode}) as
    prepick/enter_at_open/eod_squareoff/eod_signal_scan, wired via
    main.py's _FEATURE_COLUMNS. Column defaults to True (see its model
    docstring) so a gate row that predates this migration, or one no admin
    has touched yet, behaves exactly as config.OVERNIGHT_HOLD_ENABLED
    already did — zero behavior change until an admin explicitly flips the
    new toggle off. Fails open to the config default if the gate row is
    somehow missing (should not happen in practice — _eod_squareoff already
    requires a gate row to run at all)."""
    try:
        gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
    except Exception:
        gate = None
    if gate is not None:
        return bool(getattr(gate, "overnight_hold_enabled", True))
    return config.OVERNIGHT_HOLD_ENABLED


async def _select_overnight_holds(db, mode: str, positions: list) -> tuple[set, dict]:
    """2026-09-18 fix (user audit finding): _eod_squareoff used to flatten
    EVERY open position unconditionally, discarding candidate_engine's own
    backtested Day+1 continuation signal (high_conviction/upper_circuit
    tiers, time_stop_hint="EOD+1") every single day before it ever had a
    chance to play out. This picks a narrow, capped subset of CURRENTLY
    OPEN positions that are allowed to skip today's square-off, using data
    the pipeline already computed at entry time (never anything guessed
    fresh here).

    Eligibility (ALL must hold — fail-closed on missing data):
      1. entry_decision_label in config.OVERNIGHT_HOLD_ELIGIBLE_LABELS
         (default: only the two tiers with a real backtested win rate —
         UPPER_CIRCUIT 69.7%, HIGH_CONVICTION 55.7% — explicitly excludes
         the base VOLUME_SHOCK tier at 48.1%/+0.66%, too thin an edge to
         justify overnight gap risk).
      2. Currently at/above breakeven (config.OVERNIGHT_HOLD_REQUIRE_PROFITABLE).
      3. Not already extended near today's high (range_pos below
         config.OVERNIGHT_HOLD_MAX_RANGE_POS) — mirrors entry_engine's own
         near-high exhaustion logic. Requires a live tick with day_high/
         day_low; missing range data means NOT eligible (fail-closed).
      4. Aggregate value of everything kept stays within
         config.OVERNIGHT_HOLD_MAX_EXPOSURE_PCT of equity — ranked by
         entry_conviction_score, highest first, until the cap is hit.

    Returns (set of position ids to KEEP OPEN, dict[position.id -> reason
    string] for logging/notification). Any position not returned in the
    keep-set squares off exactly as before this fix.
    """
    if not _overnight_hold_enabled(db, mode) or not positions:
        return set(), {}

    eligible_labels = config.OVERNIGHT_HOLD_ELIGIBLE_LABELS
    candidates = [
        p for p in positions
        if (getattr(p, "entry_decision_label", None) or "").upper() in eligible_labels
    ]
    if not candidates:
        return set(), {}

    from market_feed.feed import get_quotes
    from portfolio.portfolio import get_account as _pf_get_account
    syms = list({p.symbol for p in candidates})
    ticks = await get_quotes(syms)

    scored: list[tuple[float, object, float]] = []  # (conviction, position, position_value)
    reasons: dict = {}
    for p in candidates:
        tick = ticks.get(p.symbol)
        if tick is None or not tick.price:
            continue  # fail-closed: no live price to verify against
        ltp = float(tick.price)

        if config.OVERNIGHT_HOLD_REQUIRE_PROFITABLE and ltp < (p.avg_entry_price or 0):
            continue

        dh = getattr(tick, "day_high", None)
        dl = getattr(tick, "day_low", None)
        if not dh or not dl or (dh - dl) <= 1e-6:
            continue  # fail-closed: can't verify range position
        range_pos = max(0.0, min(1.0, (ltp - dl) / (dh - dl)))
        if range_pos >= config.OVERNIGHT_HOLD_MAX_RANGE_POS:
            continue  # already extended near today's high — not the exhausted-breakout case

        conviction = float(getattr(p, "entry_conviction_score", None) or 0.0)
        position_value = ltp * p.qty_open
        scored.append((conviction, p, position_value))
        reasons[p.id] = (
            f"overnight hold: {p.entry_decision_label} (conviction {conviction:.0f}), "
            f"+{((ltp / p.avg_entry_price) - 1) * 100:.1f}% unrealized, "
            f"range_pos {range_pos:.2f} < {config.OVERNIGHT_HOLD_MAX_RANGE_POS:.2f}"
        )

    if not scored:
        return set(), {}

    scored.sort(key=lambda t: t[0], reverse=True)

    account = _pf_get_account(db, mode)
    equity = float(account.current_equity or 0.0)
    cap = equity * (config.OVERNIGHT_HOLD_MAX_EXPOSURE_PCT / 100.0)

    # Only candidates that passed the profitability/range checks above ever
    # compete for the cap — everything else squares off regardless, so it
    # doesn't count against overnight exposure.
    keep_ids: set = set()
    running_value = 0.0
    for conviction, p, position_value in scored:
        if equity > 0 and (running_value + position_value) > cap:
            reasons.pop(p.id, None)
            continue
        keep_ids.add(p.id)
        running_value += position_value

    return keep_ids, {k: v for k, v in reasons.items() if k in keep_ids}


async def _eod_squareoff(db, mode: str) -> None:
    """~15:00 before the close (moved from 15:15 on 2026-09-10, session22 —
    see config.EOD_SQUAREOFF_TIME_IST): flatten every open position for this
    mode so nothing is carried overnight (intraday square-off) — EXCEPT a
    narrow, capped subset selected by _select_overnight_holds (2026-09-18
    fix) for positions the pipeline already has real backtested evidence
    for. Reuses the exact manual-close paths: DEMO closes at the live tick;
    REAL sends a MARKET sell to Dhan."""
    from portfolio.portfolio import open_positions as _pf_open_positions, close_position as _pf_close_position
    positions = list(_pf_open_positions(db, mode))
    if not positions:
        logger.info("[schedule] EOD square-off %s: no open positions", mode)
        return

    hold_ids, hold_reasons = await _select_overnight_holds(db, mode, positions)
    if hold_ids:
        # 2026-09-18 fix (follow-on item #6): this reason used to only ever
        # be logged (logger.info) — never persisted anywhere queryable, so
        # the dashboard had no way to show *why* a position skipped
        # square-off. Now stamped onto the row itself (models.py
        # TradePosition.overnight_hold_reason, additive migration) so
        # GET /positions/{mode} can surface it as a badge.
        by_id = {p.id: p for p in positions}
        for pid, reason in hold_reasons.items():
            logger.info("[schedule] EOD square-off %s: HOLDING position %s overnight — %s", mode, pid, reason)
            pos = by_id.get(pid)
            if pos is not None:
                pos.overnight_hold_reason = reason
                db.add(models.TradePositionEvent(
                    position_id=pos.id, event_type="OVERNIGHT_HOLD", detail=reason,
                ))
        db.commit()
    positions = [p for p in positions if p.id not in hold_ids]

    closed, sent, failed, skipped = 0, 0, 0, 0
    if not positions:
        pass
    elif mode == "DEMO":
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
        from exit_engine.exit import _send_real_sell, _has_pending_real_sell
        # BUG FIX (2026-09-10, session21 audit): missing duplicate-send guard
        # — a sequential double-sell race, not a threading one (this loop
        # runs single-threaded under the schedule tick's own per-mode lock,
        # so no other tick can interleave with it).
        #
        # record_real_exit_sent's own docstring (portfolio.py) is explicit
        # that a PARTIAL exit (full=False — e.g. a target-hit partial fired
        # by the fast-exit or full-cycle tick shortly before close) leaves
        # the position status at OPEN/PARTIALLY_CLOSED on purpose, so the
        # remainder stays live for further evaluation; only the in-flight
        # TradeOrder row (status PLACED/PARTIAL) tracks that a SELL is
        # already working at the broker, and exit_engine's own
        # evaluate_mode() never sends a second SELL without checking
        # _has_pending_real_sell() first. p.qty_open is likewise only ever
        # decremented once reconcile_real_orders() confirms the fill against
        # Dhan's own trade book — never at send time — so a position with a
        # partial SELL still in flight is returned by open_positions() with
        # its PRE-partial-exit qty_open intact.
        #
        # This loop called _send_real_sell directly, with no equivalent
        # check: if that partial SELL was still awaiting broker confirmation
        # when EOD square-off ran, this would send a SECOND MARKET SELL for
        # the position's full (stale) qty_open while the first partial SELL
        # was still working — two live SELL orders outstanding for
        # overlapping shares at once (an oversell that can get one order
        # broker-rejected, or — worse — filled short). Skipping a position
        # here is safe: the pending SELL is already flattening it, and the
        # next fast-exit tick (or reconcile) picks up wherever it lands.
        for p in positions:
            if _has_pending_real_sell(db, p.symbol):
                logger.info(
                    "[schedule] EOD square-off %s: skipping %s — a SELL is "
                    "already placed and awaiting broker fill confirmation",
                    mode, p.symbol,
                )
                skipped += 1
                continue
            try:
                if _send_real_sell(db, p, p.qty_open, "eod_squareoff", full=True):
                    sent += 1
                else:
                    failed += 1
            except Exception:
                logger.exception("[schedule] EOD real-sell failed for %s %s", mode, p.symbol)
                failed += 1
    held = len(hold_ids)
    logger.info(
        "[schedule] EOD square-off %s: closed=%s sent=%s failed=%s skipped=%s held_overnight=%s",
        mode, closed, sent, failed, skipped, held,
    )
    await notify_async(
        f"🌆 *EOD square-off — {mode}*\n"
        + (f"Closed {closed} position(s)." if mode == "DEMO" else f"Sent {sent} sell order(s) to Dhan.")
        + (f" {failed} could not be closed (no price / broker reject) — check manually." if failed else "")
        + (f" {skipped} skipped — SELL already pending broker confirmation (will settle on its own)." if skipped else "")
        + (f" 🌙 {held} held overnight (high-conviction, in-profit, not near-high — see logs)." if held else "")
    )


_EOD_SCAN_POSITIVE_LABELS = {
    "BUY NOW", "PREPARE TO BUY",
    "VOLUME_SHOCK", "VOLUME_SHOCK_HIGH_CONVICTION", "VOLUME_SHOCK_UPPER_CIRCUIT",
}


async def _eod_signal_scan(db, mode: str, gate_armed: bool) -> None:
    """~15:05, right after EOD square-off (user request, 2026-09-10 session22):
    "at market close, pick some stocks based on end-of-day signals (positive
    news/results/events/momentum into the close) so we can buy immediately at
    tomorrow's open instead of waiting to rediscover them."

    Re-scans the normal source feeds one more time (catching anything —
    results, board outcomes, bulk deals — that only landed late in the day)
    via the same refresh_candidates() every other cycle uses, keeps the top
    config.EOD_SIGNAL_SCAN_MAX_CANDIDATES by conviction among candidates with
    a genuinely positive decision label.

    2026-09-11 (session23, user request): "if it looks good, place the order
    that day — better than the next morning's open, which is more volatile
    and we might miss it." Two tiers within that pick list:
      - conviction >= config.EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION (top
        config.EOD_SIGNAL_SCAN_ENTRY_MAX_CANDIDATES of those): handed to the
        normal entry pipeline (entry_engine.entry.evaluate_mode) for a
        same-day fill attempt, RIGHT NOW — every existing gate (extension/
        drift caps, risk_engine, regime gate, cash) still applies in full,
        nothing is bypassed. If gate_armed is False, or a candidate is
        picked but doesn't actually fill (gate-rejected, out of cash, etc.),
        it automatically falls back into the overnight-priority queue below
        instead of being lost.
      - everything else in the pick list: saved to the resilience cache for
        tomorrow's _prepick to re-queue as overnight_priority candidates
        (see _requeue_overnight_priority_candidates above and
        config.ENTRY_OVERNIGHT_PRIORITY_BONUS in entry.py), exactly as
        before — still places NO order today, since minutes before the
        15:30 close is never worth entering for these lower-conviction
        picks (no time for the setup to work)."""
    from candidate_engine.candidates import refresh_candidates
    from resilience.local_cache import save_snapshot

    await refresh_candidates(db, mode)

    pool = (
        db.query(models.TradeCandidate)
        .filter_by(mode=mode, consumed=False)
        .all()
    )
    # Sort in Python (desc, None treated as lowest) — avoids relying on
    # NULLS LAST ordering syntax, which SQLAlchemy renders differently
    # across the oracledb/psycopg2 dialects this service supports.
    pool.sort(key=lambda c: (c.conviction_score if c.conviction_score is not None else -1), reverse=True)
    picked = []
    for c in pool:
        label = (c.decision_label or "").upper()
        conviction = c.conviction_score or 0
        if label not in _EOD_SCAN_POSITIVE_LABELS:
            continue
        if conviction < config.EOD_SIGNAL_SCAN_MIN_CONVICTION:
            continue
        picked.append(c)
        if len(picked) >= config.EOD_SIGNAL_SCAN_MAX_CANDIDATES:
            break

    # picked is already sorted by conviction desc (inherited from pool) —
    # take the top N that clear the stricter same-day-entry bar.
    same_day_entry = [
        c for c in picked
        if (c.conviction_score or 0) >= config.EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION
    ][: config.EOD_SIGNAL_SCAN_ENTRY_MAX_CANDIDATES]
    same_day_ids = {c.id for c in same_day_entry}
    queue_only = [c for c in picked if c.id not in same_day_ids]

    # Everything except same_day_entry is done for today — consume it now
    # with a logged decision (same posture as before this change), rather
    # than leaving it unconsumed for a normal cycle to silently WAIT (and
    # mis-attribute) minutes from now. same_day_entry stays UNCONSUMED here
    # on purpose so entry_evaluate (below) picks it up.
    for c in pool:
        if c.id in same_day_ids:
            continue
        c.consumed = True
        db.add(models.TradeDecision(
            mode=mode, candidate_id=c.id, symbol=c.symbol, decision_type="ENTRY",
            action="WAIT",
            reasoning=(
                "EOD signal scan: market is closing, not evaluated for entry today."
                + (" Selected as an overnight-priority pick for tomorrow's open."
                   if c in queue_only else " Not selected as an overnight-priority pick.")
            ),
        ))
    db.commit()

    entered_symbols: set[str] = set()
    if same_day_entry and gate_armed:
        from entry_engine.entry import evaluate_mode as entry_evaluate
        try:
            entry_result = await entry_evaluate(db, mode, gate_armed)
            entered_symbols = {
                d.get("symbol") for d in entry_result.get("entry_details", [])
                if d.get("action") == "ENTER"
            }
        except Exception:
            logger.exception(
                "[schedule] EOD signal scan %s: same-day entry evaluation failed "
                "(candidates fall back to overnight-priority queue)", mode,
            )
    elif same_day_entry and not gate_armed:
        logger.info(
            "[schedule] EOD signal scan %s: %d high-conviction pick(s) skipped "
            "same-day entry — mode disarmed, falling back to overnight-priority queue",
            mode, len(same_day_entry),
        )

    # Anything in same_day_entry that didn't actually fill (disarmed, gate
    # rejection, out of cash, exception above) falls back into the normal
    # overnight-priority queue instead of being lost, and gets explicitly
    # consumed here if entry_evaluate didn't already do so.
    fallback_queue = list(queue_only)
    for c in same_day_entry:
        if c.symbol not in entered_symbols:
            if not c.consumed:
                c.consumed = True
                db.add(models.TradeDecision(
                    mode=mode, candidate_id=c.id, symbol=c.symbol, decision_type="ENTRY",
                    action="WAIT",
                    reasoning=(
                        "EOD signal scan: qualified for same-day entry "
                        f"(conviction {c.conviction_score} >= "
                        f"{config.EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION:.0f}) but did not "
                        "fill today — carried over as an overnight-priority pick for "
                        "tomorrow's open instead."
                    ),
                ))
            fallback_queue.append(c)
    db.commit()

    key = f"{_OVERNIGHT_SNAPSHOT_KEY_PREFIX}:{mode}"
    save_snapshot(db, key, {
        "trading_date": ist_today_str(),
        "consumed": False,
        "candidates": [
            {
                "symbol": c.symbol,
                "decision_label": c.decision_label,
                "conviction_score": c.conviction_score,
                "signal_price": c.signal_price,
                "raw_payload": c.raw_payload,
            }
            for c in fallback_queue
        ],
    })

    logger.info(
        "[schedule] EOD signal scan %s: %d candidate(s) scanned, %d selected "
        "(%d same-day entry attempted, %d entered, %d queued/carried for tomorrow)",
        mode, len(pool), len(picked), len(same_day_entry), len(entered_symbols), len(fallback_queue),
    )
    lines = [f"🌙 *EOD signal scan — {mode}*"]
    if same_day_entry:
        lines.append(
            f"⚡ {len(same_day_entry)} high-conviction pick(s) "
            f"(≥{config.EOD_SIGNAL_SCAN_ENTRY_MIN_CONVICTION:.0f}) evaluated for same-day entry:"
        )
        for c in same_day_entry:
            price_txt = f"₹{c.signal_price:.2f}" if c.signal_price is not None else "—"
            tag = "✅ entered today" if c.symbol in entered_symbols else "⏭ didn't fill — carried to tomorrow"
            lines.append(f"  • {c.symbol} @ {price_txt} (conviction {c.conviction_score}) — {tag}")
    if queue_only:
        lines.append(f"Queued {len(queue_only)} more overnight-priority pick(s) for tomorrow's open:")
        for c in queue_only:
            price_txt = f"₹{c.signal_price:.2f}" if c.signal_price is not None else "—"
            lines.append(f"  • {c.symbol} @ {price_txt} ({c.decision_label}, conviction {c.conviction_score})")
    if not picked:
        lines.append("No candidate cleared the overnight-priority bar today — nothing queued.")
    await notify_async("\n".join(lines))


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
            and ist_time_at_or_after(parse_hhmm(config.EOD_SQUAREOFF_TIME_IST, 15, 0))
        ):
            gate.eod_squareoff_last_run = today
            db.commit()
            try:
                # BUG FIX (session47): _eod_squareoff sends real exit orders
                # (_send_real_sell / close_position) — exactly the kind of
                # exit-side action that now needs the dedicated exit lock,
                # not just whatever lock this schedule tick happens to be
                # running under (the entry lock — see _run_schedule_tick_sync).
                # Before the entry/exit lock split, one shared lock covered
                # this automatically; after the split, without this, a
                # concurrent manual Close Position / fast-exit tick / a full
                # cycle's own exit stage (all now exit-lock-only) could send
                # a duplicate SELL for the same position at the same instant
                # this does. Acquired in the same fixed order used
                # everywhere else (entry already held, exit taken second) so
                # this can never deadlock against cycle_runner.py's exit
                # stage or the manual cancel-order route.
                exit_lock = _get_exit_lock(mode)
                exit_lock.acquire()
                try:
                    await _eod_squareoff(db, mode)
                finally:
                    exit_lock.release()
            except Exception:
                logger.exception("[schedule] EOD square-off failed for %s", mode)
                await notify_async(f"⚠️ *EOD square-off error — {mode}* — see server logs.")

        # ── EOD signal scan (during hours, right after square-off) ────────
        # 2026-09-10 (session22, user request). Independent toggle from
        # square-off on purpose — an admin may want positions flattened
        # without the extra source-feed re-scan, or vice versa.
        if (
            getattr(gate, "eod_signal_scan_enabled", False)
            and getattr(gate, "eod_signal_scan_last_run", None) != today
            and is_market_open_ist()
            and ist_time_at_or_after(parse_hhmm(config.EOD_SIGNAL_SCAN_TIME_IST, 15, 5))
        ):
            gate.eod_signal_scan_last_run = today
            db.commit()
            try:
                await _eod_signal_scan(db, mode, gate.armed)
            except Exception:
                logger.exception("[schedule] EOD signal scan failed for %s", mode)
                await notify_async(f"⚠️ *EOD signal scan error — {mode}* — see server logs.")
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


# ══════════════════════════════════════════════════════════════════════════
# After-hours news scan loop (2026-09-17, session56)
#
# Runs 24/7 (started unconditionally at boot like _totp_refresh_loop), but
# only does real work during the after-hours window defined by
# config.AFTERHOURS_SCAN_START_IST → config.AFTERHOURS_SCAN_END_IST, and
# only when gate.afterhours_news_scan_enabled is True for that mode. The
# finalize pass fires once per day at AFTERHOURS_FINALIZE_TIME_IST.
# ══════════════════════════════════════════════════════════════════════════

def _is_afterhours_window_active() -> bool:
    """True if current IST time is in the after-hours scan window
    (15:45–08:45 next morning). The window spans midnight, so we check:
    time >= START (15:45) OR time < END (08:45)."""
    from tz_utils import ist_now, parse_hhmm
    now_t = ist_now().time()
    start_t = parse_hhmm(config.AFTERHOURS_SCAN_START_IST, 15, 45)
    end_t   = parse_hhmm(config.AFTERHOURS_SCAN_END_IST, 8, 45)
    # Window spans midnight: active from start until end (next day)
    return now_t >= start_t or now_t < end_t


# Per-mode flag: has the finalize pass fired today?


def _compute_afterhours_market_date(now_t=None):
    """Shared market-date helper (2026-09-17, session58) — extracted out of
    _afterhours_scan_body so the manual-trigger path (run_afterhours_scan_manual,
    below) computes the exact same target date as the scheduled tick instead of
    duplicating (and risking drifting from) this logic. Determines the next
    upcoming trading date: after close (15:45+ IST) → target tomorrow; before
    open (<08:45 IST) → target today. Skips weekends and tz_utils.is_nse_holiday()
    dates on both branches (session57 fix — see the original inline comment this
    was extracted from for the Balipratipada/Diwali incident this covers).
    `now_t` lets a caller pass an already-computed ist_now().time() (the
    scheduled tick already has one); defaults to computing it fresh, which is
    what the manual trigger does."""
    from zoneinfo import ZoneInfo
    from tz_utils import ist_now, is_nse_holiday

    if now_t is None:
        now_t = ist_now().time()
    start_t = parse_hhmm(config.AFTERHOURS_SCAN_START_IST, 15, 45)
    if now_t >= start_t:
        tomorrow = datetime.now(ZoneInfo("Asia/Kolkata")) + timedelta(days=1)
        while tomorrow.weekday() >= 5 or is_nse_holiday(tomorrow):
            tomorrow += timedelta(days=1)
        return tomorrow.strftime("%Y-%m-%d")
    else:
        candidate = datetime.now(ZoneInfo("Asia/Kolkata"))
        while candidate.weekday() >= 5 or is_nse_holiday(candidate):
            candidate += timedelta(days=1)
        return candidate.strftime("%Y-%m-%d")


async def _afterhours_scan_body(mode: str, manual: bool = False) -> dict:
    """One after-hours scan tick for a mode. Called from worker thread.

    `manual=True` (2026-09-17, session58) is the new manual-trigger path used
    by POST /afterhours/run-manual/{mode}: it bypasses the
    afterhours_news_scan_enabled toggle and the 15:45–08:45 window check (a
    manual "run it now" click is an explicit override of both — same posture
    as how /cycle/run/{mode} already lets a manual click run outside
    auto-pilot's own schedule), but otherwise executes the exact same
    finalize-or-scan body so results are identical either way. Every tick —
    scheduled or manual — now records gate.afterhours_scan_last_run_at/_ok so
    the frontend has one accurate "last run" indicator regardless of which
    path fired it. Returns a small result dict for the manual caller to hand
    back to the HTTP response; the scheduled path (which doesn't read the
    return value) is unaffected by this change."""
    from tz_utils import ist_today_str, ist_now, parse_hhmm, is_nse_holiday
    from watchlist_engine.afterhours_scan import run_afterhours_scan, finalize_nextday_watchlist

    Session = get_session_factory()
    db = Session()
    result = {"mode": mode, "ran": False, "reason": None, "market_date": None,
              "written": 0, "finalized": False}
    try:
        gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
        if gate is None:
            result["reason"] = "gate_not_found"
            return result
        if not manual:
            if not getattr(gate, "afterhours_news_scan_enabled", False):
                result["reason"] = "feature_disabled"
                return result
            if not _is_afterhours_window_active():
                result["reason"] = "outside_window"
                return result
        # A10 fix: is_ist_weekday() guard removed — Sat/Sun nights are valid scan
        # windows for Monday open. market_date calculation below already skips weekends.

        now_t = ist_now().time()
        market_date = _compute_afterhours_market_date(now_t)
        result["market_date"] = market_date

        # ── Finalize pass (once per day, at/after AFTERHOURS_FINALIZE_TIME_IST) ──
        finalize_t = parse_hhmm(config.AFTERHOURS_FINALIZE_TIME_IST, 8, 45)
        today = ist_today_str()
        if (
            now_t >= finalize_t
            and gate.afterhours_finalize_last_run != today
            and market_date == today  # only finalize for today's target
        ):
            # 2026-09-17 fix (session56 audit): persisted to the gate row
            # (was an in-memory module dict) so a restart between finalize
            # time and market open can't re-fire this and re-send the
            # Telegram notification — same guard pattern every other
            # scheduled feature's _last_run column already uses.
            gate.afterhours_finalize_last_run = today
            gate.afterhours_scan_last_run_at = ist_now()
            gate.afterhours_scan_last_run_ok = True
            db.commit()
            shortlist = await finalize_nextday_watchlist(db, mode, market_date)
            if shortlist:
                await notify_async(
                    f"📋 *After-hours watchlist finalized — {mode}*\n"
                    f"Top {len(shortlist)} symbol(s) ready for today's open:\n"
                    + "\n".join(f"  • {s}" for s in shortlist)
                )
            result["ran"] = True
            result["finalized"] = True
            return result  # finalize is the last action before open — no scan this tick

        # ── Regular scan pass ─────────────────────────────────────────────────
        written = await run_afterhours_scan(db, mode, market_date)
        if written:
            logger.info(
                "[afterhours] scan tick [%s]: %d row(s) upserted for market_date=%s",
                mode, written, market_date,
            )
        gate.afterhours_scan_last_run_at = ist_now()
        gate.afterhours_scan_last_run_ok = True
        db.commit()
        result["ran"] = True
        result["written"] = written or 0
        return result
    except Exception as e:
        logger.exception("afterhours scan tick failed for %s", mode)
        await notify_async(f"⚠️ *After-hours scan error — {mode}*\n{str(e)[:200]}")
        try:
            # Best-effort: roll back whatever this attempt left dangling
            # before recording the failure, so the UPDATE below doesn't
            # itself fail on a poisoned transaction.
            db.rollback()
            gate = db.query(models.TradeGateState).filter_by(mode=mode).first()
            if gate is not None:
                from tz_utils import ist_now as _ist_now
                gate.afterhours_scan_last_run_at = _ist_now()
                gate.afterhours_scan_last_run_ok = False
                db.commit()
        except Exception:
            logger.exception("afterhours scan: also failed to record last_run failure for %s", mode)
        result["reason"] = str(e)[:200]
        return result
    finally:
        db.close()


def _run_afterhours_tick_sync(mode: str) -> None:
    """Worker-thread wrapper — same pattern as the other tick bodies.
    2026-09-17 (session58): now takes the same non-blocking
    _get_afterhours_lock a manual trigger uses, so a scheduled tick that
    lands mid-manual-run simply skips (there's another one along in
    AFTERHOURS_SCAN_INTERVAL_SECONDS) instead of running concurrently
    against a manual click writing the same gate row / NextDayWatchlistEntry
    rows for the same mode."""
    lock = _get_afterhours_lock(mode)
    if not lock.acquire(blocking=False):
        logger.info("afterhours scan: skipping scheduled tick for %s — a run is already in progress", mode)
        return
    try:
        _run_coro_in_new_loop(_afterhours_scan_body, mode)
    finally:
        lock.release()


# ── Manual trigger (2026-09-17, session58, user request) ────────────────────
# Dedicated lock (not the entry/exit locks _get_lock / _get_exit_lock — this
# never touches orders, so there's no reason to contend with those) so a
# manual click can't overlap either the scheduled afterhours loop's own tick
# for the same mode or a second manual click fired before the first returns.
_afterhours_mode_locks: dict = {}
_afterhours_locks_init_lock = threading.Lock()


def _get_afterhours_lock(mode: str) -> threading.Lock:
    with _afterhours_locks_init_lock:
        if mode not in _afterhours_mode_locks:
            _afterhours_mode_locks[mode] = threading.Lock()
        return _afterhours_mode_locks[mode]


def run_afterhours_scan_manual_sync(mode: str) -> dict:
    """Synchronous worker-thread entry point for POST /afterhours/run-manual/{mode}.
    Non-blocking lock acquire — mirrors /cycle/run/{mode}'s contract: if a
    scan for this mode is already in progress (the scheduled loop's own tick,
    or a previous manual click still running), return immediately instead of
    queueing, so the caller isn't left hanging for up to
    AFTERHOURS_SCAN_INTERVAL_SECONDS. Must be called via
    `await asyncio.to_thread(run_afterhours_scan_manual_sync, mode)` from the
    FastAPI route — never awaited directly on the main loop (same reasoning
    as every other tick body in this module — see the module docstring)."""
    lock = _get_afterhours_lock(mode)
    if not lock.acquire(blocking=False):
        return {"mode": mode, "ran": False, "reason": "already_in_progress"}
    try:
        return asyncio.run(_afterhours_scan_body(mode, manual=True))
    finally:
        lock.release()


async def _afterhours_scan_loop() -> None:
    """After-hours news scan loop. Runs 24/7; does nothing outside the
    15:45–08:45 window or when the toggle is off. Interval from config."""
    await asyncio.sleep(_STARTUP_DELAY_SECONDS + 25)
    logger.info(
        "Auto-pilot AFTERHOURS SCAN loop running (interval=%ss, window=%s–%s IST)",
        config.AFTERHOURS_SCAN_INTERVAL_SECONDS,
        config.AFTERHOURS_SCAN_START_IST,
        config.AFTERHOURS_SCAN_END_IST,
    )
    while True:
        for mode in ("DEMO", "REAL"):
            try:
                await asyncio.to_thread(_run_afterhours_tick_sync, mode)
            except Exception:
                logger.exception("afterhours scan loop: unexpected error for %s", mode)
        await asyncio.sleep(config.AFTERHOURS_SCAN_INTERVAL_SECONDS)


def start() -> None:
    """Idempotent — safe to call from startup() even if hot-reloaded."""
    global _full_task, _exit_task, _schedule_task, _totp_task, _afterhours_task
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
    # After-hours news scan loop — harmless no-op while toggle is off;
    # becomes active at 15:45 IST when afterhours_news_scan_enabled=True.
    if _afterhours_task is None or _afterhours_task.done():
        _afterhours_task = asyncio.create_task(_afterhours_scan_loop())
        logger.info(
            "Auto-pilot AFTERHOURS SCAN background task created (interval=%ss).",
            config.AFTERHOURS_SCAN_INTERVAL_SECONDS,
        )
