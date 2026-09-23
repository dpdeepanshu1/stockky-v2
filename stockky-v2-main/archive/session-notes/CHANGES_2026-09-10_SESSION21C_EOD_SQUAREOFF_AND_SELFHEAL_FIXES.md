# Fixes — 2026-09-10 Session 21c

## Summary

Continued the session 21 audit of `services/real-trade-service`. Four
bugs found and fixed across two audit passes, all in the same family as
session 21's Fix 1 (missing mutual-exclusion / missing duplicate-send
guard around a real order): two are double-sell/oversell risks for REAL
capital (EOD square-off, manual SELL confirm), one is a REAL-account
bookkeeping-corruption risk (holdings-sync reconcile double-booking a
partial exit), and one is a double-fill risk for the DEMO paper account
(dashboard self-heal).

---

## Fix 5 — EOD square-off could send a second REAL SELL over a still-pending partial exit

**File:** `services/real-trade-service/execution/auto_pilot.py` (`_eod_squareoff`)

**Root cause:** `record_real_exit_sent`'s own docstring (`portfolio.py`) is
explicit: a **partial** exit (`full=False` — e.g. a target-hit partial
fired by the fast-exit or full-cycle tick shortly before the close)
deliberately leaves the position's status at `OPEN`/`PARTIALLY_CLOSED` —
the remainder is still live and needs further evaluation. The only thing
tracking that a SELL is already working at the broker is the `TradeOrder`
row itself (`status` in `PLACED`/`PARTIAL`), and `exit_engine.evaluate_mode`
always checks `_has_pending_real_sell()` before sending a new one.

`p.qty_open` is likewise only decremented once `reconcile_real_orders()`
confirms the fill against Dhan's own trade book — never at send time. So a
position with a partial SELL still awaiting broker confirmation is
returned by `open_positions()` with its **pre-partial-exit** `qty_open`
still intact.

`_eod_squareoff`'s REAL branch called `_send_real_sell(db, p, p.qty_open,
...)` directly for every position in that snapshot, with **no**
`_has_pending_real_sell()` check — the one direct call site in the service
that skipped the guard every other SELL-sending path already has.

**Impact:** If a partial target-hit exit was sent minutes before the
15:15 square-off window and hadn't yet been confirmed filled by Dhan
(reconcile runs on its own cadence, not synchronously with the send), EOD
square-off would fire a **second, full-quantity MARKET SELL** for the same
position while the first partial SELL was still resting at the broker —
two live SELL orders outstanding against overlapping shares at once. Best
case, Dhan rejects the second order (`is_oversell_error`); worst case, it
fills against demat holdings the first order hasn't finished consuming
yet, understating what's actually still held and leaving the account short
of what this system's own books show.

Note: this is a **sequential**, single-threaded race, not a
concurrent-thread one — `_eod_squareoff` already runs under the same
per-mode `threading.Lock` as every other tick for that mode (via
`_run_schedule_tick_sync`), so no other tick can interleave with it. The
race is purely "the guard other send-sites have was never added here."

**Fix:** The REAL branch now checks `_has_pending_real_sell(db, p.symbol)`
before calling `_send_real_sell`, skipping (and counting, in a new
`skipped` tally) any position that already has a SELL in flight — that
position's fate is already being handled by the pending order, and the
next fast-exit tick or reconcile pass settles it normally. The EOD
square-off Telegram notification now reports the skipped count alongside
closed/sent/failed.

---

## Fix 6 — DEMO self-heal (`GET /positions|orders/DEMO`) could double-fill a pending BUY

**File:** `services/real-trade-service/main.py` (`_self_heal_orders`)

**Root cause:** Session 21's Fix (decision #32 follow-up, already present
in this codebase) locked the **REAL** branch of `_self_heal_orders` behind
the same per-mode `threading.Lock` used everywhere else, specifically
because this function runs on every `GET /positions/{mode}` and
`GET /orders/{mode}` poll — i.e. continuously from an open dashboard tab —
so it can race a manual Run Cycle or an in-flight auto-pilot tick
constantly, not just on a rare coincidence.

The **DEMO** branch (`check_pending_fills` + `expire_stale_orders`) was
left completely unguarded. `try_fill_entry`'s own re-entrancy check
(`order.status != "PLACED": return False`) is a plain in-memory read
against whatever a given SQLAlchemy session already has loaded — not a
`SELECT ... FOR UPDATE` — so it does nothing to stop two different
sessions (e.g. a dashboard poll's self-heal and a concurrently-running
manual Run Cycle or auto-pilot full-cycle tick for DEMO, both of which
also call `check_pending_fills` via `cycle_runner.run_cycle_core`) from
each loading the same `PLACED` BUY order before either commits, both
passing the check, and both filling it.

**Impact:** A double fill on the same order adds the qty **twice** to the
DEMO position (or opens it twice, depending on timing) and debits
`cash_available` **twice** for shares that were only ever "bought" once —
silently wrong DEMO P&L and position sizing, which is exactly the
paper-trading data this system's calibration and go/no-go-to-REAL
decisions lean on.

**Fix:** The DEMO branch now takes the same per-mode lock as the REAL
branch, with the same non-blocking, skip-if-busy semantics (this is a
best-effort catch-up on a read path — never an error; the very next poll
retries). No functional change to `check_pending_fills`/
`expire_stale_orders` themselves.

---

---

## Fix 7 — Manual SELL confirm could send a second REAL SELL over a still-pending partial exit

**File:** `services/real-trade-service/manual_engine.py` (`evaluate_manual_order`, SELL branch)

**Root cause:** Same missing guard as Fix 5, at a second, independent
`_send_real_sell` call site. `_resolve_position()` matches positions in
`OPEN`/`PARTIALLY_CLOSED` — the exact statuses a position keeps while a
*partial* exit (`full=False`) is still awaiting broker confirmation (see
Fix 5's writeup) — and `position.qty_open` isn't decremented until
`reconcile_real_orders()` confirms the fill. The manual SELL path never
called `_has_pending_real_sell()` before sending.

**Impact:** A user clicking "Confirm SELL" on a position that had a
partial target-hit exit sent moments earlier by the fast-exit/full-cycle
tick (still unreconciled) would fire a second MARKET SELL over the same
overlapping shares a pending order was already working — the identical
oversell exposure as Fix 5, just reached through the manual ticket flow
instead of the schedule loop. The per-mode lock held by
`/manual-order/{mode}/confirm` rules out a *concurrent* send from another
tick; it does nothing about an *earlier* tick's send that's still in
flight, which is exactly this case.

**Fix:** The SELL branch now checks `_has_pending_real_sell(db, symbol)`
before calling `_send_real_sell`, returning a clean `REJECTED` /
`pending_sell` preview instead of sending a second order when one is
already working.

---

## Fix 8 — `holdings_sync_reconcile` could double-book a partial REAL exit's qty and cash

**File:** `services/real-trade-service/portfolio/portfolio.py` (`holdings_sync_reconcile`)

**Root cause:** This function's candidate query excludes `PENDING_EXIT`
(a full exit in flight) but — like Fixes 5 and 7 — never accounted for a
*partial* exit in flight, which leaves the position at
`OPEN`/`PARTIALLY_CLOSED` on purpose. It compares `qty_open` against
Dhan's live holdings/positions snapshot and, wherever the broker reports
fewer shares than `qty_open`, treats the gap as an *external* partial sale
(placed outside this app): it caps `qty_open` down and refunds cash at
`avg_entry_price` (cost basis) right there.

`cycle_runner.run_cycle_core` runs the exit stage (which can send a
partial SELL as a MARKET order — NSE typically fills these within the
same second) *before* the reconcile stage. `reconcile_real_orders()`
itself calls `holdings_sync_reconcile()` *before* the loop that actually
checks pending orders against Dhan's order book and books a confirmed
fill via `record_real_exit_fill()`. So within one cycle: a partial SELL
can already be filled at the broker (reflected in the holdings/positions
snapshot) while its `TradeOrder` row is still `PLACED`, moments before the
same `reconcile_real_orders()` call reaches the code that would book it
properly.

**Impact:** `holdings_sync_reconcile` would misread that gap as an
external sale and cap `qty_open` down + refund cash at cost basis, and
then — later in the *same* function call — the pending-orders loop would
find the same SELL order confirmed at the broker and book it *again* via
`record_real_exit_fill` (a second `qty_open` decrement off the
already-reduced figure, and a second, this time correct, cash credit at
the actual exit price). Net effect: `qty_open` decremented twice for one
real-world sell (position can close early / go inconsistent) and
`cash_available` credited twice (once at the wrong cost-basis figure via
the sync path, once correctly via the fill path) — silent REAL-account
bookkeeping corruption on essentially any partial exit that's fast enough
to beat the reconcile pass to the holdings API, which for a MARKET order
is the common case, not an edge case.

**Fix:** `holdings_sync_reconcile`'s per-position loop now skips (via
`continue`) any position with `_has_pending_real_sell(db, symbol) ==
True`, the same guard used everywhere else a REAL SELL's in-flight state
needs to be respected. The in-flight SELL is left for the pending-orders
loop (this cycle or the next) to book through the one path that's allowed
to — the sync/ghost-close logic only ever runs against positions with no
SELL currently working.

---

## Files Changed

| File | Change |
|------|--------|
| `services/real-trade-service/execution/auto_pilot.py` | Fix 5: `_eod_squareoff` REAL branch checks `_has_pending_real_sell` before sending; notification includes skipped count |
| `services/real-trade-service/main.py` | Fix 6: `_self_heal_orders` DEMO branch now guarded by the same per-mode lock as the REAL branch |
| `services/real-trade-service/manual_engine.py` | Fix 7: manual SELL confirm checks `_has_pending_real_sell` before sending |
| `services/real-trade-service/portfolio/portfolio.py` | Fix 8: `holdings_sync_reconcile` skips positions with a pending REAL SELL |

## Verified, not re-fixed

Re-checked all four fixes from `CHANGES_2026-09-10_SESSION21_REAL_TRADE_FIXES.md`
against the current code — all four are present and intact:
`/cycle/run/{mode}`'s lock acquisition, `_ensure_account_columns` wired
into `init_schema()`, `_mode_locks_meta` guarding `_get_lock`'s lazy init,
and the UC composite-score floor in `entry_engine/entry.py`.

Also read in full and found no further bugs in: `cycle_runner.py`,
`risk_engine/engine.py` (all 9 checks, ordering, SELL-side bypass list),
the rest of `exit_engine/exit.py` (every exit branch — emergency gap,
stop-hit, target-hit-partial, time-stop, breakeven, ATR trailing — and
every Dhan-rejection branch), `entry_engine/entry.py`'s candidate
consumption/dedup loop and `evaluate_mode`, `execution/dhan_client.py`'s
`place_order`/`cancel_order` (no unguarded retry that could double-send),
`execution/reconcile.py`'s fill-booking loop, every other call site of
`auto_pilot._get_lock` in `main.py`, and `db.py`'s remaining
`_ensure_*_columns` migrations against `models.py`'s current columns.
