# group150 (2026-10-05) - intraday news tick no longer fails with ORA-00908 on Oracle

Cumulative on group149. Rebuild real-trade-service: `docker compose build real-trade-service && docker compose up -d`.

## Cause (VM log after the group 149 redeploy)
`real-trade-service` logged `ERROR intraday news tick failed (non-fatal)` with `ORA-00908: Unexpected token 1 after IS or IS NOT` and the SQL `WHERE trade_gate_state.afterhours_news_scan_enabled IS 1`. `_intraday_news_body` (execution/auto_pilot.py) filtered with `.is_(True)`. Oracle has no native BOOLEAN in this setup, so SQLAlchemy renders that as `IS 1`. SQLite accepts it, which is why the SQLite-backed tests passed. Effect: the market-hours news loop (every 900 s, 09:00-15:45 IST) could never read the feature toggle on the VM, so it never ran. It was non-fatal (caught and logged), and no trading path uses it.

## Fix
`.is_(True)` -> `== True` (renders `= 1`). Only occurrence in `services/` (searched all non-test Python for `.is_/isnot/is_not(True|False)`).

## Tests
`services/real-trade-service/tests/test_oracle_boolean_filters.py` (4 tests): the filter compiles to `= 1` under the Oracle dialect; `.is_(True)` really compiles to `IS 1` (documents the bug); the source uses the safe form; a guard that fails if any service source uses `.is_/isnot/is_not(True|False)`. Verified the new tests fail on the old code. real-trade suite: 2718 passed, 1 skipped, run here with 3 modules excluded (`test_admin_auth`, `test_main_routes_core`, `test_main_routes_trading` do not import in this sandbox: fastapi version mismatch).

## Verify on the VM during market hours
`docker compose logs real-trade-service | grep -i "intraday news"` should show the loop running with no ORA-00908. The tick still only does work when the after-hours news toggle is on for at least one mode.

## Other lines in the same log (no code change)
- `AngelOne quote(batch) returned HTTP 403 ... exceeding access rate` (once, at boot): already cooled down and log-throttled.
- `bootstrap cookies weak (403)` from NSE: same VM-IP block as before, bhavcopy fallback active.
- Still open from the earlier review: AngelOne feed shows a 250-symbol then a 491-symbol "background thread started" (check the old thread is replaced), and the afterhours scan picked `IT`/`ACE` as symbols.
- The pasted log is cut off mid-line at the end; nothing after that point was reviewed.
