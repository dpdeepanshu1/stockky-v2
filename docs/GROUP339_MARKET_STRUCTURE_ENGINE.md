# Market-structure engine: design, flags, rollback (as built through group 382)

Written from the code and `.env.example` (group 383). Where the code differs from `STOCKKY_STRUCTURE_UPGRADE_EXECUTION_PLAN.md` the code wins and the difference is listed at the end.

## 1. What it is
A stdlib-only daily market-structure engine (`shared/market_structure/`, package `stockky_ms`): swings, HH/HL/LH/LL trend state, BOS / CHOCH, liquidity pools and sweeps,
fair-value gaps, order blocks, Fibonacci, structure-aware support/resistance, entry-location score, chase (ATR beyond the demand zone), structure R:R, family scoring.
It does no I/O, reads no env and no clock. Output is versioned (`engine_version`, `schema_version`, `params_hash`). Canonical source: `shared/market_structure/`.
Vendored byte-identical into 5 service folders as `stockky_ms/` (a container image only contains its own service folder). Drift check:
`python3 scripts/sync_market_structure.py --check` (`--sync` rewrites the copies). Edit only the canonical copy.

## 2. Data flow
1. **analysis-intelligence `technical/main.py`**: on `/analyze` (and `GET /structure/{symbol}`) it runs the engine on the UNTRIMMED daily frame with today's forming bar dropped
   until 15:30 IST + `STRUCTURE_BAR_CLOSE_BUFFER_MIN` (20). It adds `market_structure`, the flat `structure_summary` (`ms_*` keys), `macd`, `macd_hist`, `sma20`; no existing field changes.
   `?lite=1` skips the computation. Any engine error returns `quality:"unavailable"` and leaves the classic fields untouched. `/health` shows structure counters.
2. **api-gateway**: copies the `ms_*` summary into the stored feed rows; `structure_refresh.py` refreshes missing/stale rows after the close (QStash/GitHub trigger
   and its own catch-up loop when the market is closed/holiday); the scan passes the summary to the decision service; the Buy Sniper uses it (`SNIPER_STRUCTURE_MODE`)
   and picks extra PREPARE rows to re-quote on structure events (`SNIPER_PREPARE_STRUCTURE_MODE`).
3. **decision-prediction `decision/structure_policy.py`**: runs LAST in the decision pipeline (after the multi-horizon override and the second data-quality gate).
   Demote rules P2-P8 plus P1_NO_STRUCTURE (a BUY NOW with no usable structure becomes WAIT), the P9 PREPARE TO BUY -> BUY NOW upgrade, and structural stop/target
   (`legacy_levels` keep the old values). Result is in `structure{...}` on the response. An already-owned stock is never relabelled.
4. **training**: every decision (including rejects) is logged to `decision_structure_observations` (`training/structure_obs.py`) and scored at T+1/5/10/20 sessions
   (entry = next session open + `STRUCTURE_OBS_SLIPPAGE_PCT`, MFE/MAE, first touch of the structural stop/target, stop wins a same-bar tie, NIFTY-relative).
   Report: `GET /api/structure/report` (edge of passed vs demoted, `prepare_upgrade`). Observation stats: `GET /structure/observation-stats`.
5. **real-trade `entry_engine/structure_gate.py`** (Gate 4e): snapshot-based, no engine in the service; fetches the summary through the gateway (cached, fail-open on errors,
   but `STRUCTURE_REQUIRE_DATA=true` makes "no usable structure" a WAIT). Structural stop option (`STRUCTURE_STOP_MODE`). `exit_engine/structure_exit.py`: ratchet-only stop tightening/trailing
   of profitable positions. `trade_decisions.structure_json` stores the snapshot. The risk engine is not modified.
6. **position-stocks**: daily-structure gate in `orders/structure_gate.py` (looser limits than real-trade), observation hook, board switch `BOARD_USE_MS_ENGINE` (default 1 = new engine, live since group354; `0` forces the legacy code; any engine error falls back to legacy for that call).
7. **frontend**: StructurePanel, StructureGateCard, StructureEdgeCard, sniper badges / held-back list, wait-for-entry text.

## 3. Defaults that matter (live money)
| Setting | Default | Meaning |
|---|---|---|
| `STRUCTURE_POLICY_MODE` | `full` | decision rules live; `off` = old decisions exactly |
| `STRUCTURE_PREPARE_UPGRADE` | `0` (was `1` until group 415) | P9 upgrade off; `1` = on |
| `STRUCTURE_REQUIRE_FOR_BUY_NOW` | `true` | no structure = WAIT |
| `STRUCTURE_GATE_MODE` / `STRUCTURE_GATE_DEMO_MODE` | `enforce` | real-trade and position-stocks entry gate; `off` = kill switch |
| `STRUCTURE_REQUIRE_DATA` | `true` | real-trade / position-stocks: no usable structure = WAIT |
| `STRUCTURE_STOP_MODE` | `off` (was `on` until group 415) | `off` = ATR stop; `on` = structural stop used when sane (within 0.5x-1.6x of the ATR-stop risk) |
| `STRUCTURE_EXIT_MODE` | `log` (was `exit` from group 399 to 414) | `off` / `log` / `tighten` (raise stop only) / `exit` (sells an in-profit position on a fresh bearish daily-structure signal; a losing one is never touched) |
| `SNIPER_STRUCTURE_MODE` | `enforce` | sniper drops chasing / stretched / no-room / bearish cards; `off` |
| `STRUCTURE_MAX_CHASE_ATR` / `STRUCTURE_MAX_STRETCH_ATR` / `STRUCTURE_MIN_ROOM_ATR` / `STRUCTURE_MIN_RR` | 1.5 / 3.0 / 0.5 / 0 (group424: R:R floor off; 1.2 was the old value) | real-trade limits (position-stocks 4.0 / 5.0 / 0.3) |
| `STRUCTURE_OBS_ENABLED` | `true` | `false` = nothing recorded or evaluated |

## 4. Rollback
No code deploy needed: set the env vars below and restart the service. The engine keeps computing and logging; only decisions change.
`STRUCTURE_POLICY_MODE=off` (decision) - `STRUCTURE_GATE_MODE=off` and `STRUCTURE_GATE_DEMO_MODE=off` (real-trade, position-stocks) - `STRUCTURE_STOP_MODE=off` -
`STRUCTURE_EXIT_MODE=off` - `SNIPER_STRUCTURE_MODE=off` - `SNIPER_PREPARE_STRUCTURE_MODE=off` - `STRUCTURE_PREPARE_UPGRADE=0` - `STRUCTURE_REQUIRE_DATA=false` and
`STRUCTURE_REQUIRE_FOR_BUY_NOW=false` (fail open on missing data). `STRUCTURE_ENABLED=0` (technical) removes structure from `/analyze`. A value in the VM `.env` overrides every default above.

## 5. Evidence and gates
`python3 scripts/structure_gate_check.py --ab <ab_report.json> ... --live <report.json>` reports plan gates G2 (A/B benefit) and G4 (live observations).
Latest known evidence (group424): the 346-symbol A/B was `no_edge` (the `room` rule slightly negative); a later 177-symbol run (seed 7) was `pass` (+0.56 pp) but a 276-symbol run (seed 11) was `no_edge` (0.0 pp, 90% CI [-0.19, +0.21]), so no edge has replicated and G2 is not met. The `rr` rule's drop-one edge flipped sign (+0.39 on seed 7, -0.19 on seed 11): do not raise `STRUCTURE_MIN_RR` on that evidence. `chase`, `bearish` and `stop_wide` were non-negative on both seeds. Drop-one edge = candidate minus the gate without that rule, so a NEGATIVE value means the gate does better without it. 1 live observation. Backtest tooling: `scripts/fetch_history_for_backtest.py`,
`scripts/ms_ab_report.py`, `scripts/legacy_gate_backtest.py`, workflow `structure-backtest.yml`. Thresholds are starting values, not calibrated.

## 6. Where the code differs from the plan
- Rules are live (no shadow stage); plan D7 wanted off -> shadow -> caps -> full with gates in between.
- Vendored into 5 services (plan: 3). `STRUCTURE_ENABLED` default is on, `STRUCTURE_POLICY_MODE` default is `full` (plan: shadow). P9 (`STRUCTURE_PREPARE_UPGRADE`) default off since group 415 (plan: off).
- Policy thresholds differ from the plan text (R:R minimum was 1.2, off by default since group424, rules named P1-P9 with their own ordering); see `decision/structure_policy.py`.
- Not built: walk-forward calibration run, ML features v2, weekly / intraday structure, Wyckoff, volume profile; `screening/market_structure.py` bodies not replaced by the wrapper (switch instead).
