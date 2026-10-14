# Group 370 - structure rules are always live (no shadow mode), strict BUY NOW, active PREPARE / P9

## What changed
- **decision-service `structure_policy.py`**: modes are `full` (always live) or `off` (kill switch). Stale `shadow` / `caps` in an old `.env` = live.
  - P7: BUY NOW needs >= 3 independent groups AND entry-location score >= 55 (a missing value counts as NOT met once a usable summary exists).
  - P8: BUY NOW needs a bullish, non-failed BOS <= 10 bars old (`P8_FAILED_BOS` / `P8_NO_BOS` / `P8_OLD_BOS`).
  - PREPARE TO BUY: chase / stretch / R:R / bearish-trend are recomputed at the live price and now ACTIVE (-> WAIT).
  - P9: a PREPARE that passes every BUY NOW requirement and has a trigger (fresh BOS / CHOCH / sweep / zone / fib) becomes BUY NOW, only when data quality allows
    (not already owned, `actionable_ok`, not provisional, confidence not Low). Kill switch `STRUCTURE_PREPARE_UPGRADE=0`.
  - Overrides: `STRUCTURE_POLICY_PARAMS_JSON` accepts `groups_min`, `loc_min`, `bos_max_bars`.
- **real-trade**: `STRUCTURE_GATE_MODE` / `_DEMO_MODE` = enforce | off; `STRUCTURE_STOP_MODE` = on | off; `OPENING_GATE_SHADOW` and its branch removed; counters lose `shadow_only`.
- **position-stocks**: `STRUCTURE_GATE_MODE` = enforce | off; `BOARD_GATE_MODE` = enforce | off (the `auto` promotion machinery is gone); `OPENING_GATE_SHADOW` removed.
- **shared `observation.py`**: only `rejected_enforced | passed | no_opinion`; legacy `rejected_shadow` rows are read as rejected_enforced. `stockky_ms/` re-synced.
- **docker-compose.yml / .env.example**: the group 369 "SAFE ROLLOUT" block (which forced shadow everywhere) is removed; compose default is `full`.

## Not done here (still open)
- (done in group 371) api-gateway `buy_sniper.py` structure gate rebuilt and wired: see `docs/GROUP371_SNIPER_STRUCTURE_GATE_REBUILT.md`.
- gateway `structure_refresh.py` is not hooked into `main.py` (routes `/data-feed/structure-refresh*`, tick action, jobs snapshot, `stockky_prepare_structure_total`); position-stocks `ScalpStructureObservation` is not hooked. (`sniper_observation.py` is hooked since group 371.)
- Frontend shadow / observe-only labels; backtest / walk-forward on real data; unadjusted-data split test; full CI with real FastAPI.

## Verified in the sandbox
`sync_market_structure.py --check`, `gen_env_reference.py --check`, shared engine suite (97 passed), decision `structure_policy` tests (20 passed).
Syntax-checked only (no SQLAlchemy / FastAPI / httpx here): real-trade and position-stocks edits and tests, decision `main.py` wiring - run `bash scripts/docker_test.sh <service>` on the VM.
