# Group 377 - live-money safety defaults (Group A of the 2026-10-15 log / test-run review)

## Changes
1. **Broker-side stop is ON by default.** `BROKER_STOP_ENABLED` (real-trade) defaults to `true` (was `false`). A resting Dhan stop-loss order protects CNC holdings
   held since an earlier day if the software exit loop (polled every ~8 s) is down or blind. Kill switch: `BROKER_STOP_ENABLED=false`.
   **Not exercised against a live Dhan account.** Watch the first position that qualifies: real-trade `GET /orders/REAL` and Dhan's order book. A CNC stop on holdings
   needs eDIS / TPIN authorisation; a rejection is logged by name (`real-trade-broker-stop`). The boot-time SAFETY warning only appears when it is off.
   A value already in the VM `.env` overrides the default - check `grep BROKER_STOP .env`.
2. **No usable daily structure is no longer a silent pass.**
   - real-trade and position-stocks: `STRUCTURE_REQUIRE_DATA` defaults to `true` (was `false`): missing / stale / insufficient structure = WAIT (tag `structure_nodata`).
   - decision-prediction: new rule **P1_NO_STRUCTURE** (`STRUCTURE_REQUIRE_FOR_BUY_NOW`, default on): a BUY NOW with no usable structure summary becomes WAIT
     ("no usable daily structure (...) - a BUY NOW needs a checked entry"). Only BUY NOW is touched; PREPARE / WAIT / DO NOT BUY / HOLD are not; an already-owned
     stock is never re-labelled; `STRUCTURE_POLICY_MODE=off` beats it; a policy exception on a BUY NOW also fails closed (WAIT) instead of passing.
   - Consequence to expect: until the daily refresh (`structure-refresh.yml`, 16:00 and 08:15 IST) has seeded `ms_*` into the feed rows, a bulk-scan BUY NOW without a
     summary shows WAIT. Single-stock `/decide/{sym}` fetches structure itself. Relax with `STRUCTURE_REQUIRE_FOR_BUY_NOW=false` if coverage turns out low.
3. **Kill switches (no code change; thresholds NOT tuned - the 346-symbol A/B verdict is `no_edge`):**
   `STRUCTURE_POLICY_MODE=off` (decision), `STRUCTURE_GATE_MODE=off` / `STRUCTURE_GATE_DEMO_MODE=off` (real-trade), `STRUCTURE_GATE_MODE=off` (position-stocks),
   `SNIPER_STRUCTURE_MODE=off` (gateway sniper), `STRUCTURE_STOP_MODE=off`, `STRUCTURE_EXIT_MODE=off|log`, `STRUCTURE_REQUIRE_DATA=false`, `STRUCTURE_REQUIRE_FOR_BUY_NOW=false`.

## Tests
- `decision/tests/test_group353_structure_policy.py`: unknown-structure cases now assert P1 (WAIT) by default and the old pass-through with `require_data=False`;
  only BUY NOW is touched; env switch and `mode=off`; garbage input fails closed for BUY NOW. `test_group353_decide_structure_wiring.py` updated the same way.
- `real-trade-service/tests/test_group377_safe_defaults.py` (new): source defaults, `.env.example` agreement.
- real-trade and position-stocks `conftest.py`: the autouse fixtures turn `STRUCTURE_REQUIRE_DATA` (both) and `BROKER_STOP_ENABLED` (real-trade) OFF for the older tests,
  which have no summary / no broker; tests that need them set them explicitly (test_group322 and test_group351 already do).

## Verified / not verified
Run in the sandbox (stdlib only): decision policy tests (20), the new defaults test (4), `sync_market_structure --check`, `gen_env_reference.py --check`, `check_shared_copies.py`,
`check_table_name_collisions.py`. NOT run here: the pytest suites of decision-prediction, real-trade and position-stocks (no pytest / fastapi), any live Dhan call.
Run: `bash scripts/docker_test.sh decision-prediction-service` (and real-trade-service, position-stocks-service) before deploying.

## Not changed (still open from the review)
Oracle latency probe for real-trade / market-data, the non-hermetic `test_group368_analyze_baseline.py` (stubs history and quotes but not the delivery HTTP call; it hits the
default public market-data URL when the machine has internet), frontend shadow-label cleanup, candidate-engine 52-week rule and ETF filter.
