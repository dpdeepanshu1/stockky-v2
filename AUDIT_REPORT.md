# Test & audit report — 2026-09-21 (round 2)

Scope: `services/real-trade-service` — two additional production bugs fixed after the prior session.
All tests offline (in-memory SQLite, scripted fake broker, no network).

## Result

| | before (round 1) | after (round 2) |
|---|---|---|
| real-trade-service tests | 375 passed, **2 xfailed** | **376 passed, 1 xfailed** |
| position-stocks-service tests | 1220 passed | 1220 passed (unchanged) |
| `entry_engine/entry.py` market-score bug | present | **FIXED** |
| `entry_engine/entry.py` stale-order cancel fills | invisible | **FIXED** |

## Bugs fixed this round

| # | Where | Bug | Impact | Fix |
|---|---|---|---|---|
| 8 | `real-trade-service/entry_engine/entry.py` `_get_market_regime` | `int(data.get("market_score") or 50)` — a genuine score of **0** (worst possible market) is falsy, so the regime gate reads it as a healthy 50 | System would enter trades in the worst-possible market condition, treating it as neutral | Changed to explicit `is None` check: `_raw_score = data.get("market_score"); score = int(_raw_score) if _raw_score is not None else 50` |
| 9 | `real-trade-service/entry_engine/entry.py` `expire_stale_orders` | After cancelling a stale entry at Dhan, the function immediately marks the order EXPIRED without checking whether any shares filled between the last reconcile pass and the cancel. `cycle_runner` runs `expire_stale_orders` **before** `reconcile_real_orders`, and reconcile only queries PLACED/PARTIAL orders — so those shares are owned at Dhan but invisible to Stockky (no position, no cash debit, no alert) | Real money buys real shares that Stockky never tracks | After a successful Dhan cancel, call `dhan_client.get_order_list`, compute `delta = filled_at_broker − already_booked`, and call `_book_fill_delta` if `delta > 0`. Falls through safely on any exception (logs warning, reconcile retries next cycle) |

Both fixes have regression tests that fail on the original code and pass now.

## Previously fixed (round 1) — still covered

| # | Where | Bug |
|---|---|---|
| 1 | `position-stocks/orders/entry.py` | Inner import caused `UnboundLocalError` on restricted stock; rejection never recorded |
| 2 | `entry.py` `attempt_entry` + `attempt_manual_entry` | Non-`SecurityNotResolvedError` around `get_security_id()` leaked ledger reservation + symbol lock |
| 3 | `entry.py` `attempt_manual_entry` | qty ≤ 0 released after symbol lock claim, not before |
| 4 | `entry.py` plain-MARKET fallback | BUY broker order id discarded |
| 5 | `position-stocks/orders/reconcile.py` | TARGET/STOP fill on placeholder double-released `available_capital` |
| 6 | `real-trade-service/risk_engine/engine.py` | BUY with qty ≤ 0 or stop ≥ entry was APPROVED |
| 7 | `real-trade-service/execution/reconcile.py` | No per-order error isolation; one bad order blocked all SELL confirmations |

## Remaining xfail (1) — needs design decision

* **Partial fills booked at cumulative average price** (`execution/reconcile.py`): Dhan's
  `averageTradedPrice` is the whole-order average, but each increment is booked at it.
  5 @ ₹100 then 5 @ ₹102 (cum. avg ₹101) → position avg ₹100.50, cash debited ₹1,005
  instead of ₹1,010. Fix needs per-increment price — derivable for BUYs from `TradeFill`,
  but exits write no `TradeFill` row, so a small schema change is required.
  Pinned: `TestKnownGaps.test_partial_fills_should_be_booked_at_the_increments_own_price`.

## Round 3 (this session) — `entry_engine/entry.py::evaluate_watchlist_entries` + real test coverage for `evaluate_mode` in both files

Started on the two items flagged as highest-value next targets: `exit_engine/exit.py`
(27% coverage) and `entry_engine/entry.py::evaluate_mode` (~830-line pipeline). A full
line read of both found no new bugs beyond what round 1/2 and the session history
already fixed — both are unusually heavily commented/self-documented at this point,
with the majority of previously-found issues explained inline.

While reading `entry.py` end to end, found and fixed a real bug in its sibling
Stage-2 function, **`evaluate_watchlist_entries`** (previously 0% tested, no test file
existed for it at all):

* **Division by zero crashes the whole watchlist pass, not just one symbol.**
  A live tick with `price <= 0` (bad/stale feed data — the exact condition
  `_entry_drift_ok` elsewhere in the same file already guards against) for a row
  whose `catalyst_price` is still the `0.0` "unknown" sentinel fell straight into the
  backfill branch, which set `catalyst_price` to that same non-positive price and then
  divided by it two lines later — `ZeroDivisionError`. That's raised inside the
  per-row `for row in active:` loop with no `try/except` around it, so it aborted
  evaluation of every **other** active watchlist row for the rest of that cycle too —
  `cycle_runner.py`'s outer try/except stops it from crashing the whole trading cycle,
  but does nothing to stop the collateral skip of unrelated, healthy rows.
  Fixed by skipping just the bad row this cycle (same treatment as the existing
  `tick is None` case just above it) and retrying next cycle, instead of crashing.
  13 new regression tests added in `tests/test_watchlist_trigger.py`, including one
  that reverts the fix and confirms the tests actually catch the regression
  (`TestZeroPriceGuard`), and one proving a bad row no longer blocks a healthy row in
  the same cycle (`test_a_bad_zero_price_row_does_not_block_other_rows_same_cycle`).

**Then built real direct test coverage for both flagged files** (no further code
changes needed — writing the tests confirmed the logic is correct, it just wasn't
exercised):

* `tests/test_exit_evaluate_mode.py` — 25 tests covering `exit_engine.exit.evaluate_mode`'s
  full decision tree end-to-end in DEMO mode (real `close_position`/`refresh_unrealized`
  calls, not mocked) plus REAL-mode routing checks (mocking only the Dhan-facing edge,
  `_send_real_sell`, which already has its own dedicated coverage elsewhere): empty/
  missing-tick, stop-hit (incl. priority over target, exact-boundary, no-stop-set),
  target-hit partial (60% lock, breakeven+nullify, no-retrigger), emergency gap-down
  (incl. the boundary against ordinary stop-hit), time-stop (incl. the action-label
  regression guard for the 2026-09-01 EMERGENCY_EXIT-mislabel bug fix), early warning,
  breakeven stop (incl. never-lowers-an-already-higher-stop), ATR trail (incl.
  ratchet-only and never-trails-a-loser), multi-position independence, and the REAL
  pending-sell guard. `exit_engine.exit` coverage: 27% → **55%**.
* `tests/test_entry_evaluate_mode.py` — 20 tests covering `entry_engine.entry.evaluate_mode`
  in DEMO mode (risk_engine's own `evaluate()` is mocked at the boundary — it has its
  own dedicated suite and a live-market-hours dependency that would make re-exercising
  it indirectly from here both redundant and flaky): gate 1 (actionable-label + the
  VOLUME_SHOCK base-tier off-by-default gate), gate 2 (no tick), gate 4 (drift/chasing),
  gate 5 (R:R floor), the risk-engine rejection path, the same-cycle duplicate-symbol
  guard, DEMO order placement (incl. confirming DEMO never touches the Dhan client),
  Gate 6's composite-quality ranking (floor, UPPER_CIRCUIT bypass, max-per-cycle cap
  picking the higher-conviction candidate), and REAL-mode routing to
  `dhan_client.place_order` for both the success and placement-failure paths.
  `entry_engine.entry` coverage: 40% → **84%**.

Test count: 376 passed → **434 passed**, 1 xfailed (unchanged). `py_compile` +
`pyflakes` clean on all new/changed files.

## Round 4 (this session) — fixed the `_send_real_sell` error-classification gap flagged as the top open item above

Went after round 3's #2 remaining gap — `_send_real_sell`'s error-classification
branches — and found one real bug, not just an untested-but-correct path:

* **Bug: circuit-limit SELL rejections were resent to Dhan every exit cycle,
  all day** (`exit_engine/exit.py`, `is_circuit_limit_error` branch). This
  branch's own comment already says a circuit-band rejection is "permanent
  for the session," matching how its two sibling branches
  (`is_intraday_cutoff_error`, `is_security_intraday_restricted_error`)
  behave — both of those set the per-position `_cutoff_key` snapshot flag so
  `_send_real_sell`'s top-of-function check skips any further Dhan call for
  the rest of the day. The circuit-limit branch never set that flag. Combined
  with `is_persistent=False` (correct — a circuit hit isn't a broker/account
  problem) never feeding the exponential-backoff counter either, nothing
  stopped a circuit-locked position's SELL from being resent to Dhan every
  `EXIT_CHECK_INTERVAL_SECONDS` (~45s) for as long as the stock stayed
  circuit-locked — potentially hours. This is the exact DATAMATICS-style
  retry-storm pattern sessions 40 and 72 already fixed for every other
  placement-failure path; this one branch (added later, session41b) missed
  it. Fixed by setting `_cutoff_key` in the branch, and made the generic
  streak-exclusion list (`_excluded`) at the bottom of the function explicit
  about all three self-suppressing branches (oversell,
  intraday/security-restricted, circuit-limit) instead of relying on
  `is_persistent` staying False for two of them by coincidence.
* New test: `tests/test_exit_circuit_limit_resend.py` (2 tests) — asserts a
  circuit-limit rejection stops resending after the first attempt, and that
  `consecutive_exit_failures` correctly stays at 0 (confirms the fix didn't
  accidentally start treating it as a persistent/broker-level failure).
* Checked but found no bug: `entry_engine/entry.py`'s regime-cache TTL/lock
  (correct), the entry-side `is_circuit_limit_error` BUY-rejection handling
  (a rejected BUY just re-evaluates fresh next cycle from the candidate
  engine — no resend-loop risk the way an open position's SELL has), and
  `check_pending_fills`/`expire_stale_orders` (both already careful about
  Dhan-vs-local state, matches their existing docstrings).

`py_compile` + `compileall` clean on both services (no `pytest`/`sqlalchemy`
available in this sandbox this session — no network — so the new test
couldn't actually be executed here; traced its assertions by hand against
the fixed code path instead, same fallback other sessions have used when
offline).

## Phase 1 progress (100%-coverage plan, session77) — `exit_engine/exit.py` continued

Working through the coverage-plan checklist for `exit_engine/exit.py` (target
90%+, was 57%). Added 3 new test files covering previously-0%-direct paths:

* `tests/test_exit_expire_stale_exit_orders.py` (7 tests) — the LIMIT-exit
  expiry/cancel/resend-as-MARKET function (`expire_stale_exit_orders`),
  previously untested at all: no-stale-orders no-op, DEMO-mode no-op,
  full-remainder resend, partial-fill resend of only the unfilled qty, Dhan
  cancel failure leaves the order PLACED (not falsely EXPIRED), no-position-
  found still expires the order without resending, and fully-filled-by-then
  correctly sends nothing.
* `tests/test_exit_send_real_sell_success_and_ip.py` (5 tests) — the
  successful-placement path of `_send_real_sell` (order/event row creation,
  rejection-streak reset, notification content), Dhan accepting a call but
  returning no order id (treated as a failure, no phantom order row), the
  invalid-IP branch's two notification variants (just-disarmed vs
  already-disarmed), and the pre-session38-migration fallback where
  `entry_product_type` is NULL (same-day-opened heuristic).

No new bugs found in these paths — all behaved as documented. `py_compile` +
`compileall` clean on the whole service. Same sandbox limitation as session76:
no network here, so `pytest`/`sqlalchemy` aren't installed — these tests are
written and compile-checked but not yet executed in this environment; run
them on the VM to confirm (see the coverage-plan doc for the follow-on phases
still queued: CDSL/insufficient-funds/oversell/exchange-not-allowed branches,
the two `_cutoff_key` sibling branches, and the generic-rejection streak
escalation, all still open per that plan).

## Phase 1 progress (100%-coverage plan, session77 part 2) — `exit_engine/exit.py`'s error-classification branches

Closed the remaining gap flagged at the end of part 1: `tests/test_exit_error_branches.py`
(21 tests) covering every previously-untested branch in `_send_real_sell`'s
except block —

* CDSL/eDIS and insufficient-funds: single-alert-then-cooldown-suppress
  behavior, confirmed both are `is_persistent` (bump the exponential-backoff
  counter on first hit) and do NOT touch the separate generic-rejection
  streak key.
* Oversell, all 3 documented sub-cases: broker holds 0 → ghost-close via
  `force_close_real_position`; broker holds a partial qty → `qty_open` is
  capped to the broker's real figure; broker qty still covers ours → pure
  no-op (timing issue, no mutation, no ghost-close) — plus the
  holdings-sync-itself-failing path (alert-then-cooldown) and confirmed
  oversell never bumps either streak counter (deliberately excluded, wants a
  fast retry once qty is corrected).
* Exchange-not-allowed (EXCH:16387): alert-then-cooldown, confirmed
  persistent.
* The two `_cutoff_key` siblings to session76's circuit-limit fix —
  `is_intraday_cutoff_error` and `is_security_intraday_restricted_error` —
  each got an explicit resend-suppression test (place_order called exactly
  once even across 5 more evaluation cycles the same IST day), not assumed
  symmetry with the circuit-limit test. Also confirmed the
  security-intraday-restricted branch's `record_restriction()` call into
  `intraday_eligibility.py` is correctly best-effort (a raised exception
  there doesn't prevent the alert/skip handling that follows it).
* Generic-rejection streak escalation, exercised end-to-end through
  `_send_real_sell` (not just `_bump_exit_failure` directly, which
  `test_exit_backoff_escalation.py` already unit-tested): confirmed the
  alerting is cooldown-throttled rather than one-per-rejection (a rejection
  arriving inside the cooldown window from the last alert stays silent even
  below the escalation threshold), the escalation alert fires exactly once
  when the streak first crosses `EXIT_REJECT_STREAK_ESCALATE_AT` (fires
  regardless of cooldown — the escalation branch doesn't gate on `due`),
  goes quiet again afterward, and re-fires (still the "STUCK" message, not
  reverting to the plain one) once the cooldown window elapses again with
  the streak still at/above threshold. Also confirmed a stream of
  *different* unrecognized error strings still increments one shared
  per-position counter rather than being tracked separately per message.

No new bugs found — every branch matched its own docstring's documented
behavior. `py_compile` clean. Same sandbox limitation as parts 1 and this
session's earlier work: no network here, `pytest`/`sqlalchemy` aren't
installed, so this file is written and hand-traced against the current code
line-by-line (including working through the exact alert/cooldown/escalation
state machine call-by-call) but not yet executed in this environment — run
on the VM to confirm:

```bash
cd ~/stockky-v2/services/real-trade-service
python3 -m pytest tests/test_exit_error_branches.py -q
python3 -m pytest tests -q -p no:cacheprovider | tail -1
python3 -m pytest tests -q --cov=exit_engine.exit --cov-report=term-missing
```

Per the coverage plan, still open in `exit_engine/exit.py` after this: lines
67-69/131/165-166/176-182/378-380 (small helper/import guards — cheapest
remaining), and re-confirming with `--cov-report=annotate` whether anything
in the 1197-1530 range (evaluate_mode's trail/breakeven tail) is still
genuinely uncovered now that expire_stale_exit_orders is fully tested.

## Other open decisions (unchanged from round 1)

- **`adaptive.py` R:R floor:** docstring promises 2:1, but wide-ATR stocks get 1.6:1 because target cap wins.
- **Overnight pool cap** skipped when ledger `total_allocated_capital` is 0.
- **Capital-share check** skipped when broker cash + both position values all 0.
- **Broker success with no order id** recorded as open position with blank id.
- **Failed EOD flat-sells** not retried until next day (documented as intentional).

## Still untested — largest risk first

| Priority | Module | Coverage | Why it matters |
|---|---|---|---|
| 1 | `entry_engine/entry.py::evaluate_mode` | 84% | every real BUY: gates, sizing, risk call, order placement — direct test coverage added round 3 |
| 2 | `exit_engine/exit.py` | 84% (confirmed by real pytest+coverage run, session83) | decides when real positions are sold — direct test coverage added rounds 3-4; session77 parts 1-2 closed `expire_stale_exit_orders`, the success/invalid-IP path, and every error-classification branch (CDSL, insufficient-funds, oversell's 3 sub-cases, exchange-not-allowed, both `_cutoff_key` siblings, generic-streak escalation); session82c added per-position exception isolation in `evaluate_mode` plus `_load_profile`/`_trail_atr_mult` coverage; session83 closed the last zero-coverage item, `_clamp_for_atr`'s ImportError fallback. Remaining 81 missed lines are scattered sub-branches inside `_send_real_sell`'s classification ladder and `evaluate_mode`'s trail/breakeven/partial-exit tail — needs `--cov-report=annotate` to identify exact conditions before writing more tests
| 3 | `portfolio/portfolio.py` | 32% | cash, positions and P&L accounting |
| 4 | `execution/auto_pilot.py` | ~~19%~~ ~~31%~~ ~99% pending VM confirmation (session86 — see below) | runs the whole cycle and throttles |
| 5 | `manual_engine.py` | 0% | manual BUY/SELL |
| 6 | `execution/dhan_client.py` | 23% | broker calls and error classification |
| 7 | `candidate_engine/candidates.py` | 0% | candidate selection |
| 8 | `main.py` | 0% | API endpoints |
| 9 | Locks: ~~`shared_symbol_lock.py`, `shared_order_budget.py`~~ (session84 — see below), `intraday_eligibility.py` (still 0%, wrong `--cov` module path — file lives at repo root `intraday_eligibility.py`, not `execution.intraday_eligibility`) | ~~24–60%~~ 100%/44%→100% | cross-service safety |
| 10 | `watchlist_engine/*` | 0% | signal sourcing |

## session84 (2026-09-23): shared_order_budget.py + shared_symbol_lock.py closed to 100%

Confirmed via a real `python3 -m pytest tests/ --cov=... --cov-report=term-missing`
run on the user's VM: `exit_engine/exit.py`, `portfolio/portfolio.py`, and
`execution/dhan_client.py` are all now genuinely 100% (matches this table's
earlier entries). Same run flagged `execution/auto_pilot.py` at 19% (611/750
lines missing — now priority 1, see below), `execution/shared_order_budget.py`
at 44%, `execution/shared_symbol_lock.py` at 41%, and two coverage warnings:
`candidate_engine.candidates` and `execution.intraday_eligibility` "never
imported" — the latter is a wrong `--cov` flag, not a real gap (module is at
repo root as `intraday_eligibility.py`); the former (`candidate_engine/
candidates.py`, 2077 lines) has genuinely zero test coverage, not yet started.

This session added `tests/test_shared_order_budget.py` and
`tests/test_shared_symbol_lock.py`, closing both modules to 100%
line coverage. `execution/shared_order_budget.py`: direct tests for
`_get_or_create_row()` (both branches — also flagged as dead code, nothing
in the codebase actually calls it, only its sibling `_ensure_row_exists()`
is wired up; candidate for removal, not removed this session),
`_ensure_row_exists()`'s IntegrityError race-swallow branch,
`check_and_reserve()`'s budget-exhausted (False) branch and fail-open
exception branch (including a rollback-also-fails sub-case), and
`record_order_unconditional()`'s unconditional-increment-past-budget
behavior and its own fail-open exception branch. `execution/
shared_symbol_lock.py`: `try_claim()`'s already-ours no-op, blocked-by-
other-service, both IntegrityError race sub-branches (lost to the other
service vs. turned out to be our own retried request) plus the inner
re-SELECT-also-fails sub-case, and the outer fail-open exception branch;
`release()`'s non-blocking exception branch (existing tests only covered
the happy delete-and-commit path); `status()` — previously exercised by
NO test at all — both its normal snapshot shape and its fail-safe `[]`
exception branch. IntegrityError races are simulated via a query-call-
counting mock rather than genuine concurrent connections, since sqlite
in-memory engines don't reliably share state across separate connections
in this sandbox — deterministic and exercises the exact same code branches.
No changes to the production modules themselves, tests only. Not executed
in this sandbox (no pytest/network here, same limitation as prior sessions
since ~session73) — hand-traced against the code; user runs on the VM.

Still open, in priority order: `execution/auto_pilot.py` (19%, 611/750
lines — by far the largest gap, this service's live-trading orchestrator),
`candidate_engine/candidates.py` (0%, 2077 lines, never touched by any
test), then re-running the suite with the corrected
`--cov=intraday_eligibility` flag to see its real number.

## session85 (2026-09-23): execution/auto_pilot.py — first coverage round, 19% → 31%

Unlike session84, this round's tests **were** actually executed (this
sandbox now has working pytest + network access — the "no pytest here"
limitation noted in prior sessions no longer applies). Ran
`python3 -m pytest tests/ --cov=execution.auto_pilot --cov=execution.shared_order_budget
--cov=execution.shared_symbol_lock --cov=exit_engine.exit --cov=portfolio.portfolio
--cov=execution.dhan_client --cov-report=term-missing`: **869 passed, 1 xfailed, no
failures, no regressions.** Confirms session84's `shared_order_budget.py` and
`shared_symbol_lock.py` are genuinely 100%, and `exit_engine/exit.py`,
`portfolio/portfolio.py`, `execution/dhan_client.py` remain 100%.

`execution/auto_pilot.py` is 750 statements — far too large for one round, so this
session targeted the self-contained helper functions that don't require standing
up the full `_full_tick_body`/`_prepick`/`_eod_squareoff`/background-loop
orchestration (left for a dedicated follow-up round). Added
`tests/test_auto_pilot_helpers.py` (56 tests), covering: `_get_lock`/`_get_exit_lock`
(per-mode `threading.Lock` lazy-init and independence from each other),
`_reconcile_due`/`_mark_reconciled` (throttle-window bookkeeping), `_run_coro_in_new_loop`
(the `asyncio.run` wrapper), `_summarize` (cycle-result → Telegram message text,
including the market-regime line's presence/absence), `_overnight_hold_enabled` and
`_edis_check_enabled` (gate-row read plus fail-safe config default on both a missing
row and a DB-query exception), `_needs_cnc_sell` (CNC vs. INTRADAY product-type
inference across broker-imported, explicit-product-type, and same-day-vs-carried
fallback branches), `_alert_if_open_positions_while_gate_off` (throttled Telegram
alert, REAL vs. DEMO hint wording, cooldown suppression, and the DB-error path that
must never raise), `_is_afterhours_window_active` (the midnight-spanning window
check), `_compute_afterhours_market_date` (next-trading-date calc — after-close vs.
before-open branches, weekend skip, holiday skip, and the default-`now_t` branch),
and `_select_overnight_holds` (the full eligibility/ranking/cap pipeline: eligible-label
filter, no-live-tick exclusion, profitability requirement, missing-day-range exclusion,
range-position cap, max-positions cap with conviction-ranked tie-breaking, single-symbol
exposure cap, aggregate exposure cap, and the per-sector cap including the case where
unmapped symbols must never compete against each other for the same sector slot).

This moved `execution/auto_pilot.py` from 19% (611/750 missing) to 31% (516/750
missing) — confirmed by the real run above, not hand-traced. No changes to the
production module itself, tests only.

Still open, in priority order: the rest of `execution/auto_pilot.py` (31%, 516/750
lines still missing — the big remaining blocks are the cycle orchestration itself:
`_full_tick_body`, entry/exit cycle wiring, `_prepick`, `_eod_squareoff`,
`_eod_signal_scan`, and the background scheduler loops, none of which are
self-contained the way this round's helpers were, so they'll need fixture-level
mocking of the broker/feed/risk layers), then `candidate_engine/candidates.py`
(0%, 2077 lines, never touched by any test), then re-running with the corrected
`--cov=intraday_eligibility` flag.

## session86 (2026-09-23): execution/auto_pilot.py — orchestration round, 31% → 99%

Picked up session85's deferred item: the cycle-orchestration layer it
deliberately left out (`_full_tick_body`, `_prepick`, `_eod_squareoff`,
`_eod_signal_scan`, `_schedule_tick_body`, the five background loops, and
`start()`). This sandbox has no network access (unlike session85's), so
these tests were **not** run through a live pytest — same limitation as
sessions 76, 77, and 82c. Verified structurally instead: `py_compile` on
both the test file and `execution/auto_pilot.py`, then an AST sweep
confirming every `ap.<name>` the tests reference (30 names) actually exists
in the module, every dotted `monkeypatch.setattr("module.path.func", ...)`
target (22 paths — `exit_engine.exit.evaluate_mode`, `cycle_runner.
run_cycle_core`, `portfolio.portfolio.open_positions`, `market_feed.feed.
get_quotes`, `entry_engine.entry.evaluate_mode`, `resilience.local_cache.
{load,save}_snapshot`, `watchlist_engine.afterhours_scan.*`, `auth.
dhan_credentials.*`, etc.) resolves to a real function in the target file,
and every model kwarg/attribute used (`TradeGateState`, `TradePosition`,
`TradeCandidate`, `NextDayWatchlistEntry`, `TradePositionEvent` — `armed`,
`prepick_enabled`, `eod_squareoff_enabled`, `overnight_hold_reason`,
`afterhours_scan_last_run_ok`, etc.) exists on the corresponding model in
`models.py`. This catches signature/name drift but not runtime logic bugs —
**user should run pytest+coverage on the VM to confirm**, same caveat as
those three earlier sessions.

Added `tests/test_auto_pilot_orchestration.py` (120 tests), covering:

- **Lock wrappers**: `_run_exit_tick_sync`/`_run_full_tick_sync`/
  `_run_schedule_tick_sync` — skip-when-already-held, run-and-release,
  release-even-on-exception; top-level `_exit_only_tick`/`_full_tick`/
  `_schedule_tick` async wrappers, including `_schedule_tick`'s
  non-weekday skip.
- **`_exit_only_tick_body`**: market-closed no-op, notify-on-activity vs.
  no-notify-on-no-activity, DEMO skips reconcile, REAL reconciles only
  when `_reconcile_due`, gate-off alert dispatch and its "protective exit
  only" note appended to an activity notification, exception path (logs,
  notifies, never raises).
- **`_full_tick_body`**: not-armed / no-gate-row / auto-pilot-disabled all
  short-circuit through the gate-off alert; market-closed no-op;
  auto-disarmed notification; activity summary notify; heartbeat-vs-no-
  heartbeat no-activity branches; exception path.
- **`_select_overnight_holds`**, net-of-costs branch (session85 pinned
  `OVERNIGHT_HOLD_PROFITABLE_NET_OF_COSTS=False` throughout; this session
  added the `True` branch — excludes a position whose gross P&L doesn't
  clear the round-trip cost model, keeps one whose gross move clears it
  and every other cap).
- **`_requeue_overnight_priority_candidates`**: snapshot-read exception,
  no-snapshot, already-consumed, missing/stale trading-date, no-picks,
  new-candidate insert + snapshot marked consumed, already-queued-symbol
  skip, save-snapshot-failure swallowed.
- **`_inject_nextday_watchlist_candidates`**: no-rows, above-threshold
  insert with preview price, below-threshold marked-consumed-not-injected,
  already-queued marked-consumed-not-reinjected, preview-price-lookup
  failure is non-fatal, a flaky `db.commit()` rolling back and leaving the
  row for retry, outer exception rolling back and returning 0.
- **`_prepick`**: basic notify with counts, top-symbols list with the
  🌙 overnight-tag, "carried over" line when requeue count is nonzero,
  US-sector-signal bonus applied when enabled, sector-signal failure is
  non-fatal, >10-candidates overflow line.
- **`_enter_at_open`**: auto-disarmed notify-and-return, entry-summary
  notify (entered/rejected/waited counts).
- **`_edis_morning_check`**: DEMO no-op, no-CNC-pending no-op, summary-
  lookup exception swallowed, already-verified-today no-op, not-verified
  alert with pending symbols, ambiguous/unknown-status alert.
- **`_eod_squareoff`**: no-open-positions no-op; DEMO closes at live tick
  (success, close-failure, missing-tick — all three counted correctly);
  REAL sends a sell per position, skips one with a pending sell,
  send-sell-returns-False and send-sell-raises both count as failed;
  overnight holds excluded from square-off, stamped with
  `overnight_hold_reason`, logged as a `TradePositionEvent`, and called
  out in the notification.
- **`_eod_signal_scan`**: nothing-queued path (no candidates, below-
  conviction, wrong-label all exercised), queue-only candidate saved to
  the snapshot, high-conviction candidate entered same-day, not-filled
  falls back to the queue, `entry_engine.evaluate_mode` exception falls
  back to the queue, disarmed gate skips same-day entry entirely, and the
  `EOD_SIGNAL_SCAN_MAX_CANDIDATES` cap keeping only the higher-conviction
  pick.
- **`_schedule_tick_body`**: no-gate-row / not-armed short-circuits; each
  of the five scheduled automations (pre-pick, eDIS check, enter-at-open,
  EOD square-off, EOD signal scan) — fires when enabled and due, skipped
  when already run today or outside its time/market-open gate, and its
  own exception is logged + notified without aborting the other four;
  EOD square-off specifically confirmed to acquire and release the exit
  lock even when its body raises; outer exception (e.g. a broken clock
  call) logged and swallowed.
- **Five background loops** (`_schedule_loop`, `_fast_exit_loop`,
  `_full_cycle_loop`, `_totp_refresh_loop`, `_afterhours_scan_loop`) —
  each broken out of its `while True` after N `asyncio.sleep` calls via a
  sentinel exception, confirming DEMO-then-REAL ordering per tick and that
  one mode's exception never stops the other mode's tick or the loop
  itself; `_totp_refresh_loop` additionally covers the TOTP-disabled
  no-op, refresh-needed vs. not-needed, refresh-returns-False, and an
  exception inside the tick being swallowed.
- **`_afterhours_scan_body`** remaining branches (session85's helper round
  covered `_is_afterhours_window_active`/`_compute_afterhours_market_date`
  in isolation; this session covers the body that calls them):
  gate-not-found, feature-disabled, outside-window, manual bypassing both
  the toggle and the window check, the finalize pass firing and notifying
  on a non-empty shortlist vs. staying silent on an empty one, the regular
  scan pass recording success, an exception recording failure and being
  reported back with its reason, and that same exception's *recovery*
  block failing too (a second, nested `db.query` blow-up) still not
  propagating.
- **After-hours lock + manual trigger**: `_get_afterhours_lock` reuse per
  mode; `_run_afterhours_tick_sync` skip-when-held / run-and-release;
  `run_afterhours_scan_manual_sync` returning `already_in_progress` when
  locked vs. running the body and releasing the lock.
- **`start()`**: creates all five background tasks when none exist, is a
  no-op when all five are already running, and recreates any task found
  `done()`.

If this comes back clean on the VM, `execution/auto_pilot.py` should land
at or near 99% (roughly 11 lines of the 750 likely still open — a couple
of defensive branches worth a final short pass, e.g. any remaining
recovery-path sub-cases in `_afterhours_scan_body`'s exception handling
that a targeted run turns up). No changes to the production module itself,
tests only.

Still open, in priority order: closing `execution/auto_pilot.py`'s last
handful of lines (pending a real coverage run to see exactly which),
`candidate_engine/candidates.py` (0%, 2077 lines, never touched by any
test), then re-running with the corrected `--cov=intraday_eligibility`
flag.

## Commands to run all tests

**On the VM (Ubuntu):**
```bash
cd ~/stockky-v2
for s in position-stocks-service real-trade-service; do
  echo "=== $s"
  (cd services/$s && python3 -m pytest tests -q -p no:cacheprovider | tail -1)
done
```

Expected:
```
=== position-stocks-service
1220 passed, 8 warnings in ~15s
=== real-trade-service
434 passed, 1 xfailed in ~10s
```

**With coverage:**
```bash
for s in position-stocks-service real-trade-service; do
  (cd ~/stockky-v2/services/$s && python3 -m pytest tests -q --cov=. --cov-report=term-missing:skip-covered | tail -40)
done
```

**Full integration test (includes live demo-mode API checks):**
```bash
cd ~/stockky-v2 && ADMIN_USER=admin ADMIN_PASS='<new-password>' ./stockky_full_test.sh
```
