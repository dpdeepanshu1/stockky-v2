# E220 - position-stocks: no manual BUY when the market is closed or after the EOD sweep; Swing lane "stock list: [object Object]"

Includes E219.

## What I did
Read `attempt_manual_entry` (orders/entry.py) and its route `POST /positions/manual/buy` (main.py), compared them with `attempt_entry`, and read the EOD sweep's
once-a-day guard (E183). Read the three screenshots of the Real Money Calibration page (Short selling, Swing lane, Reconciliation).

## Findings (fixed)
1. A manual BUY had no time check at all. The automatic path only runs inside the trading loop (market hours, before the sweep), but the manual route checked
   armed / kill switch / budget / max positions / price age and then sent the order. Two cases: (a) a click while the market is closed went to Dhan; (b) a click
   after 15:00, once `eod_squareoff_fired_date == today`, opened an INTRADAY position that the once-a-day sweep would never flatten (only the stop, target or Dhan's own
   15:15+ auto square-off remained). New `orders/manual_buy_window.py`, called in `attempt_manual_entry` before any capital or symbol claim. Fails open on any error.
   Switch: `MANUAL_BUY_WINDOW_GUARD` (default true, Calibration page "Loss-review guards", never changed by auto-calibration). Manual SELL / close is untouched.
2. Swing lane card printed `stock list: [object Object]`. The backend stores the scan's universe as `{source, size}`; the frontend typed it as a string. Now
   an object is shown as `stock list used: N stocks (source)` only when it differs from the current universe, a plain string still prints as before.

## Tests
`tests/test_e220_manual_buy_window.py`: 9 check() tests (run here in a stand-in runner, 9/9) + 3 tests through `attempt_manual_entry` (need real pytest + SQLAlchemy:
refused when closed, refused after the sweep, and nothing reserved / no broker call; allowed inside the window). `frontend/src/swing.test.mjs` +4 assertions (passes on Node 22).
`docs/ENV_REFERENCE.md` regenerated (`gen_env_reference.py --check` is clean).

## Not done on purpose
Nothing was switched from shadow to live. See the reply: Short selling has 3 closed shadow trades (needs 100 on 20 days, net -Rs12 so far, Dhan SELL probe not run),
Swing lane has 0 of 30 closed trades. Both have built-in gates that stay in force.

## Run on your VM
`bash scripts/docker_test.sh position-stocks-service`, `cd frontend && npm test`, then rebuild/restart position-stocks-service and the frontend.
