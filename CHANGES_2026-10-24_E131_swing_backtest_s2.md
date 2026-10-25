# E131: swing-hold plan step S2, the swing momentum replay (evidence only)

Plan: `Stockky_Swing_Momentum_Plan.md`, step S2. **Read-only evidence: nothing is traded, no switch changes, no service code changes.** It decides whether step S3 (the shadow lane) is built at all.

| File | What |
| --- | --- |
| `scripts/swing_backtest.py` | Replays "buy a top-momentum stock, hold up to 5 sessions" on the same HIST_DIR as `entry_rules_backtest.py`. Scores every stock each signal day with the SAME module the live `/swing/rank` uses (`swing/momentum.py`), ranks the folder together, enters at the next open plus slippage, exits by `hold` (close of session 5), `stop` (ATR stop) or `trail` (stop ratcheted to close minus stop %). Costs are the real Dhan delivery round trip at each trade size (3,000 / 10,000 / 20,000 / 50,000, plus your `--primary-size`). |
| `scripts/test_swing_backtest.py` | 13 tests (plain asserts or pytest) |
| `services/calibration-runner-service/jobs.py` | New runner job `swing_backtest` (monthly group, heavy, closed-market window, resumable, needs `fetch_history`). Not added to any pack: run it on demand. |
| `services/calibration-runner-service/Dockerfile` | Copies `swing/__init__.py` and `swing/momentum.py` into the runner image (stdlib only) |

Verdict (first line of the report): primary setup **SCREEN** (top 20% by momentum AND above its 200-day average), exit **trail**, at `--primary-size` (default Rs 20,000). **USEFUL** needs all of: the 5th percentile of the net mean per trade above zero (resampling whole days), the edge over the other ranked stocks `helps` in the robust test (day-cluster interval, median and clipped views), and at most one calendar year with 30+ entries negative. **NO_EDGE** otherwise, with the failed test named, and a note when a gross edge is eaten by costs. **INSUFFICIENT** when there are too few entries. The other four setups (ALL, TOP, SCREEN_DIP, SCREEN_BRK) and all sizes are exploratory and carry no verdict.

Cost figures (from `config.py`, flat DP charge Rs 12.50 + GST = Rs 14.75): Rs 3,000 trade 0.71% round trip, Rs 10,000 0.37%, Rs 20,000 0.30%, Rs 50,000 0.25%. This corrects the earlier plan draft (Rs 17.4 and 0.8%).

Checked here: all 13 tests pass as a plain script, including the delivery cost matching `cost_model.estimate_round_trip_cost` to the paisa; 9 deliberate breakages (look-ahead window, DP charge, trailing ratchet, gap-open exit, entry price, each of the three verdict conditions, the per-year minimum, plus the verdict combiner) are each caught; the runner job builds its command line; repo guards pass. **Not run here:** pytest, the runner tests, the Docker build, and the replay on your real history folder.

Honest limits (also printed in the report): daily bars cannot show intraday stop-outs; every 3rd session is sampled so neighbouring days overlap; the folder holds today's symbols (survivorship flatters momentum); one primary setup is declared on purpose so the exploratory rows are not findings.
