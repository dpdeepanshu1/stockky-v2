# Group 268 - entry window opens at 09:15, opening-quality gate until 10:00

## Why
Scalp service, last 14 days (33 trades): entries before 10:30 won 1 of 10 (-Rs 154 gross); from 10:30 on, 11 of 23 (+Rs 116).
Small sample. A fixed clock block also throws away the good opening trades, so both services now start at 09:15 but every
automatic entry between the open and a settle time (default 10:00) must pass an opening-quality gate. After the settle time
the normal rules apply unchanged.

## position-stocks-service (scalper)
- `ENTRY_NO_BEFORE_IST` default 09:30 -> 09:15 (main.py).
- New `screening/opening_gate.py`, wired in `orders/entry.py` right before the price guard. Checks, all must pass:
  at least 5 min since the open; gap (day open vs previous close) between -1.0% and +3.0%; price at/above the day open and the
  previous close; day-range position below 0.88; opening range (09:15-09:30, from the tick buffer) once complete: price in its
  upper half and not more than 1.0% above its high; Nifty vs its day open and vs previous close both >= 0.
- Fails CLOSED while active: missing exchange day stats, previous close, Nifty reading, fewer than 10 opening-range ticks
  (e.g. after a restart), or any internal error skips the entry (reason `OPENING_GATE:<CODE>:...` in the candidate log).
- The ENTERED candidate-log row now ends with ` gap=.. vs_open=.. range_pos=.. or_pos=.. nifty_prev=..` at any time of day,
  so winners and losers can be compared later to tune the thresholds.
- Env: `OPENING_GATE_ENABLED` (1), `OPENING_GATE_SETTLE_IST` (10:00), `OPENING_GATE_MIN_MINUTES_AFTER_OPEN` (5),
  `OPENING_GATE_MAX_GAP_UP_PCT` (3.0), `OPENING_GATE_MAX_GAP_DOWN_PCT` (1.0), `OPENING_GATE_MAX_RANGE_POS` (0.88),
  `OPENING_GATE_OR_MINUTES` (15), `OPENING_GATE_MIN_OR_POS` (0.5), `OPENING_GATE_MAX_ABOVE_OR_HIGH_PCT` (1.0),
  `OPENING_GATE_MIN_OR_TICKS` (10), `OPENING_GATE_NIFTY_MIN_VS_OPEN_PCT` (0), `OPENING_GATE_NIFTY_MIN_VS_PREV_PCT` (0).
  A 0 on a gap or range number disables just that check.

## real-trade-service
- `OPENING_ENTRY_NOT_BEFORE_IST` default 09:30 -> 09:15 (the group 220 guard now only matters if you raise it again).
- New `entry_engine/opening_gate.py`, called at the top of the candidate loop in `evaluate_mode()`. Its tick has no day-open
  price, so "gap" is price vs previous close. Passes only if: at least 5 min since the open; change vs previous close within
  -0.5% .. +3.0%; day-range position below 0.88.
- A candidate that fails is NOT consumed or rejected: it stays queued and is checked again next cycle (like the group 220
  guard). Missing previous close or day range while active holds it back (fail closed).
- Covers the modes in `OPENING_ENTRY_GUARD_MODES` (REAL, DEMO by default; set `REAL` to keep DEMO as a control).
- Env: `OPENING_GATE_ENABLED` (true), `OPENING_GATE_SETTLE_IST` (10:00), `OPENING_GATE_MIN_MINUTES_AFTER_OPEN` (5),
  `OPENING_GATE_MIN_CHANGE_PCT` (-0.5), `OPENING_GATE_MAX_CHANGE_PCT` (3.0), `OPENING_GATE_MAX_RANGE_POS` (0.88).

## Not done (from the plan)
- Previous-day candle checks and the shadow mode were left out of group 268; shadow mode came in group 269 and the
  previous-day checks in group 270 (docs/GROUP270_OPENING_GATE_PREVIOUS_DAY_CHECKS.md).
- Real-trade has no day-open and no Nifty check in this gate (its regime gate already covers Nifty).
- The thresholds are judgement calls from a 33-trade sample, not measured.

## Tests
position-stocks: `tests/test_group268_opening_gate.py` (new, 26) + 2 wiring tests in `tests/test_entry.py`; `tests/conftest.py`
switches the gate off for older tests. Full suite 2955 passed, 1 failed (`test_group210_symbol_lock_sweep::test_throttled_to_one_pass_per_interval`, fails the same on the group 267 upload).
real-trade: `tests/test_group268_opening_gate.py` (new, 15); group 220 test expectation changed 09:30 -> 09:15; conftest switches
the gate off for older tests. Full suite 3821 passed, 1 skipped, 1 error (`test_group172_volume_shock_history_reasons`, same error on the group 267 upload).
Not run against a live feed or broker.
