# E213 - Calibration page Trades CSV: carries what a loss analysis needs

**Why the CSV exists:** the Trades panel on Real Money Calibration builds `trades_<service>_<date>.csv` live from `/trades/history` (position-stocks) and `/positions/REAL/records` (real-trade) for loss analysis. It only had 10 columns (no planned stop, prices, excursion or slippage), so a loss like 2026-10-26's (-70.89, four quick stop-outs = -99.65) could not be explained from the file.
**Where:** frontend/src/calibrationTrades.ts, calibration.test.mjs.
**Changes:**
- 12 new columns after the unchanged first 10: side, entry_price, exit_price, stop_pct (stop the trade was opened with), target_pct, r_mult (return / stop_pct; -1 = lost one stop), best_gain_pct, worst_dd_pct, entry_slip_pct (+ = worse fill), exit_slip_pct (+ = better; position-stocks: stop exits only, vs the last stored stop), charges, stop_basis. Empty when the service did not record it.
- real-trade ghost records (P&L exactly 0 and no return %, e.g. HDFCLIFE 2026-10-20) are dropped from the panel and CSV (the Python report already did).
- position-stocks return_pct had the wrong sign for shorts; excursions are flipped for shorts.
**Tests:** `node --experimental-strip-types src/calibration.test.mjs` passes (run here). tsc / vite build / browser NOT run.
