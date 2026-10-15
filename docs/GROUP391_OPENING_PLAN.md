# Group 391 - 09:10 opening plan (position-stocks)

## Problem
At 09:15 a symbol has no 5-minute bars. `orders/adaptive.py` then fell through to the legacy path: stop floor `MIN_STOP_PCT` (2%), target about 4.4%, the same for every name at the open (the group 340 note already named this for COFFEEDAY, SYNCOMF, CLEANMAX).

## Change
- `screening/opening_plan.py` (new): at `OPENING_PLAN_TIME_IST` (09:10) `build()` reads each F&O-universe symbol's previous-session daily ATR % from `screening/prev_day.py` (cache only; a miss starts prev_day's own background fetch). The expected 5-minute bar range = daily ATR % x `OPENING_PLAN_BAR_FRACTION` (0.25). With no ATR (fewer than 15 candles) the mean daily range % is used; with neither, the symbol has no plan.
- `orders/adaptive.py`: when live bars give nothing (`_bar_atr_pct` is None), `opening_plan.bar_atr_pct(symbol)` supplies the bar ATR and the SAME bar-level rules run (stop = bar ATR x 1.3 clamped 0.8-2.0%, target = stop x 1.8 capped 3.5%, then the scalp target cap). Live bars always win. A symbol missing from the plan is read from the prev_day cache (`peek`, never a fetch).
- `main.py`: the scan loop builds the plan once per IST day, from 09:10 until `OPENING_PLAN_USE_UNTIL_IST` (10:00), right after the 09:08 snapshot hook.
- Nothing on the entry path makes a network call. A symbol with no previous-day data keeps the legacy path.

## Env
`OPENING_PLAN_ENABLED` (1), `OPENING_PLAN_TIME_IST` (09:10), `OPENING_PLAN_USE_UNTIL_IST` (10:00), `OPENING_PLAN_BAR_FRACTION` (0.25). `OPENING_PLAN_ENABLED=0` restores the old behaviour.

## Honest limits
- 0.25 is an assumption (a typical 5-minute range as a share of the daily ATR), not a backtested value; opening bars are usually wider than the day's average, so check the journal's stop-outs in 09:15-09:45 and tune the fraction.
- A daily ATR lags a regime change.
- The real-trade service is untouched: it already sizes its stop from the daily ATR (`tick.atr`), so it did not have the bar-history gap.

## Tests
`tests/test_group391_opening_plan.py` (16): expected bar maths, window / due rules, build and lookup, the adaptive hook (plan replaces the 2% stop, clamps hold, no plan or outside window keeps legacy, live bars win, plan error falls back). position-stocks suite 3786 passed.

## Still open (from the opening plan)
Opening-specific protection (50% size, SL-M, pause after 2 stop-outs) and the replay / validation phase. Update: protection is done in groups 392 (position-stocks) and 394 (real-trade); the replay tool is group 393; group 395 tags each stop's source (`stop_basis`) and exports the stop figures so this plan's stops can be assessed from a ledger.
