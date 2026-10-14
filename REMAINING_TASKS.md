# Remaining tasks: market-structure engine (after group 349, session 4)

## Done so far
- Engine package `shared/market_structure/` (17 modules + `compat_board`), `analyze()` end to end, vendored into 5 services as `stockky_ms/` (`sync --check` clean).
- **Wired (all flagged, fail-open):**
  - position-stocks board switch (`BOARD_USE_MS_ENGINE`, default 0, legacy fallback)
  - technical service `market_structure` + `structure_summary` on `/analyze`, `macd` / `macd_hist` / `sma20` keys, `GET /structure/{symbol}`
  - gateway `ms_passthrough.py`, merge into `/stock/{symbol}`, `ms_*` seeding on repair, `structure_summary` in `/decide/evaluate` payload, `GET /stock/{symbol}/structure`
- **Golden baseline:** `tests/test_engine_golden.py` + `golden/analyze_golden.json` (10 series, params hash d0042325).
- **Tests (plain `python`, none under pytest):** package 34 + 3 golden; position-stocks 5 parity + 3 switch; technical helper 11; gateway 5.

## Blocking / decisions for you
1. ~~Confirm the 5 sync targets~~ DONE (confirmed by the owner): analysis-intelligence, api-gateway, position-stocks, decision-prediction, real-trade.
2. The design document is lost, so thresholds in `params.py` are hypotheses. Send the original numbers if you have them.
3. Run everything under real pytest (no network here): the new test files and the full position-stocks / analysis-intelligence / api-gateway / real-trade / decision suites. Expected failures: only the known 4 `test_oracle_compat`.

## Work list, in order
1. **Verify on your side** (above), plus start the gateway and technical service once and hit `/stock/RELIANCE/structure` and `/technical/structure/RELIANCE`. The routes were only checked by source inspection and helper tests, not live.
2. ~~**Technical `lite` switch + cache**~~ DONE (group 350): `lite=1` param, `tech_ms:` structure cache (`STRUCTURE_CACHE_TTL_S`), size guard (`STRUCTURE_MAX_BYTES`). Still to verify live under real FastAPI.
3. **Board A/B:** turn `BOARD_USE_MS_ENGINE=1` on one instance, compare a day of live boards old vs new (expect only the one-bar zone difference), then decide.
4. **Gateway scan / sniper:** carry `ms_*` into the scan payload, the sniper chase gate (`ms_chase_atr`) and the PREPARE refresh (log-only first). Not done: today the keys only reach feed rows, `/stock/{symbol}` and the decide payload.
5. **Decision service:** `structure_policy.py` (lost; rules P1-P9 from the plan) that reads `structure_summary`; shadow mode first (log only, no effect on entries), params hash logged with every decision. The decision service currently ignores the new payload key.
6. **Training / learning:** store `params_hash`, `engine_version` and the `ms_*` keys with each training row (`StructureObservation`, T+1/5/10/20 evaluator); do not retrain on mixed versions.
7. **real-trade:** `structure_gate.py` using `ms_loc_score` / `ms_rr` as an advisory input only, behind a flag, shadow first; `structure_json` column.
8. **Frontend:** show trend, BOS/CHOCH, zone, FVG, sweep, levels and the `explain` text from `/stock/{symbol}/structure`; handle `quality` = limited / insufficient / stale / unavailable.
9. **Rollout:** shadow, compare outcomes, enable per service. Review params with real data; there is no proven edge yet.
10. **Housekeeping:** add `sync --check` and the golden test to CI / `check_shared_copies`; add `BOARD_USE_MS_ENGINE` to the env docs (it is read through `_get_bool`, so the generator misses it); `STRUCTURE_*` entries in `.env.oracle.example` / compose.

## Known gaps
- `compat_board` is rebuilt from the legacy code, not recovered; `swings()` with left/right < 1 returns [].
- Detector and golden tests use synthetic bars, not real market data.
- The golden file only freezes current behaviour; it does not prove the thresholds are right.
- Dockerfiles need no change (`COPY . .`). The technical sub-app finds `stockky_ms` through the parent service folder; a standalone technical image has no block (by design).
