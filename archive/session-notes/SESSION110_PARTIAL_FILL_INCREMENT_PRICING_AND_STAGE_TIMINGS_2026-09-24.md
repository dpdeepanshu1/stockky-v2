# Session 110 (2026-09-24): partial fills booked at their own price (the last xfail) + exact pipeline stage timings

Picked from the session109 open-issues list: #1 (partial fills at the cumulative
average — the only remaining xfail) and #10 (`pipeline_status` stage timings
misattributed since session48b). Not attempted: #2 (`notify_sync` can block ~42 s
inline in exit paths) — it is a design change across dozens of call sites and
deserves its own round.

Baseline (VM, after session109): 2638 passed, 1 skipped, 1 xfailed, 8218 stmts,
1 missed. This session ends at **2716 passed, 2 skipped (sandbox; expect 1 on the
VM), 0 xfailed, 8304 stmts, 0 missed (100%)**.

## 1. Partial fills are now booked at each increment's own price (`execution/reconcile.py`)

**The bug (pinned as a strict xfail since round 1).** Dhan's `averageTradedPrice`
is the average of the WHOLE order to date. reconcile booked every new increment at
that cumulative average: 5 @ 100 then 5 @ 102 (cumulative 101) was booked as
5 @ 100 + 5 @ 101 -> position average 100.50 and cash −1,005 instead of 101.00 /
−1,010. SELL partials drifted realized P&L the same way, and the "BUY partial
fill" Telegram message quoted the wrong price.

**The fix.** The increment's own price is recoverable from two consecutive polls:
`(cum_qty × cum_avg − previous cumulative value) / delta_qty`. New nullable column
`TradeOrder.broker_fill_notional` stores the broker's cumulative filled value as of
the last booked poll (additive migration `_ensure_fill_notional_column`, registered
in `init_schema` and in `tests/test_db.py`'s `COLUMN_FUNCS`; Oracle `BINARY_DOUBLE`,
Postgres `FLOAT`; the drift guard and DDL-vs-model parity tests pass).
`_book_fill_delta` takes an optional `cumulative_qty`; both reconcile call sites
(normal path and the dead-status-after-partial path) and the post-cancel late-fill
path in `entry_engine/entry.py::expire_stale_orders` now pass it. Calls without it
(direct callers) behave exactly as before.

Property verified end to end: after any sequence of polls the position's average
equals the broker's final cumulative average exactly (e.g. 4 @ 100, then cum 10 @
101, then cum 12 @ 101.5 -> position 12 @ 101.50; SELL 4 @ 110 then cum 10 @ 112
-> realized P&L exactly 10 × (112 − 100)).

**Why it falls back to the cumulative average (the old behaviour, never worse)**
in `_increment_price`, rather than trusting the derivation blindly — the two
averages are rounded to paise, so deriving from them multiplies that noise by
`cum_qty / delta_qty` (a +10-share increment on a 1,000-share order can hide
~₹1.00 of noise per share):
- first fill (`cum_qty == delta_qty`: increment == cumulative, exact) or no stored
  baseline (orders booked before this column existed: that one increment books at
  the cumulative average as before, and tracking starts from there);
- non-positive `delta`/`cum_avg`, a non-finite or non-positive derived price;
- the correction is not larger than the worst-case paise-rounding noise
  `0.005 × (cum_qty + prev_qty) / delta_qty`;
- the derived price is more than 25% from the cumulative average (circuit limits
  cap a day's move at 20%, so this means inconsistent broker figures).

Tests: the xfail became passing tests (`TestIncrementPricing`, 8 tests incl. BUY,
3-poll, SELL, cancelled-remainder, legacy order, repeated poll) plus
`TestIncrementPriceFunction` (19 cases: every fallback + both sides of the band and
noise thresholds) and a late-fill-after-partial test on `expire_stale_orders`.
`reconcile.py` and `db.py` stay 100%. 15 mutations, 0 real survivors — the two that
survive are provably equivalent (`<=` vs `<` at exact float equality of the noise
bound; and `inc <= 0` being redundant with the 25% band that already rejects it).

**Deploy note.** The migration runs on next boot (`ALTER TABLE trade_orders ADD
... broker_fill_notional`, nullable, no default). Orders already `PLACED`/`PARTIAL`
when it ships have NULL there, so their next increment is booked the old way once.
Recommended: run a DEMO cycle, then check a real partial fill's `TradeFill.price`
vs Dhan's tradebook on the first REAL partial.

## 2. Pipeline stage timings are exact again (`pipeline_status.py`, `cycle_runner.py`)

`dynamic_universe -> watchlist` and `candidates` run concurrently (session48b) but
`set_stage()` only tracks ONE current stage, so they overwrote each other
(session94 probe: real 50/100/300 ms reported as candidates=50.7, watchlist=249.5).
New `stage_started(mode, stage)` / `stage_finished(mode, stage)` keep an exact
timer per overlapping stage; `set_stage()` no longer overwrites an exactly-timed
stage and still drives the live "current stage" label and times the sequential
stages as before. `cycle_runner` wraps the three overlapping stages (timer stop in
`finally`, best-effort like `_stage`). The frontend only reads key presence, so it
needs no change (and the dots for all three stages now reliably light up).
Tests: 9 deterministic fake-clock unit tests (incl. the exact session94 scenario);
3 real-`asyncio.sleep` integration tests (lower bounds = each stage's own sleep,
plus generous upper bounds on the two short stages, which is what catches the old
bug); and a test that timer failures never block a cycle. 15 mutations, 0 survivors.
Not changed: the live stage LABEL still flips among the three concurrent stages
while they run.

## Housekeeping
- `AUDIT_REPORT.md`: the "Remaining xfail (1)" section marked resolved.
- Still open list: see session109's; #1 and #10 are now closed, #2 remains the
  top item.
