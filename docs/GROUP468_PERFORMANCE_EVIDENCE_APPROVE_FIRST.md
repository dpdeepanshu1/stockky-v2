# Group 468: performance block, charges share, log-only evidence, slippage review, calibration compare, approve-first

Nothing here changes what a gate decides, except approve-first, which is OFF by default.

## Reports (items 2 and 3)
- `scripts/records_report.py` and `services/real-trade-service/trade_records.py`: `performance_block` (daily net P&L: max drawdown in rupees with
  start / end day, days underwater, Return/MaxDD, longest winning-day run, best day, equity curve), `charges_share_of_gross_profit_pct`.
  Drawdown is additive rupees from a book that starts at 0; a first-day loss is a drawdown. `exit_slippage_pct` (report script) only measures
  `stop_hit` exits against the INITIAL stop, because records carry no per-exit trigger price (a trailed stop makes it approximate).
  True per-exit slippage needs a trigger price stored on the closing order in `build_records`: not built.
- QuantStats cross-check lives in `scripts/test_group467_performance.py` and skips when QuantStats is absent. It is not a dependency.

## Log-only evidence (item 8)
- `candidate_engine/log_context.py`: Mansfield RS (daily-bar version, default 200 bars, benchmark `^NSEI`) and breadth (% of the symbols this
  service has read in the last 30 min that close above their 50-day EMA; None until 20 symbols). Written to the candidate's
  `raw_payload["_log_only"]`, copied into trade records as `log_mansfield_rs`, `log_above_ema50`, `log_breadth_pct`, and grouped in the
  expectancy report (`by_mansfield_rs`, `by_breadth`) and `records_report.py`. Kill switch: `CANDIDATE_LOG_CONTEXT=0`.
- Breadth is the breadth of the candidate engine's own universe, NOT the whole exchange. Mansfield RS here is a daily-bar cousin of the
  classic 52-weekly-bar number. Do not gate on either before a report shows an edge (about 100 trades).

## Entry slippage review (item 9)
`python3 scripts/entry_slippage_review.py rt_*.json` prints mean / median / worst entry slippage, rupees paid above the signal, share of gross
profit, by hour and source tab, and a verdict (`material` / `not_material` / `too_few_trades`; thresholds 0.15 % mean or 10 % of gross profit,
30 trades minimum, all adjustable and all judgement calls). Build the limit-order walk only on `material`.

## Calibration snapshot and compare (item 10)
`python3 scripts/calibration_snapshot.py snapshot --out-dir calibration_out --env ~/stockky-v2/.env --label NAME` stores gate settings (secrets
never) plus per-seed A/B metrics; `compare A.json B.json` shows changed settings and metric movement, and warns when seeds differ, a seed is
incomplete, or the settings are identical. Not wired into `vm_calibrate.sh`; run it by hand before and after a settings change.

## Approve-first mode (item 11)
- `APPROVE_FIRST_N` (default 0 = off, max 20), `APPROVE_FIRST_TTL_MIN` (default 15). REAL only, automatic entries only (manual BUYs, exits and
  position-stocks are untouched).
- The gate settings are fingerprinted (env names STRUCTURE_, PS_STRUCTURE_, CANDIDATE_, ENTRY_, RISK_, TIMESFM_, BROKER_STOP, minus secrets). A new
  fingerprint, or a new N, starts a fresh round: the next N automatic REAL entries each wait for approval.
- A held candidate stays queued (no decision row), you get one Telegram alert per symbol, and the dashboard shows an "Entry approvals" panel
  (Real Automatic Trade, overview). API: `GET /approvals/REAL`, `POST /approvals/REAL/{symbol}/approve`, `.../reject`, `POST /approvals/REAL/reset`.
- An approval is used up only when the BUY was actually sent. Unapproved within the window = not bought. The helper fails OPEN: any storage error
  lets the entry proceed as before.
- Caveat: one held candidate occupies a slot of that cycle's shortlist (`ENTRY_MAX_NEW_PER_CYCLE`); lower-ranked picks wait a cycle.
- Because the fingerprint includes every STRUCTURE_/CANDIDATE_/ENTRY_/RISK_ variable, editing any of them (even one you do not think of as a "gate")
  starts a new round. That is deliberate: it is what "after a config change" means here.

## Not verified here
No pytest, sqlalchemy, httpx or node_modules in the build sandbox: the new test files ran through a stand-in runner; `entry.py`, `candidates.py` and
`main.py` compile but the existing suites around them (test_candidates_*, test_group290, entry tests) were not run; the frontend was only syntax-checked.


---

# Group 469 follow-ups (the items that were "not built")

- **True per-exit slippage.** `trade_records.build_records` now loads `TradeExitDecision` rows and stores `exit_trigger_price`, `exit_trigger_source`
  and `exit_slippage_pct` on every record. The trigger is the LTP stored on the position's last real exit decision (HOLD / TRAIL_STOP rows are not
  exits) = `decision_ltp`; a stop exit with no decision (the broker-side stop fired on its own) uses the position's final stop = `final_stop`;
  anything else has no trigger and nothing is guessed. The decision LTP is the price seen when the exit was decided, so it measures the slippage of the
  sell order itself (market-order slippage, MPP effect), not the stop's own gap. Records made before this change carry None. Shown overall and per exit
  reason in `GET /records` expectancy (`exit_slippage`) and in `records_report.py`; the old stop-hit-only proxy is only used when no record has the field.
- **Calibration snapshot in `vm_calibrate.sh`.** Opt-in: `SNAPSHOT_LABEL=name bash scripts/vm_calibrate.sh` (also `SNAPSHOT_STORE`, `ENV_FILE`). A failed
  snapshot prints a note and never fails the run.
- **Window drawdown in the daily Telegram message.** `build_daily_snapshot` stores `week_performance` and the message adds "Last N days drawdown ...".
  It is the drawdown over the rolling window (default 7 days), not the whole book; the whole-book number stays in `/records` and `records_report.py`.
- **Approve-first for position-stocks.** `orders/approve_first.py` (same state machine, JSON-file state in the `control-state-ps` volume), checked in
  `attempt_entry` after every other gate and before capital is reserved (the symbol claim is released while held); `GET /approvals`,
  `POST /approvals/{symbol}/approve|reject`, `POST /approvals/reset`; the same panel on the Position Stocks tab. Fingerprint prefixes: ENTRY_, BOARD_,
  OPENING_, MIN_, SCALP_, ADAPTIVE_, STRUCTURE_, PS_STRUCTURE_, SHORTLIST_, QUALITY_, REARM_, CALENDAR_, VIX_, STOP_, MAX_, TRAIL_, POSITION_, MARKET_,
  WINDOW_, LOSS_, EXIT_, SIZING_, RISK_. Position-stocks has no demo mode, so every automatic entry is held while a round is open. The two services keep
  separate state and separate `APPROVE_FIRST_N` is shared through the one `.env` (both read the same variable).

## Real Money Calibration page (group 469)
The two new scripts are runner jobs (`calibration-runner-service/jobs.py`), listed on the page under "Weekly calibration" and in the written plan:
- `entry_slippage` - Entry slippage review (needs the saved records); part of the **Friday review pack**, after the closed-trade report.
- `calibration_snapshot` - stores the switches in force in both services (GET /control/flags, effective values, `real-trade:KEY` / `position-stocks:KEY`)
  plus the per-seed A/B metrics of the last calibration, in `<calibration_out>/snapshots/` on the runner volume. The runner has no `.env`, so the switches come from the services.
- `calibration_compare` - compares the two newest snapshots. Both snapshot jobs are the last two steps of the **Weekly calibration pack**.
`records_report` (Friday pack) already prints the performance block, charges share, exit slippage and the two log-only tables. The unit tests themselves still run with
`scripts/docker_test.sh` (they are not page jobs): `calibration-runner-service/tests/test_group469_new_jobs.py` covers the new jobs.
