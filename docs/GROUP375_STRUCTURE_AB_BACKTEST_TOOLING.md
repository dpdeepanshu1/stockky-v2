# Group 375 - structure A/B backtest tooling (WS-7 / gate G2)

The biggest open gap after group 374 was: the structure thresholds were never checked against real daily history. The sandbox cannot fetch market
data, so this group makes the check runnable end to end on your side. A session that began this work was cut off before saving anything, so the
pieces below were rebuilt from the repo.

## What was added
- `scripts/fetch_history_for_backtest.py` - downloads daily candles from the market-data `/history/{symbol}` into one JSON per symbol (plus `^NSEI.json`
  as benchmark). Symbols from `--symbols`, `--symbols-file` or `--universe-url` (the gateway's `/api/universe`). Resumable (keeps good files), retries,
  skips 404/delisted at once, rejects unsafe names (`/`, `..`), and exits 1 when the benchmark is missing or more than `--max-failures-pct` failed.
- `scripts/ms_ab_report.py` - replays the engine with no look-ahead and compares, on identical days:
  - baseline = above EMA20 and not extended (0 <= stretch <= 3 ATR), a technical-only proxy (news / fundamentals are not archived point-in-time);
  - candidate = baseline minus days any structure rule rejects (same rules and thresholds as the gates, via `rule_flags`).
  Entry is the next session's open + 0.10% slippage; T+h counts the entry session as 1; 0.30% round-trip cost is subtracted; a bar touching both stop
  and target counts as a stop. Only rows with a complete 20-session outcome are used, so all horizons share the same days.
  Reports mean/median net return, win %, profit factor, return vs NIFTY, stop-first / target-first %, by horizon (1/5/10/20), by rule, by regime
  (bull/bear by NIFTY 200-day SMA, high/low volatility by NIFTY ATR% vs its trailing median), and the edge = candidate - baseline with a cluster bootstrap
  (symbol x month blocks, seed 7) and a first-half / second-half split.
  Verdicts: `insufficient_sample` (< `--min-n` per group), `pass` (edge > 0, bootstrap 5th percentile > 0, both halves positive, candidate mean net > 0),
  `hurts` (bootstrap 95th percentile < 0), else `no_edge`. `--jobs N` runs symbols in parallel.
- `.github/workflows/structure-backtest.yml` - manual run: fetch -> report -> run summary + artifact. Secrets: `MARKET_DATA_URL` (existing), `API_GATEWAY_URL` (existing, for the universe).
- `scripts/test_ms_ab_report.py` (11 tests, also added to `service-tests.yml`).

## Update: walk-forward tuner priced like a real trade
`scripts/structure_backtest.py` entered at the signal-day close with no costs, so thresholds tuned with it would have looked better than anything tradable.
It now defaults to `--entry next_open` (next open + `--slip-pct` 0.10, minus `--cost-pct` 0.30, same as `ms_ab_report.py`); `--entry close` keeps the old view.
The output records the entry mode and costs. It also used to count `^NSEI.json` as a tradable symbol; the benchmark is now skipped.

## Update 2: first real-data run and what it changed
First run on the Oracle VM (129 symbols, 5y of Dhan daily candles, step 3, T+10): baseline 20,916 signals, candidate 1,972 (9.4% kept).
Edge +0.54 pp, 90% bootstrap CI [-0.01, +1.29], P(edge>0) 0.94, both history halves positive (+0.54 / +0.50): verdict `no_edge` (close, not significant).
Baseline alone: mean net +0.12%, median -0.80%, win 45.2%, profit factor 1.04. NO threshold or mode was changed from this: not significant, and the sample was the
alphabetical head of the universe. Prices looked split-adjusted (14 one-day moves >25% in 142,218 bars, in 4 symbols).
Operational findings: market-data caps history at `MAX_HISTORY_ROWS=260` (~1y) by default, so 5y needs `MAX_HISTORY_ROWS=1400` + `MAX_HISTORY_PERIOD=5y`
in `.env` temporarily (revert afterwards); 129 symbols took ~12 min on 2 cores.
Tooling changes: `fetch_history_for_backtest.py --sample N --seed` (random, seeded cross-section; `--limit` is the alphabetical head), the workflow now samples;
`ms_ab_report.py` verdict `no_data` (an empty history folder used to read as `insufficient_sample`), progress lines on stderr, and per-rule `solo_edge_pp` /
`drop_one_edge_pp` in the by-rule table (exploratory: six rules compared). 15 tests.

## How to run
Actions -> Stockky Structure Backtest -> Run workflow (start with `limit=150`). Or locally:
```
MARKET_DATA_URL=https://... python3 scripts/fetch_history_for_backtest.py hist --universe-url https://GATEWAY/api/universe --limit 150
python3 scripts/ms_ab_report.py hist --jobs 4
```
Cost: the replay is O(n^2) per symbol; about 9 s per symbol for 5 years at `--step 3` on one core.

## Reading the result
- Thresholds are the live defaults and are not tuned here, so the whole history is out of sample for them. Tuning is `scripts/structure_backtest.py` (walk-forward) - run it only after this report says something.
- A `pass` is evidence on this history, not a promise. Survivorship bias: the universe is today's listed symbols, so delisted names are missing and results are flattered.
- Do not change thresholds or turn `SNIPER_PREPARE_STRUCTURE_MODE` off/on from a thin or single-regime result.

## Not verified
- Never run on real data (sandbox has no market-data access). Verified on synthetic random-walk candles (verdict `no_edge`, as it should be) and with unit tests incl. a local HTTP server for the fetcher.
- The workflow has not run on GitHub.
