# E108 - T6 Dhan SELL probe, plus the first real pytest run of the position-stocks and real-trade suites (24 Oct 2026)

## Why this item
Every P0/P1/P2 item in the E107 plan was built. The one piece with no code at all was **T6**, the Dhan SELL probe that the live short waits on (the plain-SELL fallback and `SHORT_MODE=live` both depend on its answer). Also, SQLAlchemy/FastAPI/npm installed this time, so P0 item 6 (run the suites) was done for real.

## Built
- `position-stocks-service/orders/sell_probe.py`: ONE 1-share INTRADAY SELL Super Order (MARKET entry, stop +1.5%, target -3%) on a liquid stock; polls Dhan's Super Order list until the entry fills and the legs appear; checks stop and target legs read back within 2 ticks and are BUY legs; cancels the legs; covers with a 1-share MARKET BUY ONLY when Dhan's book says the entry filled and no exit leg is still live; reads the cover back. Verdict `SELL PROBE: PASS | FAIL | SKIPPED`, plus "rules learned" lines (Dhan's answers). Saved to `$CONTROL_DIR/sell_probe_ps.json`.
- Rails: quantity is a constant (1); exact confirm phrase `PLACE_ONE_REAL_SELL_ORDER`; 09:30-14:30 IST on a trading day; armed, kill switch clear, USE_SUPER_ORDER on, no open position or foreign lock on the symbol; one probe at a time; an unclear SELL (timeout) is looked up at Dhan before it is called failed; if the cover cannot be completed the short is adopted as a normal SHORT position row (14:55 cover, stop watchdog) and Telegram alerts; no blind BUY when state is unreadable; lock and budget released at the end.
- Routes: `POST /short/probe` (admin), `GET /short/probe`; `GET /short/shadow` now carries `sell_probe`.
- Switch `SHORT_LIVE_REQUIRES_PROBE` (ships true, calibrator-PROTECTED): `short_live._live_guards` refuses a live short (`SHORT_PROBE_NOT_PASSED`) until a PASS is on record. Decision made from the plan's own T6-before-live rule; one-switch way back = false. No change at shipped values (SHORT_MODE ships shadow).
- Runner: `scripts/dhan_sell_probe.py` and job `dhan_sell_probe` (group ops, market hours only, in no pack and no schedule, sends nothing unless the "I understand this places a real 1-share order" box is ticked; `status_only` reads the last result).
- Frontend: probe line and "Show last probe" button on the Short selling panel (read-only, cannot start the order), plan row, job tab mapping.

## Fixed (found by the real runs)
- `tests/test_e107_gap_vix_decisions.py`: read config values zeroed by conftest; now reads a fresh copy of config.py.
- real-trade `tests/test_db.py`: `_ensure_market_state_columns` (E98) was not in COLUMN_FUNCS, failing 3 migration guards.
- `scripts/test_short_live.py`: stubs `orders.sell_probe` (PASS on record).

## Checked
position-stocks pytest 4233 passed; real-trade full suite earlier 5274 passed + the 3 fixed (test_db 241); calibration-runner 224 passed; scripts test_short_live 47, test_short_exit_paths 75, test_regime_overlay 19, test_control_kit 20; check_control_catalog, check_shared_copies, secrets, requirements drift, merge conflicts, table collisions OK; frontend `npm test` green and `npm run build` (tsc + vite) OK. New tests: `tests/test_sell_probe.py` (28, seven deliberate breakages each caught), runner `tests/test_dhan_sell_probe.py` (8).

## Not done / still yours
- **T6 itself has not run**: it needs your Dhan account in market hours. Run job "Dhan SELL probe" (Run tab, tick the box) between 09:30 and 14:30 on a trading day. Then `SHORT_MODE=live` with `SHORT_LIVE_QTY_CAP=1` is T7.
- api-gateway suite not run (psycopg2-binary / sgmllib3k would not build here); nothing in it was touched.
- After T6, the plain-SELL fallback with a watchdog for plain orders can be built from what Dhan answered.
