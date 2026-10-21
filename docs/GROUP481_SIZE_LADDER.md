# Group 481 (2026-10-21): evidence-gated size ladder (real-trade-service)

Why: you asked for bigger positions. The Rs 3,000 per-trade cap (`MAX_TRADE_VALUE`) is small against the account, but size is not what loses money and
it does not lower fees: Dhan intraday brokerage is the lower of Rs 20 and 0.03 % per order, a percentage below about Rs 66,700 (group 480 note). A bigger
position scales profit and loss alike, so with no proven net edge it only loses faster. The ladder lets the cap grow by itself, but only after the stored
daily net P&L has earned it, and it falls back as soon as the evidence goes.

## Rules (`entry_engine/size_ladder.py`, stateless, recomputed at most every 5 minutes)
Input: `trade_pnl_daily.net_realized` (gross minus charges) of REAL, newest first; a day counts only when it has orders and a stored net.
* Level 1: the newest 10 captured days (`SIZE_LADDER_MIN_DAYS`) have a positive total and at least 60 % net-positive days.
* Level 2: the newest 20 days meet the same test.
* Brake: when the newest 3 days (`SIZE_LADDER_BRAKE_DAYS`) sum below 0 the level is 0.
* Cap = base x 1.25 ^ level (`SIZE_LADDER_STEP_PCT`): Rs 3,000 -> 3,750 -> 4,687.50. Never below the base; the base is your admin `max_trade_value` override when set, else `MAX_TRADE_VALUE`.
* Applies to REAL automatic entries only (`entry_engine/entry.py`). DEMO and manual orders keep the plain cap. The 25 % concentration cap, capital-share cap,
  cash check, stop-out guards and every other risk gate still apply on top. Any error returns the base cap.

## Today
With the September-October results (net negative) the level is 0: **nothing changes until the numbers earn it.** `GET /review-fixes/status` shows `size_ladder`
(level, the reason, base cap, effective cap).

## Settings
`SIZE_LADDER_ENABLED` (true; `false` = plain cap), `SIZE_LADDER_STEP_PCT` 25, `SIZE_LADDER_MAX_LEVEL` 2 (0 = never grow), `SIZE_LADDER_MIN_DAYS` 10,
`SIZE_LADDER_MIN_POSITIVE_DAYS_PCT` 60, `SIZE_LADDER_BRAKE_DAYS` 3. `SIZE_LADDER_ENABLED` and `SIZE_LADDER_MAX_LEVEL` are protected from auto-calibration; the enable flag is on the calibration page.

## Limits
* trade_pnl_daily holds gross from the day's account counter and charges from the ledger; a day with an estimated charge is counted as stored. Check `/charges/REAL/cumulative`.
* 10 days is a small sample; a lucky fortnight can earn level 1. That is why the step is 25 %, the cap stops at level 2, and the 3-day brake exists.
* Higher size also raises the stop-out loss per trade in rupees; the daily loss limits and stop-out pause are unchanged and apply to the larger size.

## Tests
`real-trade-service/tests/test_group481_size_ladder.py` (12: level rules, brake, cap math, no-order days, DEMO/disabled/error fallback, cache, wiring, shipped defaults).
