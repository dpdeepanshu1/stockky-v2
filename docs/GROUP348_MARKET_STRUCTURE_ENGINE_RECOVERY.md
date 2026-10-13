# Group 348: market-structure engine, partial recovery

The earlier sessions built a shared market-structure engine and were wiped by sandbox resets. Only part of that code survives in the
session log, so this zip carries that part as a new, unwired package: `shared/market_structure/`.

## Recovered
`ms_types`, `params`, `atr`, `prep`, `swings`, `order_blocks`, `liquidity`, `fvg`, `structure`, `summary` (+ `__init__`).
Pure stdlib, no I/O, no clock, no env. Nothing imports it, so no service behaviour changes.

## NOT recovered (not in the log, not guessed)
`fibonacci`, `levels`, `location`, `scoring`, `explain`, `engine` (the public `analyze()`), `compat_board` (position-stocks wrapper),
`scripts/sync_market_structure.py`, technical-service integration, decision `structure_policy.py` + its wiring, frontend, golden tests.
The design document the earlier sessions worked from is not in the log either.

## Checked
`shared/market_structure/tests/test_engine_smoke.py` (7 cases) passes with plain `python`. pytest could not be installed (no network),
and the full service suites were NOT run.

## Update (session 2): engine completed, vendored, unwired
Added `fibonacci`, `levels`, `location`, `scoring`, `explain`, `engine` (`analyze()`), `scripts/sync_market_structure.py` (copies the package to
`services/<svc>/stockky_ms/` for 5 services; `--check` fails on drift or hand-edits) and `tests/test_engine_analyze.py` (21 cases).
Fix: `structure_rr` now picks the first stop candidate inside the min/max risk band instead of the first one above the minimum, so a far
order block no longer hides a nearer valid swing-low stop. Still unwired: no service imports `stockky_ms`. `compat_board` is still missing.

## Update (session 3, group 349): board adapter rebuilt, detector + no-lookahead tests
`compat_board.py` was not in the zip, so it was rewritten from the legacy `screening/market_structure.py` (`find_zones`, `swings`, `atr`), not
recovered. It runs the new engine with the legacy single-value ATR, `compat` swings and `compat` / `full` zone bounds, and returns the legacy
zone / swing shape. Parity (60 random walks x 2 zone modes, plus 25 for swings): swings and ATR are identical; zones are identical except for
one intended difference, the engine looks at the bar right after the displacement candle (legacy: two later), so it may call a zone `tested`
that legacy calls `fresh` and drops a zone that bar already closed through. It never adds a zone. `respected` is reported as `tested`.
Tests: `tests/test_engine_detectors.py` (6: wick-only break, split, FVG, sweep, order block, real T-NLA with a non-empty-events guard) and
position-stocks `tests/test_group349_compat_board.py` (5). The board is NOT switched over yet. All Dockerfiles use `COPY . .`, so `stockky_ms/`
ships without Dockerfile changes. Not run: pytest (cannot be installed offline; tests were run with plain `python`) and the full service suites.

## Update (session 4, group 349): board switch, technical + gateway wiring, golden baseline
- **position-stocks:** `screening/ms_engine_switch.py` + `BOARD_USE_MS_ENGINE` (default 0). `board.py` now calls `zones_and_swings()`; flag off = the legacy
  functions, flag on = `stockky_ms.compat_board`, any error falls back to legacy for that call. Tests: `tests/test_group349_ms_switch.py` (3).
- **technical service:** `/analyze/{symbol}` gets an additive `market_structure` + `structure_summary` (fail-open, `STRUCTURE_ENABLED`, stale after 7 days),
  plus the `macd`, `macd_hist`, `sma20` keys the gateway already looked for. New `GET /technical/structure/{symbol}` (503 when the engine is off/unavailable).
  `stockky_ms` is found because the parent loader keeps its folder on `sys.path`; a standalone technical image simply has no block. Tests: 5 (helper source is
  exec'd, no fastapi needed).
- **gateway:** `ms_passthrough.py` (schema_version 1 only, never raises, `STRUCTURE_PASSTHROUGH`), merged into `/stock/{symbol}`, `ms_*` keys seeded into feed rows
  on repair, `structure_summary` added to the `/decide/evaluate` payload, and `GET /stock/{symbol}/structure` (always 200, empty default). Tests: 5.
- **golden baseline (WS-0):** `shared/market_structure/tests/test_engine_golden.py` + `golden/analyze_golden.json` (10 fixed series, params hash d0042325).
  Regenerate on purpose with `--update`.
- `docs/ENV_REFERENCE.md` regenerated (`STRUCTURE_ENABLED`, `STRUCTURE_PASSTHROUGH`). Not run: pytest, full service suites, the gateway route live.
- The decision service does NOT read `structure_summary` yet (extra payload key is ignored). The board flag defaults to off, so no behaviour change.

## Update (session 5, group 350): technical `lite` switch, structure cache, size guard
- Sync targets confirmed by the owner: analysis-intelligence, api-gateway, position-stocks, decision-prediction, real-trade (`sync --check` clean).
- **`/analyze/{symbol}?lite=1`** (new param, default off): skips the structure computation. A warm cached `structure_summary` is still returned (flat keys, so the
  gateway repair path can still seed `ms_*`); the heavy `market_structure` block is never sent in lite mode and a lite result is never written to the shared
  `tech_analysis:` key (so a later full call is not served a structure-less result). Cached full results are returned with the block stripped.
- **Structure cache:** `_structure_cached()` keeps one entry per (symbol, extension flags) under `tech_ms:`, validated against the last bar timestamp + bar count,
  TTL = `STRUCTURE_CACHE_TTL_S` (default: the analysis TTL, 5 min open / 6 h closed; 0 disables). Shared by `/analyze` (including the adaptive-override path, which is
  not cached as a whole) and `/structure/{symbol}`. Cache errors fail open to an uncached compute.
- **Payload size guard:** `STRUCTURE_MAX_BYTES` (default 60000, 0 disables). Over the cap the block is dropped, `structure_summary` is kept and
  `structure_truncated: true` is set. A normal 250-bar block is about 3.3 KB.
- Tests: 6 added to `analysis-intelligence-service/tests/test_group349_structure_helper.py` (11 total). Route bodies were also run with a stubbed fastapi
  (lite cold/warm/force, override, `/structure`, capped): engine called once across all of them. Not run: pytest, the real FastAPI app, the full service suites.

