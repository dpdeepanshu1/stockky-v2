# Group 348 - double-exit guard and Dhan-based day P&L

Verification: `py_compile` on every changed file; the leg-verification helpers were exercised with a stub script and the new
`tests/test_group348_day_pnl.py` was run directly (pass). **pytest was not run** here (no sqlalchemy/pytest in the sandbox): run
`services/position-stocks-service` tests, especially `tests/test_eod_squareoff.py` (new class `TestExitLegCancelVerification`).

| Issue | Change |
|---|---|
| 13 Oct: MANALIPETC, INFOBEAN, JAYBARMARU, SATIN, FMGOETZE each show 2 sells + 2 buys at Dhan for one position (about -Rs 41 net) | `orders/eod_squareoff.py`: new `ensure_exit_legs_cancelled()`. Before a flat SELL (manual, stagnation, stop-watchdog via `close_position_now`, and the EOD sweep) it re-reads the super order and retries the TARGET/STOP leg cancel until none is live (`LEG_CANCEL_VERIFY_ATTEMPTS`, default 3 rounds, `LEG_CANCEL_VERIFY_WAIT_S` 0.4 s; 0 = off). A leg that stays live never blocks the exit; `close_position_now` cancels it again right after the SELL. Lookup errors fail open. |
| The app showed about -Rs 25, Dhan summed to about -Rs 73 | New read-only `GET /dhan/day-pnl` on position-stocks (port 8006): per-symbol bought/sold, average prices, gross P&L, `net_qty` and a `SHORT` / `HELD` flag, from Dhan's order book (all services, gross of charges). `orders/day_pnl.py`. |

Cause NOT yet confirmed: the earlier log was lost when the containers were recreated at 15:08 IST. The fix covers both likely
causes (a leg still live when the market SELL went out, or a leg that woke up during it). Confirm with Dhan order times for the
five symbols; a double sell with no leg left live afterwards would point elsewhere.

Check tomorrow: `curl -s localhost:8006/dhan/day-pnl | python3 -m json.tool` - `not_flat` must be empty after the close; any `SHORT`
is a double exit.

Deliberately NOT changed (your decision or more data needed): `BROKER_STOP_ENABLED` (real-trade-service), the delisted list
(BIRET, HEG, HDFIRE, UNIVAFOODS, SELMC, SPELS), entry timing / targets / trail / stagnation / position size (one day of data).
