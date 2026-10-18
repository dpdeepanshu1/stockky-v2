# Group 426 - entry rules + stop/target width backtest (tooling only, no service code changed)

`scripts/entry_rules_backtest.py` (+ `scripts/test_entry_rules_backtest.py`, 7 tests) - the companion to `legacy_gate_backtest.py` (candidate gates) and
`structure_backtest.py` (market-structure rules). It covers what those two do not, on the same `--sample 400 --seed 7` history folder:

| Part | What it tests |
| --- | --- |
| 1 | Expiry day (weekly / monthly), pre-holiday, Friday, thin average turnover (1 / 3 / 10 crore), price floor (Rs 20 / 50): are those entries worse, after costs? Same scoring, cluster bootstrap and verdicts (`helps` / `hurts` / `no_edge` / `insufficient_sample`) as `legacy_gate_backtest.py`. |
| 2 | Stop / target width: 1.0-3.0 x ATR stop and 1.0-3.0 R target, against the live setting (1.5 x ATR, 2R), paired difference with a 90% interval (`BETTER` / `WORSE` / `same`). |

Run (VM): `python3 scripts/entry_rules_backtest.py ~/stockky-v2/hist400 --jobs 2` (or inside the real-trade-service image like the Kronos backtest). Output: `hist400/entry_out/entry_rules_report.md` + `.json`.

Limits: daily bars only (no intraday stop-outs, no VWAP / opening / depth gates); every sampled day is replayed, not only live-scanner candidates; weekly expiry = Tuesday from
`--expiry-switch` (2025-09-01) and Thursday before - check both against NSE circulars; the pre-holiday tag needs `^NSEI.json` in the folder (the report says which calendar was used).
Verified in the build sandbox: 7 new tests pass (calendar maths, stop/target walk, no look-ahead, end-to-end on synthetic candles); `test_legacy_gate_backtest.py` still passes. NOT run on real history.
