# Group 418 - honest holding label, exit-reason attribution, hold-bucket check

Trigger: the hold-time check on 11 closed REAL trades (16 Oct export). None was held 2-5 days (6 same day, 5 next day) while the Buy Sniper card said
"2 to 5 Trading Days", and 2 of the 5 one-day trades had no exit reason. 11 trades prove nothing about returns; they do prove those two defects.
No gate, sizing, stop, exit or risk-engine logic changed.

## 1. Card holding text (api-gateway `buy_sniper.py`)
Why it was wrong: real-trade squares every open position off at `EOD_SQUAREOFF_TIME_IST` (config default 15:00; `.env.example` sets 15:15). Only labels in
`OVERNIGHT_HOLD_ELIGIBLE_LABELS` (default the two VOLUME_SHOCK tiers) that are in profit can skip it. A BUY NOW / PREPARE entry is never carried.
Now `holding_profile(row)` returns the text: `Same day (auto square-off ~15:00 IST)`, or `Same day; carries 1 night only if in profit` for a carry-eligible label
(when `OVERNIGHT_HOLD_ENABLED` is on). Card fields: `holding_duration` (text), `holding_period` (the decision's own estimate, else the same text), additive
`holding_basis {basis, eod_squareoff_ist, overnight_carry}`. The frontend shows the text unchanged.
Env: `SNIPER_HOLDING_MODE=legacy` restores "2 to 5 Trading Days"; `SNIPER_HOLDING_DURATION=<text>` forces one text. Malformed `EOD_SQUAREOFF_TIME_IST` -> 15:00.
Not changed: the card's stop, target and R:R are still daily-bar values; whether a swing-carry path should exist is a policy decision for after the data (item 3).

## 2. Exit-reason attribution (real-trade `trade_records.py`)
Cause: a record's reason came only from the closing SELL order. Positions closed without such an order showed "unknown": holdings-sync `GHOST_CLOSED` (the
broker no longer holds it / it was sold outside the app), DEMO `close_position`, and a SELL fill that did not match the position by time.
Fix: when the order gives no reason, read the position's own events (`CLOSED`, `EXIT_SENT`, `GHOST_CLOSED`; no new column, so old records are repaired too).
`exit_reason_source` says where it came from: `order`, `order_manual`, `event_closed`, `event_exit_sent`, `event_ghost_closed`, `order_without_reason`, `none`.
New labels: `sold_outside_app`, `ghost_closed`, `unspecified_sell` (a SELL filled but nothing recorded why). Truly unattributable stays `None` (report key "unknown").
`load_records` loads the events best-effort: a failure logs a warning and the records still build.
Behaviour change to know about: a filled SELL with a blank reason used to give `None`; it now gives `unspecified_sell` (test updated).

## 3. Hold buckets (`scripts/hold_bucket_check.py`, records)
Records gain `held_days` (trading sessions between entry and exit IST dates, weekends and `tz_utils` NSE holidays skipped; 0 = same day), `hold_bucket`
(`same_day|1_day|2_5_days|6_plus_days`), `return_pct` + `return_basis` (`net` when the size is known, else `gross_price`).
`python3 scripts/hold_bucket_check.py rt_*.json` prints the table you were given, de-duplicated by (mode, position_id) across files, plus how many trades were held 2+ sessions
and how many had no exit reason. A bucket under `--min-trades` (20) gets no verdict; two buckets are compared against same-day only when both have enough trades and the
95% bootstrap interval of the difference excludes zero (fixed seed).
Important: re-running it in 3-4 weeks cannot say anything about 2-5 day holds unless the exit policy lets positions be held. While every non-eligible position is
squared off at ~15:00, the 2-5 day buckets stay empty by construction.

## Tests
`real-trade-service/tests/test_group421_exit_reason_and_hold.py` (new, 38), `scripts/test_hold_bucket_check.py` (new, 10), `api-gateway/tests/test_buy_sniper.py` (updated + `TestHoldingProfile`),
`real-trade-service/tests/test_group290_trade_records.py` (one expectation changed). CI: the new script and test are in the workflow path filters and the repo-scripts pytest line.
`docs/ENV_REFERENCE.md` regenerated. Not run: the full suites of every service, `npm run build`.
