# Group 381 - audit of the 2026-10-15 remaining-items list against the group380 zip

The list (groups A-G) was written before groups 377-379. Checked against the zip, most of it was already done:

| Item | State in the group380 zip |
|---|---|
| A1 BROKER_STOP_ENABLED | default on since group377 (check `grep BROKER_STOP .env` on the VM, a value there overrides it) |
| A3 STRUCTURE_REQUIRE_DATA | default true since group377 |
| B1 /ops/db-latency on real-trade + market-data | done in group378 |
| B2 news feed health | `GET /news/feed-health`, group378 (evidence only) |
| C1 null asof_date / params_hash | traced in group378: populated on current code, the null row was an older build |
| C2 / C3 observations, refresh catch-up | group378 |
| D1 two failing tests (delivery call) | fixed: harness blocks httpx.Client; `test_harness_is_hermetic_about_delivery` passes |
| E1 / E2 52-week rule, ETFs | group379 |
| E3 legacy gate backtest | tooling in group380 (you run it) |
| F1 / F2 sampler, PREPARE upgrade report | group379 |
| G1 shadow leftovers | comments already corrected; `StructureGateCard.tsx` / `structureView.ts` are live, and their "shadow" branches label rows stored before group370, so they are kept |
| G2 MARKET_DATA_URL default | compose service name, guard script in CI (group379) |
| G3 2027 holidays | waits for NSE (December) |
| D5 baseline table | filled in this group from your VM counts |

## Changed in group 381
- `docs/GROUP339_BASELINE.md`: results table filled with the VM numbers (21,909 passed, 2 failed, 1 skipped) and what is still unrecorded. No code changed.

## Still yours (needs the VM / CI / live data)
- `npm run build` (docker node image), `analysis-intelligence-service/run_tests.sh` coverage gate, Python 3.12 runs.
- First live BROKER_STOP check on one small position; `/structure/status/REAL` coverage before relying on STRUCTURE_REQUIRE_DATA.
- Run `structure-backtest.yml` with 2-3 seeds; read `/ops/db-latency` on real-trade (8005) and market-data; read `/news/feed-health`.
- 2027 NSE holidays in December.
