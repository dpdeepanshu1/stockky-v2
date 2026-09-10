# 2026-09-10 — reconcile-lock audit (decision #32 follow-up)

## Context

Decision #32 (same day, earlier) fixed `/cycle/run/{mode}` to acquire
`auto_pilot._get_lock(mode)` before calling `run_cycle_core`, closing a race
where a manual Run Cycle and an auto-pilot tick could evaluate
candidates/entries/exits concurrently for the same mode and double-place
orders.

That fix only covered `run_cycle_core`'s own call to
`reconcile_real_orders()` (via `cycle_runner.py`). A follow-up audit of
every other call site of `reconcile_real_orders`, `holdings_sync_reconcile`,
`_send_real_sell`, `try_fill_entry`/`close_position`, and
`dhan_client.place_order`/`cancel_order` in `real-trade-service` found five
more places that mutate the same REAL order/position/cash state with **no
lock at all**, each racing auto-pilot's ticks (or each other) exactly like
the bug #32 fixed.

## Fixed this round (all in `main.py`, all take the same per-mode
`threading.Lock` via `execution.auto_pilot._get_lock(mode)`, non-blocking
acquire, 409 on busy — same contract as `/cycle/run/{mode}`)

1. **`_self_heal_orders`** (hit by every `GET /positions/{mode}` and
   `GET /orders/{mode}` dashboard poll) — called `reconcile_real_orders()`
   unguarded on every read, i.e. continuously, not just on rare
   coincidence. Fixed to skip (not error) when the lock is busy, since this
   is a best-effort catch-up on a read path — the next poll retries.
2. **`POST /reconcile/{mode}`** (manual reconcile trigger) — same
   unguarded call; now 409s if a cycle is in flight.
3. **`POST /reconcile/{mode}/holdings-sync`** (manual ghost-position sweep)
   — same fix; `holdings_sync_reconcile` force-closes positions and
   refunds cash, so it must not run concurrently with a cycle either.
4. **`POST /manual-order/{mode}/confirm`** — manual BUY/SELL tickets wrote
   TradeOrder/TradeDecision rows and called `dhan_client.place_order` /
   `exit_engine._send_real_sell` directly, unguarded. A manual BUY could
   race entry_engine's own candidate evaluation for the same symbol
   (both read account_state before either commits); a manual SELL could
   race exit_engine sending its own SELL for the same position — two SELL
   orders at the broker for shares that only exist once.
5. **`POST /positions/{mode}/{position_id}/close`** — same class of race
   as #4, on the dedicated manual-close route. Position/qty lookup moved
   inside the locked section so a retry after a 409 sees fresh
   `qty_open`, not a value read before the race window.
6. **`POST /orders/{mode}/{order_id}/cancel`** — lower severity (no
   duplicate broker order) but real: the unconditional
   `order.status = "CANCELLED"` write could stomp a FILLED/PARTIAL status
   a concurrent reconcile had just committed, leaving the local order
   looking cancelled while the shares were actually filled and booked.
   Added the same lock plus `db.expire_all()` before the status re-check
   so a concurrent commit is always visible.

## Not changed (verified already correct)

- `auto_pilot._full_tick_body` / `_exit_only_tick_body` already run inside
  `_get_lock(mode)` (blocking and non-blocking acquires respectively) —
  their own `reconcile_real_orders()` calls were never the problem.
- `POST /candidates/manual/{mode}` only inserts a queue row that
  entry_engine reads inside the lock-protected cycle — no lock needed
  there.
- `dhan_client.place_order`/`cancel_order` have no client-side retry logic,
  so no double-submission risk at that layer independent of the
  route-level races above.

## Net effect

Every route that can send a real order to Dhan or book/close a REAL
position/order now goes through the same single per-mode mutual-exclusion
lock as auto-pilot's own ticks and the already-fixed `/cycle/run/{mode}`.
A busy lock now means "try again in a moment" (409) instead of a second,
concurrent write racing the first.

---

# 2026-09-10 (same day, follow-up pass) — entry sizing bug: `reserved_cash`
# never actually protected same-cycle candidates from each other

## Bug

`entry_engine/entry.py`'s `evaluate_mode()` threads a `reserved_cash` value
through `_account_state(db, mode, gate_armed, reserved_cash)` so that when
more than one candidate is evaluated in the same cycle, later candidates are
sized against what's actually still available — not the full account
balance as if every earlier candidate in the same batch didn't exist.

The increment (`reserved_cash += decision.proposed_qty * entry_price`) was
located in the **second** loop (`for e in selected:`), which only runs
*after* Gate 6 has ranked the whole batch — which itself only runs *after*
the entire per-candidate loop (the one that actually calls
`_account_state()`, at line ~565) has already finished. Every
`_account_state()` call this cycle had therefore already happened by the
time `reserved_cash` was ever incremented above 0. Net effect: every
candidate in a batch was sized against the **full, un-reserved**
`account.cash_available`, regardless of how many other candidates earlier
in the same cycle had already been (tentatively) approved for the same pool
of cash.

Concrete failure mode: ₹100,000 cash available, three candidates each
sized to use up to ₹50,000. Each is evaluated independently and each
individually clears risk_engine's `cash_available_cap` check (every one of
them sees the full ₹100,000, not accounting for the others). If Gate 6's
quality ranking selects more than one of them, the second (and later)
REAL placement(s) are sent to Dhan and get rejected for insufficient
funds — a risk-approved entry failing at the broker for exactly the
reason this system's own sizing logic exists to prevent.

## Fix

Moved the `reserved_cash` increment into the **first** loop, immediately
after a candidate is approved and staged into `approved_entries` — using
`(result.approved_qty or proposed_qty) * entry_price`, the same quantity
Gate 6/placement later uses. This means the very next candidate evaluated
in the same loop sees the reduced `cash_available` and sizes (or rejects
via `cash_available_cap`) accordingly, exactly as originally intended.

Removed the now-redundant increment from the second loop (it would have
double-reserved cash that no remaining `_account_state()` call in this
cycle could ever be affected by). The existing decrement lines (on Dhan
placement failure / invalid-IP auto-disarm) are unchanged and still
correctly release what was reserved at staging time, since
`decision.proposed_qty` doesn't change between staging and placement.

Trade-off accepted: a candidate later dropped by Gate 6's cross-candidate
ranking still "reserved" cash during the first loop and that reservation
is not un-done — but by the time Gate 6 runs, the first loop (the only
place that reads `reserved_cash` for sizing) has already finished, so this
has zero effect on this cycle's sizing decisions. Undersizing/rejecting a
later candidate more conservatively than strictly necessary is a far
smaller problem than the original bug (sending a doomed, risk-"approved"
order to the broker).

Verified: `python3 -m py_compile` clean across the whole service.
