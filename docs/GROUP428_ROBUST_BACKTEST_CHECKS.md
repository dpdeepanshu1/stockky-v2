# Group 428 - outlier-proof checks for the entry-rules and legacy-gate backtests (tooling only, no service code changed)

## Why
The 18 Oct `entry_rules_report` (359 symbols, 118,916 entries) said expiry-day entries earned +3.9 % in 10 sessions, monthly expiry +17.8 %, and the day before a holiday +15.8 %, with 90 % intervals reaching -51 %. No such edge exists. Two things were wrong with the evidence, and the old verdict could see neither:
1. A few huge returns (unadjusted splits / bonuses, bad candles) drag a mean far from the typical entry.
2. Every stock shares the same calendar days, so 118,916 entries are about 1,200 independent days. The old bootstrap resampled (symbol x month) clusters, which treats one market-wide day as many independent ones.

## What changed (`scripts/_robust.py`, used by both `entry_rules_backtest.py` and `legacy_gate_backtest.py`)
Per rule / gate, next to the old columns:
| column | meaning |
| --- | --- |
| median left alone / acted-on, median edge | the typical entry; one extreme return cannot move it |
| clipped edge | the edge after clipping every return to its own 1st-99th percentile |
| day-cluster 90 % CI | bootstrap of the clipped edge resampling whole ENTRY DAYS |
| extreme rows | entries with abs(return) over 100 % (almost certainly data) |
| verdict (robust) | `helps` / `hurts` only when the day-cluster CI, median edge and clipped edge all agree on the sign, else `no_edge` (`insufficient_sample` unchanged) |

**Part 1b**: the same table after dropping entries whose window (20 sessions before the signal to the end of the horizon) holds a one-day close move over `--jump-pct` (default 35 %; NSE circuits stop a real stock far below that), with the number of entries and symbols dropped. If a rule's verdict changes between the full table and 1b, the old result was the data, not the rule.
**Part 2**: a stop / target cell is `BETTER` / `WORSE` only when the 90 % interval is on one side of zero AND the difference is at least `--min-effect-pp` (default 0.05 percentage points per entry); smaller gaps are `better_tiny` / `worse_tiny` (real but not worth a config change; the 18 Oct "BETTER" cells were 0.013-0.015 pp, about 1.5 basis points against a 0.4 % round-trip cost).
Trust the robust verdict over the first table. Nothing about the replay itself (entries, exits, costs, calendar) changed.

## Run (VM)
```bash
cd ~/stockky-v2
python3 scripts/entry_rules_backtest.py hist400 --jobs 2
python3 scripts/legacy_gate_backtest.py hist400 --jobs 2
```
Reports: `hist400/entry_out/entry_rules_report.md` and `hist400/gate_out/gate_report.md` (+ `.json`). Both finish in under a minute on 359 symbols.

## Verified
`scripts/test_robust_backtest.py` (8 tests: a real difference reads `helps` on every measure; one absurd return cannot turn `no_edge` into `hurts`; `hurts` needs all three to agree; empty / one-sided input never raises; jump detection; rows carry `day` and `jump`; the tiny-effect label; both reports end to end) plus the existing entry-rules and legacy-gate tests: 31 passed. A synthetic check with unadjusted 1:3 jumps injected showed the old table calling pre-holiday entries `hurts` (mean +13 %) while the robust verdict said `no_edge`. NOT yet run on the real 359-symbol history.
