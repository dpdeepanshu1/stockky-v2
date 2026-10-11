# Groups 312-313 (2026-10-11)

## 312 - failures from the group311 test run
- `tests/test_group309_board_runner_http.py`: expected route list now includes `/board/self-check`.
- `tests/test_admin_auth_e2e.py`: the cross-service token test skips when `real-trade-service` is not mounted (a `docker run -v` of one service only).
- Board self-check B (open / avg_price) reports SKIP, not FAIL, while the market is closed; the overall `ok` ignores a skipped B.
- F&O parser ignores Dhan's `...NSETEST-...-FUT` exchange test contracts (`test_rows` counts them).

## 313 - money paths + reproducible builds
1. **Ambiguous BUY placement** (`orders/entry.py` `_recover_ambiguous_buy`, `execution/dhan_client.py`): a timeout / dropped connection / 502-504 after the request left no longer
   releases capital and the symbol claim straight away. Dhan is asked (super-order list, or plain order book when `USE_SUPER_ORDER` is off) for a BUY with the same
   security id and quantity, not dead, created within 180 s and not already one of our positions. Found -> it is tracked as the position (both auto and manual entry).
   Not found / lookup error / clean Dhan rejection / never-connected errors -> the old "failed" handling, unchanged. Dhan does not echo correlationId/tag on this account,
   so a unique id would not help and the tag stays a category (the EOD guard and `/dhan/live-orders` read it).
2. **Board runner** (`screening/board_runner.py`): backfill, snapshot and forward-return fill each have their own guard; a failing backfill no longer stops the other two.
3. **Silent excepts on money paths** now log: critical-alert send failure and the flat-SELL order-book lookup (`orders/eod_squareoff.py`), manual-close restriction record,
   symbol-lock race re-check (position-stocks and real-trade-service). The many `db.rollback()` guards stay silent on purpose.
4. **Builds**: `frontend/Dockerfile` uses `npm ci` with the lockfile; service Dockerfiles install with `constraints.txt` when present.
   `scripts/freeze_constraints.sh` (run on the VM) writes it from the environment the tests pass on. `scripts/docker_test.sh <service>` and
   `scripts/docker_frontend_check.sh` run tests on the Python / Node version in the Dockerfile.

Not changed: Python 3.11 / Node 20 in the Dockerfiles (tests now follow them, not the reverse); no exact versions are written here because the sandbox has no network.
