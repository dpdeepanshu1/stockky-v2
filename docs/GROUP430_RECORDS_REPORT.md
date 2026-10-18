# Group 430 - `scripts/records_report.py` (closed-trade report) and CI coverage for the backtest scripts

Tooling only: nothing here changes a trading decision, and no service code changed.

## 1. `scripts/records_report.py`
Replaces the throwaway `~/rt_split.py`. Read-only, standard library only. It reads the exports of real-trade-service `GET /positions/{mode}/records` (files holding `{"records": [...]}`; `days` 1-60, `limit` up to 1000, REAL mode needs the admin login) - export them with `python3 scripts/export_records.py` (group 431) - de-duplicates by `position_id` (a later file wins) and prints:
- totals (gross, net, charges and the charges' share of a gross loss, mean / median / win %), the best trade and the net without it, and the net without every trade beyond `--outlier-pct` (default 15 %) - one big winner or a -40 % gap can no longer hide inside a mean;
- tables by decision label, source tab, tier, entry hour IST, exit reason, hold bucket and stop basis, each with n, mean, MEDIAN, win %, gross, net, charges, and a `(few)` mark on any group under `--min-n` (default 10) trades - noise, do not act on it;
- order-flow buckets (best-5 imbalance, 20-level imbalance, spread at entry) from the group 427 record; trades made before it show as `unknown`, and the report says so when none carries it yet;
- a stop-overshoot line (how many points MORE than the planned stop the stopped trades lost, when the stop distance was recorded), entry slippage, every position held a day or more and then closed by `eod_squareoff`, and every `ghost_closed` / `emergency_gap_down`.
```bash
python3 scripts/records_report.py rt_*.json                       # everything, REAL
python3 scripts/records_report.py rt_*.json --since 2026-10-01   # trades opened on/after that UTC day (the post-September behaviour)
python3 scripts/records_report.py rt_*.json --mode DEMO --json out.json
```
Win % is the share of trades with `return_pct` above zero (same as the old script, so September's 63 trades reproduce: net -1,439, 63 trades). A rule is worth changing only when a bucket is clearly worse on about 100 trades.

## 2. CI
`.github/workflows/service-tests.yml` never ran `test_entry_rules_backtest.py` (group 426) or `test_robust_backtest.py` (group 428), and did not watch their scripts. Both, `test_records_report.py`, and the scripts they cover (`entry_rules_backtest.py`, `_robust.py`, `_progress.py`, `fetch_history_for_backtest.py`, `records_report.py`) are now in the push and pull-request path filters and in the "Repo scripts" step.

## Verified
`scripts/test_records_report.py`: 8 tests (parsing, de-duplication / mode filter / bad files, medians and outlier-free totals, `--since` / `--until`, order-flow buckets and the not-recorded-yet note, stop overshoot and late-eod listing, `(few)` mark / empty / missing fields, CLI end to end). Workflow YAML parses. Scripts suite: 67 tests expected (your run: 59, plus these 8).

## group 434: --origin filter

`records_report.py` and `hold_bucket_check.py` take `--origin system|imported|all` (default all). Use `--origin system` to judge the bot's own entry rules: broker-imported positions did not pass through those rules, and their `opened_at` is the import time (often just after 15:00 IST), so their hold length and "carried overnight" status are not real.
