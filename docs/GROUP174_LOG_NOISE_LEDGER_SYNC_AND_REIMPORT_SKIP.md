# Group 174 — log noise: ledger sync lines and the re-import skip line

Item 8 of the open list. Log output only; no trading rule changed.

## What was noisy
- **position-stocks-service** `capital/ledger.py`: `sync_from_broker()` runs every trading cycle (~13 s) and logged
  `ledger: synced from broker ...` and `ledger.sync_peer_pnl: ... cached` each time, mostly with identical figures.
- **real-trade-service** `portfolio/portfolio.py`: `import_broker_holdings()` runs every reconcile cycle. A lagging
  holdings feed (LATENTVIEW) repeated `skipping re-import of ...` every cycle for up to 24 h.

## Fix
- Each of the three lines logs when its figures change (or on first sight), and otherwise at most once per interval.
- Ledger lines: `LEDGER_SYNC_LOG_EVERY_S` (default 300). Re-import skip line: `RECENT_CLOSE_SKIP_LOG_EVERY_S`
  (default 1800), keyed per (symbol, closed_at).
- `0` = log every time (old behaviour). Blank or invalid = default.
- Warnings and errors (failed funds call, no usable balance, unreachable peer) are unchanged. The skip itself is unchanged.

## Not changed
- "About 10 endpoints polled per tab": that is the browser's polling, not a log setting. `/health` 200 lines are already
  dropped (group 2026-10-04). Cutting the other polls changes how fresh each tab is, so it needs your call on which tabs.

## Tests
- `position-stocks-service/tests/test_group174_ledger_sync_log_throttle.py` (7 + 4 parametrised)
- `real-trade-service/tests/test_group174_recent_close_skip_log_throttle.py` (4 + 4 parametrised)
- Ran in the sandbox: both new files, `test_ledger_coverage.py` (98 passed with the new file), `test_portfolio.py` and
  `test_portfolio_remaining_coverage.py` (84 passed).

Rebuild position-stocks-service and real-trade-service.
