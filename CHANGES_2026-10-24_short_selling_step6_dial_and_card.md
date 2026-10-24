# Short selling step 6: the Market Weather short dial, the shadow card, and a protected-list fix (Position Stocks only)

**No order can be placed by anything in this step.** `SHORT_MODE` still has no `live` value, `orders/entry.py` is untouched, and every long path reads nothing new. The new dial ships at 1.0 (no change), and Market Weather itself still ships in shadow, so with the shipped defaults trading behaves exactly as in E96.

## What it does
1. **Short dial `MARKET_STATE_SHORT_MULT`** (position-stocks, float 0.0 to 1.0, ships 1.0, `overlay=True`, live-read). It multiplies a short's risk on top of `SHORT_RISK_MULT`. `0` means no short today. Only the market-state overlay (tighten-only, gone at 15:30 IST or on restart) or a person can lower it. Unreadable counts as 0 (a short is the riskier side, so a doubt means none).
   - `config.py`, `control/catalog.py` (group "Market Weather"; the Switches page lists it automatically).
   - `capital/ledger.py`: `_market_state_short_mult()`; `_short_risk_mult("SHORT")` is now `SHORT_RISK_MULT x dial`; `reserve_capital(side="SHORT")` refuses when the product is 0, before any capital row is touched. Longs return 1.0 and never see it.
   - `orders/short_entry.py`: refuses with `SHORT_MARKET_STATE_OFF` right after the shared guards, before any scan data is fetched or any sizing runs; the shadow budget carries the dial through `_short_risk_mult`.
2. **Runner profile** (`calibration-runner-service/regime/profiles.py`), from the plan's short-exposure table: calm 0 (off), choppy 0 (off), panic 0.5 (a quarter of a long), normal and stressed unchanged (the saved `SHORT_RISK_MULT` 0.5 already makes a short half a long). Position Stocks only: real-trade never receives it. It is listed in `STATE_SPECIFIC_DIALS` because it is deliberately not monotone across states (off in choppy, back in stressed). A saved value already tighter is never loosened. `autocalib.py` PROTECTED has the dial, so the weekly engine can never save it.
3. **Protected-list fix.** Step 5 added six switches that were on neither the calibrated nor the protected list (`SHORT_ENTRY_START_IST`, `SHORT_GAP_UP_REFUSE_PCT`, `SHORT_MAX_DAY_FALL_PCT`, `SHORT_MIN_BOARD_CONVICTION`, `SHORT_MIN_ENTRY_RANGE_POSITION`, `SHORT_VWAP_TOLERANCE_PCT`). `test_every_structure_and_market_state_key_is_either_a_rule_or_protected` in the runner suite was failing on them. They are protected now.
4. **Frontend.**
   - `shortShadowView.ts` (pure, no imports): the card's states (unavailable / off / waiting / active), the plan's S2 gate (100 would-be shorts, 20 trading days, positive after charges) with plain reasons for what is missing, gate refusal counters, `shortExposureLine()`.
   - `components/board/ShortShadowCard.tsx`, mounted on the Position Stocks "Board" tab: headline, S2 progress bar, target / stop / square-off counts, why the gates said no, the latest 10 rows with a SHORT badge. Read-only, polls only while visible. `positionStocksApi.shortShadow()` calls the existing `GET /short/shadow`.
   - Market Weather card: a "Shorts: ..." line for the current state; `weatherView.ts` labels the new dial; `weather.fixtures.json` profile regenerated from `profiles.table()`.

## Tests
- NEW `position-stocks-service/tests/test_short_step6_market_state_dial.py` (9, sqlalchemy-free like `test_regime_hooks.py`): defaults change nothing, clamp and fail-closed, product and zero, longs never see it, config parsing, catalog entry and kit classify (0 tightens), reserve_capital refusal order, short_entry refusal order, and no other file reads the dial.
- Runner: `test_regime_package.py` (+2: short dial table and never loosened; the monotone-ladder test skips the short dial), `test_weather_apply.py` (fixture gains the key; calm now sends position-stocks only the short dial; panic list gains it). `scripts/test_regime_overlay.py` pins the new position-stocks overlay set.
- Frontend: NEW `shortShadow.test.mjs` (wired into `npm test`), `weather.test.mjs` updated (calm lists only the short dial; normal lists none; the exposure line).

## What was run here, and what was not
- Run and passing: calibration-runner suite with real pytest (162 passed, 1 skipped for `fastapi`), `scripts/test_regime_overlay.py` (19), `scripts/check_control_catalog.py`, `scripts/check_shared_copies.py`, `weather.test.mjs`, `shortShadow.test.mjs`, the new position-stocks file and the other short / catalog / hook files through a plain Python runner. The position-stocks files that can import without sqlalchemy fail or pass exactly as on the E96 zip (same 16 sandbox-only failures before and after).
- **Not run:** the position-stocks and real-trade pytest suites (SQLAlchemy, FastAPI and httpx could not be installed in this workspace, PyPI is blocked), `GET /short/shadow` end to end, `npm run build` (the npm registry was unreachable; the new `.ts` / `.tsx` files were only type-checked against stubs and the helpers under node). Run `pytest`, `npm test` and `npm run build` on the VM or in CI before deploying.

## Known limits
- The plan's "tighter stop" for stressed and panic shorts is not built: it needs the live short path.
- Short-side shortlist weighting (`screening/shortlist.py`) is not built: `run_short_cycle` already filters by the Market Board's bearish list, and the long shortlist keeps dropping bearish names.
- The odds recorder, the S2 per-day concentration check (the service sends the latest 50 rows only, so the card cannot judge "not from one or two days" and says a person must) and the Dhan answer S0 are still open.

## Next
Run `SHORT_MODE=shadow` for 4 weeks on the VM (with `WEATHER_ENABLED` on in shadow so rows carry a market state), then judge gate S2. S0 still blocks anything live.
