# E175 - a structure exit was not counted in the exit-cycle tally

Includes E174.

## What I did
Read `exit_engine/exit.py` `evaluate_mode` (lines 1332-1888) line by line. No bug that sells or keeps a position wrongly.
One counting gap found and fixed: when the daily-structure step sells a position (STRUCTURE_EXIT_MODE=exit), neither DEMO nor
REAL added to `full_exits`, so the returned tally and the `EXIT_CYCLE` log line undercounted exits. The sell itself was always sent.

## Change
`exit_engine/exit.py`, structure-exit branch: `full_exits += 1` after a DEMO close and after an accepted REAL sell; a refused REAL sell counts as `held` (as the other branches do).

## Not changed (read, judged fine or a design choice)
- One-share "partial" target sale sells the whole position; the text still says the remainder is trailed (cosmetic).
- Imported holdings: the 10-day time-stop clock starts at import, and the DP-fee guard covers target sales only. Your call if that is wanted.

## Tests
`tests/test_e175_structure_exit_tally.py` (2 tests, DEMO and REAL). NOT RUN in my sandbox (no SQLAlchemy, no network); it compiles.
Run: `bash scripts/docker_test.sh real-trade-service`
