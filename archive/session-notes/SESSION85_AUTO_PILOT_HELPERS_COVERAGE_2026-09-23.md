# Session 85 (2026-09-23): execution/auto_pilot.py — first coverage round

## Summary

`execution/auto_pilot.py` (750 statements) was the largest remaining coverage
gap in real-trade-service at 19% (611 lines missing) — flagged priority 1 in
session84's audit. This session started closing it, targeting the
self-contained helper functions first rather than the full cycle
orchestration, which needs a dedicated round of its own.

**This round's tests were actually executed** — this sandbox has working
pytest and network access, unlike prior sessions (~session73 onward) which
were hand-traced against the code with a note that no pytest/network was
available. That limitation no longer applies.

## Result

```
python3 -m pytest tests/ \
  --cov=execution.auto_pilot --cov=execution.shared_order_budget \
  --cov=execution.shared_symbol_lock --cov=exit_engine.exit \
  --cov=portfolio.portfolio --cov=execution.dhan_client \
  --cov-report=term-missing
```

```
Name                               Stmts   Miss  Cover
----------------------------------------------------------------
execution/auto_pilot.py              750    516    31%   (was 19%, 611 missing)
execution/dhan_client.py             382      0   100%
execution/shared_order_budget.py      55      0   100%
execution/shared_symbol_lock.py       56      0   100%
exit_engine/exit.py                  492      0   100%
portfolio/portfolio.py               430      0   100%
----------------------------------------------------------------
869 passed, 1 xfailed, 5 warnings in 38.03s
```

No failures, no regressions. Confirms session84's `shared_order_budget.py`
and `shared_symbol_lock.py` are genuinely 100%, and the three previously
100%-covered modules remain 100%.

## What was added

`tests/test_auto_pilot_helpers.py` — 56 tests, no production code touched.

| Function | What's covered |
|---|---|
| `_get_lock` / `_get_exit_lock` | Per-mode `threading.Lock` lazy-init, reuse, and independence between entry and exit locks |
| `_reconcile_due` / `_mark_reconciled` | Throttle-window bookkeeping — never-reconciled, just-reconciled, interval-elapsed |
| `_run_coro_in_new_loop` | Trivial `asyncio.run` wrapper runs a coroutine to completion |
| `_summarize` | Cycle-result → Telegram message text: no-activity message, entries/fills/candidates activity flag, exit-activity line, emergency-exits-alone activity, market-regime line present/absent |
| `_overnight_hold_enabled` / `_edis_check_enabled` | Gate-row read (True/False) plus fail-safe config default on both a missing row and a DB-query exception |
| `_needs_cnc_sell` | CNC vs. INTRADAY product-type inference: broker-imported override, explicit INTRADAY/MIS/CNC, and the no-product-type same-day-vs-carried fallback |
| `_alert_if_open_positions_while_gate_off` | No-op with no open positions, alert sent with REAL wording, DEMO uses different hint text, cooldown throttling suppresses repeat alerts, DB error never raises |
| `_is_afterhours_window_active` | Midnight-spanning window check: active late evening, active early morning, inactive during market hours |
| `_compute_afterhours_market_date` | After-close targets tomorrow, weekend skip (Friday → Monday), holiday skip, before-open targets today, before-open-on-weekend skips to next weekday, default `now_t=None` branch |
| `_select_overnight_holds` | Full eligibility/ranking/cap pipeline: disabled/no-positions/no-eligible-label short-circuits, no-live-tick exclusion, profitability requirement, missing-day-range exclusion, range-position cap, single eligible position kept, max-positions cap with conviction-ranked tie-breaking, single-symbol exposure cap, aggregate exposure cap, per-sector cap, and unmapped-sector symbols never competing against each other for the same slot |

## Deliberately left for a follow-up round

`_full_tick_body`, `_prepick`, `_eod_squareoff`, `_eod_signal_scan`, and the
background scheduler loops. These aren't self-contained the way this round's
helpers were — they need fixture-level mocking of the broker/feed/risk
layers to exercise properly, which is a bigger lift and deserves its own
session rather than being rushed into this one.

## Next up

1. `execution/auto_pilot.py` — remaining 516/750 lines (the cycle
   orchestration itself, see above)
2. `candidate_engine/candidates.py` — 0%, 2077 lines, never touched by any
   test
3. Re-run coverage with the corrected `--cov=intraday_eligibility` flag
   (repo-root module, not `execution.intraday_eligibility`) to get its real
   number instead of the "never imported" warning
