# E215 - position-stocks Trades CSV: the charges column is filled

**Why:** in the E213 CSV download, `charges` was blank for every position-stocks trade (real-trade rows had it). `/trades/history` never returned a per-trade charges figure, so the Calibration panel had nothing to write.
**Where:** services/position-stocks-service/main.py (`_row_charges`, new `charges` field on each `/trades/history` row), frontend/src/calibrationTrades.ts (reads `r.charges`), frontend/src/calibration.test.mjs, services/position-stocks-service/tests/test_e215_trades_history_charges.py.
**Change:** `charges` = round-trip charges of a settled trade from the same formula the charges ledger books (`charges_ledger.charges_for_trade`, short legs handled), rounded to 2 places; null for unsettled rows or rows without prices/quantity. No DB read, no migration, ledger untouched.
**Tests:** `node --experimental-strip-types src/calibration.test.mjs` passes (run here). `pytest tests/test_e215*` in position-stocks-service NOT run (no pytest/sqlalchemy/fastapi in this sandbox). tsc / vite build / browser NOT run.
**Deploy:** restart position-stocks-service, then download the CSVs again.
