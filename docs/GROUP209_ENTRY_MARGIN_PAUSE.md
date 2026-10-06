# Group 209 - margin rejections pause entries, failed placements rest the symbol (position-stocks-service)

Cumulative on group 208. Item 15 of the open list, second half (group 192 fixed the HEGAM loop for "rejected after
acceptance" intraday/circuit rejections). Rebuild position-stocks-service only.

## What was still wrong after group 192
1. A margin / insufficient-funds rejection is about the ACCOUNT, not the symbol. The next candidate hits the same
   RMS wall, so every cycle kept firing orders that could not fill. Group 192's per-symbol guard also counted those
   rows against the symbol (30 min cooldown, then out for the day) although the symbol did nothing wrong.
2. A BUY that failed at placement for an unclassified reason (not restricted, cut-off, funds or circuit) was
   retried for the same symbol on every cycle.

## Changes
- New `orders/entry_pause.py`: small in-process pause state (global pause + per-symbol rest). Cheap, never raises,
  lost on restart (cost: one extra attempt, never a missed exit).
- `orders/entry.py::attempt_entry`: a synchronous INSUFFICIENT_FUNDS failure pauses ALL new entries for
  `ENTRY_MARGIN_PAUSE_MINUTES` (default 5); a generic `ORDER_FAILED` rests that symbol for
  `ENTRY_ORDER_FAILED_COOLDOWN_MINUTES` (default 5). The pause check runs right after the order-budget guard, before
  the symbol lock, and logs `ENTRY_MARGIN_PAUSE:` / `ENTRY_ORDER_FAILED_COOLDOWN:` skip reasons.
- `orders/reconcile.py::_learn_from_entry_rejection`: a REJECTED entry whose reason is a margin shortfall (the
  Super Order accepted, then rejected by RMS) triggers the same global pause. It is not recorded as a restricted
  symbol. A CANCELLED entry never pauses.
- `orders/entry.py::_rejected_entry_reject`: dead entries whose stored reason is a margin shortfall no longer count
  toward the symbol's cooldown or day cap.
- `config.py`: `ENTRY_MARGIN_PAUSE_MINUTES`, `ENTRY_ORDER_FAILED_COOLDOWN_MINUTES` (env, 0 disables each).
- `tests/conftest.py`: autouse fixture resets the pause state between tests (without it one test's failed order
  rested the symbol for the next test).

## Not changed
Intraday-restricted, cut-off and circuit-limit handling (group 192 / earlier) is untouched. Exits are never paused.

## Tests
`tests/test_group209_entry_pause.py` (14 cases). The sandbox has no pytest/sqlalchemy and no network: the pure
`entry_pause` logic was run for real with a fake clock and passed; all changed files compile; the DB-backed cases and
the existing position-stocks suite (including `test_entry.py`'s parametrised rejection cases) still need your
`bash run_tests.sh` on the VM.
