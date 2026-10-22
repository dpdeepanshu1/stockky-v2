# B17: live vs backtest, per rule (offline readout, changes nothing)

1. Export live rows from real-trade (ask for many rows; the default `recent` is 40):
   `curl -s -H "Authorization: Bearer $ADMIN" "$RT/structure/observations/REAL?days=90&recent=100000" > live.json`
2. Backtest report: `python3 scripts/ms_ab_report.py HIST_DIR` -> `ab_out/ab_report.json` (its horizon is used for the live returns too).
3. `python3 scripts/live_vs_backtest.py --live live.json --ab ab_out/ab_report.json [--min-n 30]` -> `lvb_out/live_vs_backtest.{json,md}` next to live.json.

Per rule and for the whole gate: gap = mean return of entries let through minus those turned away. Backtest gap = candidate mean net minus that rule's rejected-days mean net. Live gap = `passed` rows (no reasons) minus rows whose reason text names the rule, with a 90 % bootstrap interval over whole observation days. Gaps cancel costs, so gross live and net backtest compare.

Status: `confirmed` / `weaker_than_backtest` / `reversed` / `inconclusive` / `no_backtest_edge` / `insufficient_live` (under max(--min-n, 15) rows per side, or under 15 backtest rejected days: the B15 floor). Act on `reversed` and `weaker_than_backtest` (put that rule in log mode); `insufficient_live` means keep collecting.

Limits: live rows are every /decide candidate, the backtest a technical-only baseline, so read direction and size, not equality; the live export stores reason text, not rule ids, so rules are matched by wording (unmatched rows are counted as `unmapped`); covers the structure gate only (52-week, Kronos, TimesFM rows are not in that table). No live behaviour changed.
