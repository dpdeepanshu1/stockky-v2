# Fixes — 2026-09-10 Session 21

## Summary

Four bugs found and fixed in `services/real-trade-service` during a full
audit of the real-trade service, the auto-pilot, and the entry/exit
pipeline.  Three of the four were correctness bugs capable of causing
duplicate orders or silent data-loss; one was a thread-safety race in the
lock primitive itself.

---

## Fix 1 — `/cycle/run/{mode}` never acquired the auto-pilot lock (Decision #32)

**File:** `services/real-trade-service/main.py`

**Root cause:** `auto_pilot.py`'s module docstring (point 2) explicitly
states that the per-mode `threading.Lock` prevents the manual "Run Cycle"
button from racing the auto-pilot timer.  `auto_pilot._run_full_tick_sync`,
`_run_exit_tick_sync`, and `_run_schedule_tick_sync` all acquire that lock
before running their respective cycle bodies.  `main.py`'s
`POST /cycle/run/{mode}` route — the manual "Run Cycle" button — never
touched the lock at all.

**Impact:** A manual click landing at the same moment as an auto-pilot
full-cycle tick for the same mode caused both to run `run_cycle_core`
concurrently:
- Two simultaneous entry evaluations → two `TradeCandidate` rows consumed
  independently → duplicate `TradeOrder`/`TradeDecision` rows for the
  same candidate, potentially sending two BUY orders to Dhan for the same
  symbol in the same second.
- Two simultaneous exit evaluations → `_has_pending_real_sell()` could
  return `False` in both threads at the same instant (the first thread's
  SELL order is not yet in the DB when the second thread checks) → two
  MARKET SELL orders sent for the same position.

**Fix:** The route now acquires the same per-mode `threading.Lock` via
`auto_pilot._get_lock(mode)` with `blocking=False`.  A mid-flight
auto-pilot cycle returns HTTP 409 ("A cycle for {mode} is already in
progress — try again once the current cycle finishes.") immediately,
matching the fast-exit tick's own skip-if-busy semantics.  The lock is
released in a `finally` block so a route exception never leaks it.

---

## Fix 2 — `realized_pnl_total` column on `trade_accounts` had no additive migration

**File:** `services/real-trade-service/db.py`

**Root cause:** `models.py` defines `TradeAccount.realized_pnl_total`
(added 2026-09-09 for the dashboard's all-time P&L summary).
`portfolio.py`'s `close_position()` and `record_real_exit_fill()` both
increment it on every closed trade.  `init_schema()` had no `ALTER TABLE`
migration for existing deployed databases — `create_all(checkfirst=True)`
is a no-op for tables that already exist, so the column would only appear
on a freshly-created database, never on the running production one.

**Impact:** On any already-running instance, every call to
`close_position()` or `record_real_exit_fill()` raised
`sqlalchemy.exc.OperationalError: column "realized_pnl_total" does not exist`,
crashing the exit cycle mid-flight and potentially leaving positions
in an intermediate state (exit partially booked, account cash not
updated, no P&L recorded).

**Fix:** Added `_ensure_account_columns()` using the same additive-migration
idiom as `_ensure_manual_order_columns`, `_ensure_gate_state_columns`, etc.
Called from `init_schema()` immediately after `_fix_stale_dhan_token_expiry`.

---

## Fix 3 — `_get_lock()` in `auto_pilot.py` had a thread-safety race in its own lazy init

**File:** `services/real-trade-service/execution/auto_pilot.py`

**Root cause:** The lazy-init in `_get_lock()`:

```python
def _get_lock(mode: str) -> threading.Lock:
    if mode not in _mode_locks:
        _mode_locks[mode] = threading.Lock()
    return _mode_locks[mode]
```

The check-then-assign is not atomic.  Two worker threads arriving at
`if mode not in _mode_locks` simultaneously (e.g. the DEMO full-cycle and
the DEMO fast-exit threads both starting for the first time, before any
tick has completed for that mode) can each find the key absent, each
construct a separate `threading.Lock()`, and each write their own object
to `_mode_locks[mode]`.  The second write overwrites the first, so the
two threads hold references to **two different lock objects** — the fast-exit
thread is holding one lock and the full-cycle thread acquires a different
one, and the mutual exclusion that is the entire point of the lock is void
for those two threads.

**Impact:** This is a narrow window that only opens on the very first tick
after a service start (once one tick completes, `_mode_locks[mode]` is set
and both threads read the same object thereafter).  But it is a real race:
the 20-second startup delays in `_fast_exit_loop` (+5 s) and
`_full_cycle_loop` (+0 s offset, 20 s base) are close enough that the
first DEMO fast-exit tick and the first DEMO full-cycle tick can arrive
within the same scheduler quantum, and `asyncio.to_thread` dispatches them
to real OS threads in parallel — exactly the scenario this lock was meant
to protect against.

**Fix:** Added `_mode_locks_meta: threading.Lock = threading.Lock()` as a
module-level meta-lock.  `_get_lock()` now wraps its dict check-and-insert
in `with _mode_locks_meta:`, making the init atomic.  The meta-lock is
only ever held for the microsecond it takes to read/write the dict; it is
never held while the per-mode lock itself is held.

---

## Fix 4 — `VOLUME_SHOCK_UPPER_CIRCUIT` composite score was never floored at 85

**File:** `services/real-trade-service/entry_engine/entry.py`

**Root cause:** The Gate 6 comment (added 2026-09-09 session20) said:

> *"UC scores now 85 so they sort first"*

But `_composite_quality_score()` was never modified — it computed the same
conviction/R:R/drift blend for every candidate regardless of label.  A
VOLUME_SHOCK_UPPER_CIRCUIT candidate with a low conviction score or a
price near the drift limit could compute a raw composite of, say, 32,
and sort behind non-UC candidates with scores of 55–70.

**Impact — two distinct failure modes:**

1. **Sort order wrong:** A UC candidate ranked 4th (beyond
   `ENTRY_MAX_NEW_PER_CYCLE=3`) is dropped by the per-cycle cap while
   weaker non-UC candidates ahead of it in the sort are selected —
   defeating the entire purpose of giving UC candidates priority.

2. **Floor bypass inconsistency:** A UC candidate with a raw score of 32
   clears the `is_upper_circuit` floor-bypass check and is included in
   `selected`, but sits at position 1 in the sort with a score of 32.
   If a non-UC candidate with a score of 55 is also present, it sorts
   *ahead* of the UC candidate (55 > 32), and if `ENTRY_MAX_NEW_PER_CYCLE`
   is hit, the UC candidate is still dropped — even though the floor bypass
   was supposed to guarantee it gets in.

**Fix:** UC candidates are now assigned
`composite_score = max(raw_composite_score, 85.0)` before the sort.  The
raw score is preserved in `raw_composite_score` and displayed in the
`PLACED` TradeOrderEvent detail (with a `[UC floor→85]` annotation when
the override applied), keeping the audit trail accurate.  85 matches the
value the original comment promised, and relative ordering between multiple
UC candidates is preserved (if two UC candidates both have raw scores below
85 they both score 85 and are ordered by insertion order; if one has a raw
score above 85 it keeps that higher score and sorts first).

---

## Files Changed

| File | Change |
|------|--------|
| `services/real-trade-service/main.py` | Fix 1: `/cycle/run/{mode}` acquires `auto_pilot._get_lock(mode)` |
| `services/real-trade-service/db.py` | Fix 2: `_ensure_account_columns()` added + called from `init_schema()` |
| `services/real-trade-service/execution/auto_pilot.py` | Fix 3: `_mode_locks_meta` guards lazy init in `_get_lock()` |
| `services/real-trade-service/entry_engine/entry.py` | Fix 4: UC composite score floored at 85; raw score preserved in audit log |
