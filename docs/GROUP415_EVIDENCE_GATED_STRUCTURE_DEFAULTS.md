# Group 415 - structure defaults back to what the evidence supports

Defaults only. No gate, sizing, stop or risk-engine logic was edited.

## Why
The execution plan keeps structure rules that change money decisions OFF until their validation gate passes. Gate G2 (benefit) is **not met** (A/B verdict `no_edge`:
group 375 edge +0.54 pp, CI [-0.01, 1.29]; group 410 +0.35 pp, CI [-1.63, 2.44] on 71 signals) and G3 (stability) is not started. Three defaults were more aggressive
than the plan allows, and the stale limit was looser than the plan.

## Changes
| Setting | Service | Old | New | Plan said |
|---|---|---|---|---|
| `STRUCTURE_PREPARE_UPGRADE` (P9 PREPARE -> BUY NOW) | decision | on | **off** (`1/true/on/yes` turns it on) | off |
| `STRUCTURE_STOP_MODE` | real-trade | on | **off** (`on` / `enforce` turns it on; anything else = off) | off until G3 |
| `STRUCTURE_EXIT_MODE` | real-trade | exit | **log** (record only; `off / tighten / exit` still available) | log first |
| `STRUCTURE_MAX_AGE_DAYS` | real-trade, position-stocks, api-gateway sniper | 7 | **5** | 3 |
| `_STRUCTURE_STALE_DAYS` | analysis-intelligence technical | 7 | **5** | 3 |

## Why 5 and not the plan's 3
The last daily bar is stamped at midnight (UTC or IST). On a normal Monday 09:15 IST it is already ~3.2-3.4 days old, so a 3-day limit marks every Monday as stale and,
with `STRUCTURE_REQUIRE_DATA=true`, turns every Monday entry into WAIT. Over the rest of 2026 the worst case at the 15:30 close is 4.65 days (Mon 28 Dec, after the 25 Dec
holiday), so 4.5 would fail that day and 5 clears every trading day. NSE has not published the 2027 holidays (expected December): re-check then.

## What stays on
Policy demotion rules (P1-P8), the entry structure gate (`STRUCTURE_GATE_MODE=enforce`), `STRUCTURE_REQUIRE_DATA`, the structure trail is inactive in `log` mode (it only runs
in `tighten` / `exit`). With `STRUCTURE_STOP_MODE=off` no structural levels are computed, so `decision.stop_basis` is `atr` or `flat`.

## Turn them back on (one env value each)
`STRUCTURE_PREPARE_UPGRADE=1`, `STRUCTURE_STOP_MODE=on`, `STRUCTURE_EXIT_MODE=tighten` (or `exit`), `STRUCTURE_MAX_AGE_DAYS=7`. A value already in the VM `.env`
overrides these defaults: run `grep STRUCTURE_ .env` and remove old `=exit` / `=on` / `=1` lines, or the change will not take effect.
Run `scripts/structure_gate_check.py` before re-enabling any of them.

## Tests
`decision/tests/test_group353_structure_policy.py` (P9 test now opts in; new default / opt-in test), `real-trade-service/tests/test_group365_structure_exit.py` (fallback mode
`log`), `real-trade-service/tests/test_group415_structure_defaults.py` (new: config defaults via a private config load, structural_levels off / on, the 5-day calendar
check, `.env.example` text). `docs/ENV_REFERENCE.md` regenerated.

## Verified / not verified
Run here without pytest: the 31 decision policy tests through a stand-in runner, the config defaults against the real `config.py`, `gen_env_reference.py --check`,
`sync_market_structure --check`, `check_shared_copies.py`, py_compile of the edited tests. NOT run: the real pytest suites (no pytest here), so run
`bash scripts/docker_test.sh real-trade-service` / `decision-prediction-service`, and position-stocks, api-gateway and analysis-intelligence for the age default.
