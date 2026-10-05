# Group 166 - repair reports the daily-loss limit; clock-dependent tests fixed (position-stocks-service)

## 1. Loss-limit report on the repair route
After a repair, today's corrected P&L can be past the daily-loss limit even though the kill switch never saw it.
`POST /positionstocks/reconcile/repair-closed` now adds to its response (when any row was examined):
`daily_loss_pct`, `daily_loss_limit_pct` (`MAX_DAILY_LOSS_PCT_OF_POOL`), `exceeds_daily_loss_limit`,
`kill_switch_tripped`. REPORT ONLY: the repair never trips or clears the kill switch; if `exceeds_daily_loss_limit` is
true after `?apply=true`, use `POST /kill` yourself if you want entries blocked for the rest of the day.
On a dry run the figures describe the ledger as it stands now (before the correction); read them from the apply
response. A failure while building the report is swallowed and the keys are simply absent.

## 2. test_trade_gates.py no longer depends on the wall clock
Five tests built "closed N minutes ago" rows and compared IST dates, so they failed whenever the machine clock was
within a few hours after IST midnight (they failed the same on the group 162 zip). The module now pins the clock to 12:00 IST
today for `trade_gates`, `tz_utils` and `entry`, and builds rows from that pinned time. Production code unchanged.

Tests: +3 repair-report tests; all 26 trade-gate tests pass at any hour (2551 passed in the whole service).
reconcile.py and ledger.py stay at 100%. Rebuild position-stocks-service (route change only; no trading rule changed).
