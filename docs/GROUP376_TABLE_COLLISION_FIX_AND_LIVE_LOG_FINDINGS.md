# Group 376 - table-name collision fix + findings from the live log and the 346-symbol backtest

## Bug fixed: `structure_observations` defined by two services
real-trade-service (group 356: symbol/source/mode/obs_day/verdict...) and decision-prediction-service / training (group 374: symbol/obs_date/engine_version/
policy_mode/final_decision...) both used the table name `structure_observations`. On the Oracle VM every service connects to the same ADMIN schema, so it is one
table; `create_all` skips an existing table, so the second service queried columns that did not exist (ORA-00904) and the best-effort writers failed silently.
Consequence: the group 374 all-decision observation log could not have been recording on the VM. Tests passed because they used a fake session and a per-service SQLite.
Fix: the training table is now `decision_structure_observations` (unique constraint `uq_decision_struct_obs_key`); new `scripts/check_table_name_collisions.py`
(run in CI) fails when two services define one table name with different columns (intentional identical copies such as `stockky_shared_*` and the read-only
`trade_credentials` mirror pass). The old `structure_observations` table is real-trade's and is left untouched; nothing from training was ever stored in it.

## 346-symbol random-sample A/B (T+10, net of 0.4% costs)
Verdict `no_edge`. Edge +0.065 pp, 90% CI [-0.119, +0.251], halves -0.178 / +0.294. The earlier 129-symbol result (+0.537 pp) was mostly the alphabetical sample.
Baseline mean +0.056%, candidate +0.121%, both near break-even after costs; median -0.8%, win rate 45%. Gate keeps 9.4% of baseline days, almost all of the
rejection comes from `room` and `rr` (61.2% each, nearly the same days). Exploratory per-rule: rr and bearish help slightly (+0.05 / +0.03 drop-one), room hurts
slightly (-0.10), the rest ~0. Regime: bull +0.27 pp, bear -0.09 pp (same sign in both runs); high-vol flipped from +1.33 to +0.01, so treat it as noise.
Stop-first 50% vs 37% for the candidate is partly by construction (the gate prefers a nearer stop and a farther target). No threshold was changed.

## Live log (10:17-10:19 IST, right after a full restart) - why there was no stock
See the chat reply for the full list. In short: a red tape (volume-shock needs +2.5% on the day; most of 67 names were -2% to -7%), 8 of 13 candidate rejections
were the always-on "top 12% of the 52-week range" rule (it rejects fresh highs; its message claims "choppy/weak market" but the code does not check the regime),
558 watchlist rows were skipped for trading >1.5% below the catalyst price, and a restart holds /check for ~5 minutes. The legacy candidate gates have never been backtested.
