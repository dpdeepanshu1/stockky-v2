# E185 - position-stocks: the same skip lines were logged every cycle (log flood); why no stock was bought on 2026-10-26

Includes E184.

## What I read
The compose log of 2026-10-26 (restart 03:59 UTC = 09:29 IST, ends about 04:11 UTC = 09:41 IST; 388 position-stocks lines of 4,534), `main.py`'s pick loop,
`orders/adaptive.py::apply_target_cap`, `feed/bar_backfill.py`, `screening/opening_plan.py`.

## Why no stock was bought (not a fault)
Every shortlisted pick was refused by an entry gate, cycle after cycle; nothing errored. In ~12 minutes: GAP_UP / GAP_DOWN (SESHAPAPER +9.85% > +3%, INDORAMA
-1.18%, TECHM -1.65%, BIRLACABLE), NEAR_DAY_HIGH (range_pos 0.93-1.00 against the 0.92 floor: MRPL, GNA, GRAPHITE, TANLA, JPPOWER, 63MOONS, ...),
EXTENDED_ABOVE_OR (SWSOLAR), BELOW_PREV_CLOSE / BELOW_OPEN, PREV_DAY_WEAK_CLOSE, STOP_TOO_TIGHT (JPPOWER: 0.70% stop < 0.30 x ATR 4.71%). It was a broad
up-morning where most names sat at their day high, so "do not chase the high" refused them. The gates are strategy settings; I did not loosen any.
Two smaller contributors: OFSS was priced above the scalp pool (Rs 8,775.60 = half of the Rs 17,551 Dhan balance), and MRPL / TDPOWERSYS were refused once as
EVENT_CHECK_UNAVAILABLE because the news service was slow (it was fetching 6 feeds per symbol; Yahoo's connection pool was full) - they were re-tried next cycle.
The opening-window gates (OPENING_GATE) end at 10:00 IST; only RANGE_GATE and the quality gates apply after that.

## The log problem (fixed)
The pick loop runs about every 15 s and a stock that fails a gate fails it again, so the same line was written each time: "SESHAPAPER skipped (OPENING_GATE:GAP_UP
...)" 13 times in 10 minutes, INDORAMA / BIRLACABLE 6 times, and a "no shortlisted pick cleared its entry gates" summary on nearly every cycle (about 40 of the
388 lines). `adaptive: target cap X -> Y, stop A -> B` (42 lines) is logged for every candidate on every scan with identical numbers.

## Change
- `main.py`: `_skip_is_new(symbol, skip)`. A skip is logged at INFO the first time, when the symbol's gate CHANGES (e.g. RANGE_GATE -> OPENING_GATE), or after
  `_SKIP_LOG_REPEAT_S` (300 s); otherwise at DEBUG. The "no shortlisted pick" summary is INFO only on a cycle where at least one skip line was INFO.
  Behaviour is untouched: `summary["shortlist_skips"]`, `_mark_pick` and the dashboard still get every skip.
- `orders/adaptive.py`: the target-cap line is INFO once per distinct set of numbers per `_CAP_LOG_REPEAT_S` (300 s), else DEBUG. The levels returned are unchanged.
- Left as is: `board backfill` lines (each line is a different batch of symbols, 14 bars each = the minutes missed before the restart; the 15-minute retry already
  stops repeats), the `ledger: synced` lines (every 5 min), pullback ARMED lines (once per arming).
- No new environment variable, column or migration.

## Noted, not changed
- `[opening-plan] built: 0 of 150 symbols have a previous-day volatility` after a restart inside the 09:10-10:00 window: the plan is marked built once with an
  empty table, but `bar_atr_pct` falls back to the previous-day cache (`prev_day.peek`), so gating still has the value. The line is misleading, not harmful.
- Other services' noise in the same log (real-trade `VOLUME_SHOCK CANDIDATE REJECTED`, 302 lines; event-tracker fetch lines, ~1,200) is outside this change.

## Tests
`tests/test_log_dedupe_e185.py` (7 tests: first time, same gate with moving numbers, different gate, stale repeat, per-symbol, cap line). Not run with pytest here (not
installed); the helper logic was run directly and passed.
