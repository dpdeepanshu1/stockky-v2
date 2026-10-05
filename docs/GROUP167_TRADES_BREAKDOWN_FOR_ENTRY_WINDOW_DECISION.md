# Group 167 - read-only trade breakdown to decide the entry window (position-stocks-service)

The entry window (09:30-14:30, `ENTRY_NO_BEFORE_IST` / `ENTRY_NO_AFTER_IST`) is the one item left from the group 162
review, and it was left alone because the evidence was weak. The trades themselves can settle it, so this adds a
report and changes no trading behaviour.

## `GET /positionstocks/trades/breakdown?days=3` (no auth, like /trades/history)
Closed trades with a realized P&L (OPEN / EXIT_LEGS_REJECTED / ERROR excluded), over the last `days` days (1-30; rows
are only kept for `TRADE_HISTORY_RETENTION_DAYS`, default 3), grouped three ways:
- `by_entry_time_ist`: 30-minute IST buckets of the entry time (09:30, 10:00, ...)
- `by_window`: scan window that produced the entry (1m/5m/15m/60m)
- `by_exit_status`: TARGET_HIT, STOP_HIT, STAGNATION_EXIT, ...
Each group: `trades, wins, win_rate_pct, total_pnl, avg_pnl, avg_max_gain_pct, avg_max_drawdown_pct`
(the last two from max/min price seen while open; null when unknown). Plus `overall`.

## How to use it for the window
Call it after a few trading days (on the VM: `curl https://stockky.duckdns.org/positionstocks/trades/breakdown?days=3`).
Look at the first and last buckets: if 09:30-10:00 or the 14:00-14:30 buckets are consistently negative with a
reasonable number of trades (5+ each; fewer is noise), paste the numbers and the start/end times you want, and the
window change is a one-line default. Run the group 164-166 repair first so the P&L here uses real fills.

Tests: +6 in tests/test_review_stats.py (module at 100%), +1 route test in test_main.py; 2558 passed.
No trading rule changed. Rebuild position-stocks-service.
