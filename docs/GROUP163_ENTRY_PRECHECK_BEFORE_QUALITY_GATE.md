# Group 163 — affordability and free-slot checks before the quality gate

Item 4 of the second open-market log list (position-stocks-service, `main.py::_run_cycle`).

## What was wrong
The cycle ran the quality gate (up to `QUALITY_GATE_TOP_N` HTTP calls to analysis-intelligence, plus the market
filter before it) on the top candidates every ~13 s, and only afterwards did `attempt_entry()` find out that
nothing could be entered. SATIN and COMSYN were gated over and over although the pool's available capital was
below one share of them. The same waste happened whenever every position slot was already used (`MAX_POSITIONS`).
The existing 120 s capital cooldown only covered symbols that had already reached `attempt_entry()` and failed there.

## The fix
Two cheap checks, both exact lower bounds (they never drop a candidate `attempt_entry()` could have entered):
- **Slots full:** if open positions (`OPEN` + `EXIT_LEGS_REJECTED`, the same count `attempt_entry()` uses) already
  equal `MAX_CONCURRENT_SCALP_POSITIONS`, the cycle ends with `skipped_reason = MAX_POSITIONS_FULL` before the market
  filter and the quality gate. Screening still ran, so `/candidates` stays live; reconcile/EOD are untouched
  (they run earlier). AUTO_PILOT_OFF still takes priority.
- **Unaffordable:** after the existing capital-cooldown filter and before the top-N slice, candidates whose price
  is above the pool's `available_capital` are dropped (a position needs at least 1 share). Other candidates move
  up into the top-N slots. If nothing is left: `skipped_reason = ALL_CANDIDATES_UNAFFORDABLE`. The dropped symbols
  are in `summary["unaffordable_skipped"]` and logged at most once per 5 minutes.
- Both fail open: a ledger/DB read error keeps everyone.

## What this does not do
- It does not catch a candidate whose risk-sized position value exceeds available capital while one share is
  cheaper than the pool (that still goes through the quality gate and then the 120 s cooldown, as before).
- Candidates the quality gate itself rejects are still checked every cycle (unchanged).

## Setting
`ENTRY_PRECHECK=0` restores the old order (blank/invalid = on).

## Tests
New `tests/test_group163_entry_precheck.py` (25 tests). `tests/test_main.py`: three cycle tests now patch
`ledger.get_state` (a fresh test DB has 0 available capital, which the new check correctly treats as unaffordable).

    python3 -m pytest services/position-stocks-service/tests/test_group163_entry_precheck.py services/position-stocks-service/tests/test_main.py -q

Rebuild position-stocks-service.
