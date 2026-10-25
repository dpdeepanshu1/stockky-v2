# E134 - swing lane: every scan records which stock list it ranked (2026-10-25)

Follow-up to E133. The built-in 118-name list and your own `SWING_UNIVERSE_SYMBOLS` list give different evidence, but nothing marked which scan used which.

- `swing/engine.run_scan(..., universe=None)` stores `{source, size}` in the scan run's saved detail and returns it; `latest_scan()` reads it back (`None` for scans saved before this change).
- The scan thread passes `{source: env | rank_default | builtin, size}`.
- `GET /swing/status` shows `scan_universe` (the newest scan's list) next to `universe_source` (what the next scan would use).
- No switch, no order, no table change (the detail is JSON in the existing run row).

Use it: when `scan_universe.source` changes (builtin -> env after you set the S2 list), the shadow ledger mixes two universes. Read `/swing/snapshots` or `/swing/ledger` from the switch date onward for the like-for-like verdict. The report itself does not split by universe.

Checked here: `tests/test_swing_lane.py` 27 pass (1 new), momentum tests pass, guards pass (control catalog, shared copies, merge markers). Not run: pytest, SQLAlchemy / FastAPI paths (unchanged from E132/E133).
