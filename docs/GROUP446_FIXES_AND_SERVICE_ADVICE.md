# Group 446: four small fixes + "which service ON / OFF" advice

Nothing here changes a trading decision, an order, or a switch. No new switch.

## Fixes
| # | Where | What was wrong | Now |
|---|---|---|---|
| 1 | `services/{real-trade,position-stocks}-service/control/kit.py` (both copies, still identical) | The running value reads back canonical (`3`), the catalog default is raw text (`3.0`): a float switch showed PENDING RESTART although nothing differed. | `_desired()` puts both sides into the same canonical form. Test: `scripts/test_control_kit.py::test_float_default_written_as_3_0_is_not_pending_restart`. |
| 2 | `services/market-data-service/session_proof.py` | After a restart AFTER 10:00 nothing was recorded inside 09:15-10:00, so the verdict stayed `unknown` all day and the 10:15 live check FAILed (12:46 restart). | With nothing seen in the window, the first price after the restart (until 15:30) is a late baseline; a later DIFFERENT price is a real move -> `open`. It can only prove `open`, never `closed` (frozen prices stay `unknown`). New status field `late_baseline_symbols`. 3 new tests in `tests/test_session_proof.py`. |
| 3 | `scripts/control_snapshot.py` | Switch titles were cut at 44 characters (the longest are 46). | Cut at 60. Test added. |
| 4 | `services/calibration-runner-service/runner.py` | A job whose input is not there yet (e.g. needs Saturday's weekly calibration) showed FAILED (red). | Shows `attention` (amber) with `needs data first: <what is missing>`. Other failures (script not found, could not start, bad exit code) stay FAILED. Test updated in `tests/test_runner_engine.py`. |

Not changed: the entry time in `daily_trades`. It is `opened_at` of the position, which the record builder only matches to a BUY filled within 2 minutes of it (`_ORDER_SLACK_BEFORE_S`), so the 15:02 / 15:16 times of the carry-over trades are the real entry times, not an import time. No bug was found there; nothing was changed on a guess.

## Service ON / OFF advice (new)
`scripts/service_onoff_advice.py` reads the same closed trades as `daily_trade_report.py` (real-trade net of charges, position-stocks realized) and judges each service on its own record in the window (default 10 days):

| Verdict | Rule |
|---|---|
| KEEP ON | net > 0, still > 0 without its best trade, profit factor >= 1.1 (or no losing trade), fewer than 2 losing days in a row at the end |
| TURN OFF | net < 0 AND < 0 without its best trade AND profit factor < 0.8; or net < 0 with 3 losing days in a row |
| WATCH | anything between (small loss, profit resting on one trade, thin profit factor, bad last days) |
| NOT ENOUGH DATA | fewer than 8 trades or fewer than 3 trading days: no call is made |

It also reports the overlap on the shared Dhan account (same symbol opened by both on the same day), the combined result, and which service has the better record if only one can run. Real-trade entries judged are the bot's own (`--origin system`, default); imported demat holdings are not the service's decisions.

**Unfinished day and `--since` (446b, after the first live run):** run before 15:45 IST on a weekday, the day in progress is left out of the judgement (its trades are shown in a NOTE): it no longer counts as a losing day, a streak day or a trading day. `--include-today` overrides. `--since YYYY-MM-DD` (also a field on the job: "Judge only from date") judges only trades that closed on or after that day: use it after you change switches, so the old setup does not count against the new one. The not-enough-data limits still apply, so a short window gives no call.

It RECOMMENDS ONLY. It never changes a switch or a service: turning one off is your decision (Auto-Pilot / master switch, outside market hours). Exit 0 = nothing to act on, 1 = a service is TURN OFF (the runner shows amber) or no trades were found.

Runs by itself: new job `service_advice` (group "Every trading day") is the second step of the **Daily close pack**, which the timetable runs Mon-Fri at 16:05 IST. Output: `service_advice.txt` + `service_advice.json` in the run's downloads. By hand:

```bash
TOKEN=<admin token> python3 scripts/service_onoff_advice.py --days 10 --out service_advice.txt
python3 scripts/service_onoff_advice.py --from-csv daily_trades.csv      # offline, from the CSV the Daily trades job writes
python3 scripts/service_onoff_advice.py --since 2026-10-19 --days 10     # only the days after a switch change
```
Thresholds: `--min-n 8 --min-days 3 --pf-off 0.8 --pf-keep 1.1 --streak-off 3`. A few days is a small sample: read one TURN OFF with 8-10 trades as "look hard", not as proof.
