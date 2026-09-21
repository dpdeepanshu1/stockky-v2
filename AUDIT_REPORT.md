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

## Other open decisions (unchanged from round 1)

- **`adaptive.py` R:R floor:** docstring promises 2:1, but wide-ATR stocks get 1.6:1 because target cap wins.
- **Overnight pool cap** skipped when ledger `total_allocated_capital` is 0.
- **Capital-share check** skipped when broker cash + both position values all 0.
- **Broker success with no order id** recorded as open position with blank id.
- **Failed EOD flat-sells** not retried until next day (documented as intentional).

## Still untested — largest risk first

| Priority | Module | Coverage | Why it matters |
|---|---|---|---|
| 1 | `entry_engine/entry.py::evaluate_mode` | ~38% of file | every real BUY: gates, sizing, risk call, order placement |
| 2 | `exit_engine/exit.py` | 27% | decides when real positions are sold |
| 3 | `portfolio/portfolio.py` | 32% | cash, positions and P&L accounting |
| 4 | `execution/auto_pilot.py` | 19% | runs the whole cycle and throttles |
| 5 | `manual_engine.py` | 0% | manual BUY/SELL |
| 6 | `execution/dhan_client.py` | 23% | broker calls and error classification |
| 7 | `candidate_engine/candidates.py` | 0% | candidate selection |
| 8 | `main.py` | 0% | API endpoints |
| 9 | Locks: `shared_symbol_lock.py`, `shared_order_budget.py`, `intraday_eligibility.py` | 24–60% | cross-service safety |
| 10 | `watchlist_engine/*` | 0% | signal sourcing |

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
376 passed, 1 xfailed in ~6s
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
