# E46 - the four open decisions, made (2026-10-23)

Nothing here is a new feature. The four questions left open in E45 were answered from the repo's own numbers (REMAINING_TASKS, group 415, E39, the calibration notes). No outside source was used; a changed number is one value on the calibration page or one env var.

## 1. Risk per trade (A6) - real-trade 5.0 -> 2.0
- `group322` already caps one trade's risk at the daily-loss % (3.0), so 5.0 never applied in full. The Rs 3,000 `MAX_TRADE_VALUE` cap decides the real size.
- 1.0 is NOT used: the note in `config.py` records that 1.0 starved entries at this account size (Rs ~109 budget at Rs ~11k equity). 2.0 gives Rs ~219, matches position-stocks (2.0), and still lets a normal stop size at least one share.
- Effect: the largest one-trade risk drops from 3.0 % to 2.0 % of equity; the adaptive max-price ceiling (equity x risk % / 6 %) is ~Rs 3,650 at Rs 11k, still above the Rs 3,000 trade cap.
- **Important:** the live value is the DB row (`trade_risk_config`), not the config constant. New `_migrate_risk_default_e46()` (runs after `_migrate_risk_defaults()` at boot) moves a row ONLY when it is exactly 5.0 and was never saved by an admin (`updated_by` empty or our own tag). An admin-saved row (even 5.0 on purpose) is never touched; an env pin of 5.0 makes it a no-op; after the first boot it does nothing. Tests: `TestMigrateRiskDefaultE46` (4) in `tests/test_main_routes_core.py`.
- The calibration-page switch for this setting used to do nothing on an existing row; fixed in E47 (docs/E47_RISK_SWITCH_REACHES_ENGINE.md).

## 2. Position Stocks daily entry cap - 15 -> 10
Real-trade has 3 slots and a cap of 6 (two turns of the slot book). Position Stocks has 5 slots (`MAX_CONCURRENT_SCALP_POSITIONS`), so the same ratio gives 10, which is still above real-trade because the scalper turns slots faster. Change it on the page (Risk caps) or `POSITION_STOCKS_MAX_ENTRIES_PER_DAY`. Catalog `shipped` updated (the guard checks it against config).

## 3. Log / off versus enforce - nothing switched on
| Rule | State | Decision | Why |
| --- | --- | --- | --- |
| Kronos, TimesFM | off | stay off | Kronos review says `NOT PROVEN` (passed trades did worse); TimesFM has no evidence |
| Structure gate, real-trade | log | stay log | G2 `no_edge` (CI crosses 0), G3 not passed |
| Structure gate, position-stocks | enforce | unchanged | you asked for it live (14 Oct); it only refuses entries, so its cost is missed trades, not extra risk. Re-check with `scripts/structure_gate_check.py` when G3 has data; set `STRUCTURE_GATE_MODE=off` to switch it off |
| Structure stop / exit | off / log | stay | group 415 |
| ORB | shadow | stay | no live mode exists; wait ~30 outcomes |
| Regime gate | off | stay | turned off on evidence (group notes) |
Rule used: a rule that moves money is enforced only after its own validation gate passes (B-tools, A/B with a confidence interval above 0).

## 4. Requirements pins - parent wins
docker-compose builds the PARENT folder, so the parent pins are what runs and what the tests ran against. The 8 sub-app pins were moved to the parent's: yfinance 1.5.2 -> 0.2.40 (event), httpx (fundamental, news, technical, decision), fastapi 0.104.1 -> 0.111.0, uvicorn 0.24.0 -> 0.30.1, pydantic 2.5.3 -> 2.11.5 (decision). No parent pin and no running image changed. `scripts/check_requirements_drift.py` baseline is now 2 entries (the optional `shap` / `imbalanced-learn` ML extras). If you want yfinance 1.x, change the PARENT pin first (it affects market data fallbacks) and test it on its own.

## Still not done on purpose
- Gateway router split (14.7 k-line `main.py`): thousands of tests patch helpers on `main`. Safe plan: move ONE small route group per release into `routers/<name>.py`, keep `from routers.<name> import *` re-exports in `main.py` so patches on `main.X` still resolve, run the full gateway suite (8,944 tests) before the next group. Needs pytest and a deploy-and-watch cycle; not mixed into this zip.
- Installable market-structure package: touches every Dockerfile; same reason.
- C21 pattern signals stay log-only (`in_tqs: false`) until they pass the B-tools.

## Check on the VM
`bash scripts/docker_test.sh` (real-trade + position-stocks suites), then rebuild real-trade-service and position-stocks-service. After the real-trade boot: logs should show `migrated DEMO/REAL risk_per_trade_pct 5.00% -> 2.00%` once; `GET /risk-config/REAL` shows 2.0. Not run here: pytest (not installed, no network). Run here: py_compile of the edited files, `check_control_catalog.py`, `check_requirements_drift.py` (+ its 5 tests via a stand-in), `gen_env_reference.py --check`, `check_secrets.py`.
