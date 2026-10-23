# Remaining tasks

> E53 (this zip): gateway router split group 4 - Buy Sniper edge report/evaluate and the Surprise static peek moved to `gateway_routers/scan_extras.py` (docs/E53_GATEWAY_SCAN_EXTRAS.md). The simple moves are now used up: every remaining handler needs a helper that lives in `main.py`, so the next step is a `gateway_support/` module (its own release). Everything open from E51/E52 is unchanged.

> E52 (this zip): gateway router split group 3 - the six IPO routes + `IpoAddRequest` now live in `gateway_routers/ipo_routes.py` (docs/E52_GATEWAY_IPO_ROUTES.md). On the VM: `cd services/api-gateway && bash run_tests.sh`, rebuild api-gateway, `curl /ipo/list`. Everything from E51 below is unchanged and still open: add `SCHEDULER_CONTROL_KEY`, your VM items, C21 live (needs B-tools evidence), Docker images on `pip install ./shared` (own release), the remaining helper-dependent gateway groups.

> E51 (this zip): E49 gateway routers group 2 (13 more handlers), E50 installable market-structure package, E51 scheduler alert switches on the calibration page (docs/E49_E51_ROUTERS_PACKAGE_ALERT_SWITCHES.md). On the VM: add `SCHEDULER_CONTROL_KEY` to `.env`, `docker compose up -d --build api-gateway notification-scheduler-service frontend`, open Calibration -> Alert switches. Run `cd services/api-gateway && bash run_tests.sh`, `bash scripts/docker_test.sh notification-scheduler-service`, `npm ci && npm test && npm run build`. Still open: the helper-dependent rest of the gateway split, switching the Docker images to `pip install ./shared` (own release), C21 live (needs the B-tools evidence), and your VM items (rotate Upstash token + `SESSION_SECRET`, delete the runner output holding the admin JWT, confirm no "Safety check at boot" alert after restart).

> E48 (this zip): gateway router split started with one tiny group (price alerts, docs/E48_GATEWAY_ROUTER_PILOT.md). On the VM: `cd services/api-gateway && bash run_tests.sh`, rebuild api-gateway, `curl /price-alerts`. Still open: the rest of the split (one group per release), installable package, C21 live.

> E47: the risk-per-trade calibration switch now really reaches the engine (docs/E47_RISK_SWITCH_REACHES_ENGINE.md). On the VM: `bash scripts/docker_test.sh`, rebuild real-trade-service, change the value on the page and check `GET /risk-config/REAL` shows it. Still open: gateway router split, installable package, C21 live.

> E46: the four open decisions are made (docs/E46_DECISIONS_AND_PIN_ALIGNMENT.md): risk per trade 2.0, Position Stocks entry cap 10, no new enforcement, sub-app pins aligned to the parent. On the VM: `bash scripts/docker_test.sh`, rebuild real-trade-service + position-stocks-service, expect one `migrated ... risk_per_trade_pct 5.00% -> 2.00%` log line. Still open: gateway router split and installable package (need pytest + a deploy-and-watch cycle), C21 live (needs the B-tools evidence).

> E44: E31 route splitting done in App.tsx (docs/E31_ROUTE_SPLITTING.md). On the VM: `npm ci && npm run build`, confirm separate chunks, then open every tab once (spinner, then the page) and try the low-balance deposit prompt. Still open: F33 rest (gateway routers, installable package, merge the 11 requirements files), A6, C21 live, scheduler switches on the page.

> E43 (this zip): F33 part 1 built: nightly backup + restore test (`scripts/backup_state.sh`, docs/F33_BACKUP_AND_RESTORE_TEST.md). On the VM: `bash scripts/backup_state.sh run`, `restore-test`, `schedule`; set `OFFSITE_CMD` so a copy leaves the VM. Still open from F33: gateway routers, installable package, merge the 11 requirements files. Also open: E31, A6, C21 live, calibration-page switches for the scheduler.

> E42 (this zip): the E28 notification is built (server-side baseline + 5 % move alert, docs/E42_WATCHLIST_MOVE_ALERT.md). Check on the VM after rebuilding notification-scheduler-service: `POST /scheduler/watchlist-moves/run?dry_run=true` (first call records baselines), then `dry_run=false` once a stock has moved. Still open: PATTERN_ALERTS_* / WATCHLIST_MOVE_* on the calibration page (scheduler needs admin auth first), A6, C21 live, E31, F33.

> E41 (this zip): the page now has time-of-day cut-offs (new `time` switch kind; docs/E41_TIME_CUTOFFS_ON_PAGE.md). A7 is on the page as `ENTRY_LAST_TIME_IST` (already 14:00). Still open: PATTERN_ALERTS_* switches (the scheduler needs admin auth first), A6, C21 live, E28 notification, E31, F33. Check on the VM: `tsc`, then Calibration -> Switches -> "Entry time limits" shows a time picker.

> E40 (this zip): E26 finished: pattern / regime chips now show at the top of the stock page (docs/E40_DECISIONCARD_BADGES.md). Check on the VM: `npm ci && tsc && vite build`, open a stock, the "Market mood & patterns" box must appear above the chart. Still open: PATTERN_ALERTS_* switches, time-of-day cut-offs on the page, A6, C21 live, E28 notification, E31, F33.

> E39 (this zip): Position Stocks now has a real daily entry cap (`POSITION_STOCKS_MAX_ENTRIES_PER_DAY`, ships 15, on the calibration page; docs/E39_PS_ENTRY_CAP.md). Pick your number. Still open: PATTERN_ALERTS_* switches, time-of-day cut-offs on the page, A6, C21 live, E26, E28 notification, E31, F33. Not run in the sandbox: pytest (the DB-counting test of the cap needs sqlalchemy).

> E27 (this zip): candles on 1m / 5m / 15m / 30m / 1h / daily, backend + frontend (docs/E27_CANDLE_TIMEFRAMES.md). Check on the VM after rebuilding api-gateway, analysis-intelligence-service, market-data-service and frontend: open a position's Candles block, pick 1m / 5m / 15m / 30m / 1h and confirm candles appear with a time axis (outside market hours they show the last session). Not run in the sandbox: pytest, `npm ci`, full tsc / vite build.

> E38 (this zip): the Real Money Calibration page now has every trading switch (docs/E38_CALIBRATION_SWITCHES.md) plus a guard test. Standing rule: every change = test + calibration switch + live wiring. Still open: PATTERN_ALERTS_* switches (scheduler service, not covered by the page), time-of-day cut-offs on the page, Position Stocks entry cap number, A6, A7, C21 live, E26, E28 notification, E31, F33.

> E37: E30 is done (reason + feeling on Position Stocks trades, win rate by feeling, docs/E30_JOURNAL_NOTES.md). Check on the VM after rebuilding position-stocks-service and frontend: Position Stocks -> Board tab -> Trade notes -> add a note, reload, it must still be there. Not run in the sandbox: pytest, `npm ci` / full tsc (the npm and pip registries were blocked), the live service. Still open: F33 (backups), E28 (notification part), E31.

> E32 (this zip): E25 candlestick chart and E26 pattern / regime badges are built, plus a candles block under every position in Real Trade and Position Stocks (docs/E32_CANDLES_AND_PATTERNS.md). F32 is done (secrets scan in CI, see docs/F32_SECRETS_SCAN.md; the full pytest already ran in CI via the service-tests matrix). C21 is done in E35 (patterns logged, log-only; docs/C21_PATTERN_LOGGING.md). E29 is done in E36 (daily watchlist digest, docs/E29_PATTERN_ALERTS.md). Still open: F33 (backups), E28 (notification part), E31. (group 470 status, 2026-10-20)

## Built in group 470 (from reading the 19-20 Oct runner outputs; details `docs/GROUP470_RUN_OUTPUT_FIXES.md`)
Token redaction (JWT / `--token`), `gate_check` + `calibration_pack` `--ab` repetition, snapshot / checkpoint "input missing" on first run, `monday_check` closed-market skip,
TimesFM skip when the worker is not started, G1 footer + one 15 ms budget, slippage sign wording. Tests pass in the stand-in runner (no pytest in the sandbox); run `bash scripts/docker_test.sh` for the pytest-fixture tests.

## Do first (your side)
1. **Rotate `SESSION_SECRET`** (all services share it) and delete the `calibration_full` run (id `20261020-132048-calibration_full-15ee`) from the runner volume and from every copy of the zip: its `output.txt` holds a full admin JWT.
2. **Rebuild and restart the calibration-runner image.** The saved `records_report.json` has no `performance` / `exit_slippage` keys, so the runner is running scripts older than group 467-469. Group 470 fixes only take effect after the rebuild.
3. Check `docker logs` / the order book for **BLEL (entered 14 Oct 13:47, closed 15 Oct 15:00)** and **HDFCLIFE (entered 19 Oct 13:10, closed 20 Oct 08:57)**: both outlived the 15:00 square-off. HDFCLIFE was with `OVERNIGHT_HOLD_ENABLED=false`.
4. Position-stocks order `221261016371707` (`STAGNATION_EXIT_SELL_DEAD`: cancelled, zero fill, "position may still be open at the broker"): confirm at the broker that the position is flat.
5. If 50 ms was the intended G1 budget, set `G1_BUDGET_MS=50` (the monthly job is now stricter than before).

## Decisions for you (not changed in code)
- **Late entries.** 72 of 196 REAL trades were entered in the 15:xx hour: net -1,371, charges 1,182. Trades entered at 15:02 / 15:16 (CAMS, ADVAIT, BLUESTONE, TARIL) were carried to the next trading day. **Already fixed (A7 checked in E35):** a general 14:00 IST cutoff exists in both services (`ENTRY_LAST_TIME_IST` in real-trade, group 470; `SCALP_ENTRY_CUTOFF_IST` in position-stocks, group 479). The 15:xx entries above are from before that. Verify with `scripts/charges_split.py --since 2026-10-20`.
- **Structure gate** is `enforce` in both services while G2 and G3 FAIL (policy keeps ~9.5 % of signals, needs 30 %; two of three seeds `no_edge`; G4 has 2 of 20 sessions). Option: `log` until the gates pass.
- **real-trade** `service_advice` = TURN OFF (net -369 over 24 trades in the last 10 days; -3,134 over 196 trades since 1 Sep). Several real-trade entry gates were switched off on 19 Oct (VWAP gate, opening entry guard, depth re-check); one day of data cannot judge that change.
- **Stop overshoot:** 17 of 24 stop hits lost more than the planned stop (worst 17.55 points), 5 `emergency_gap_down` exits (mean -13.5 %). `BROKER_STOP_ENABLED=true` has been on since 19 Oct, so re-measure after a week; consider a liquidity / circuit filter if it persists.
- Attribution fields (`decision_label`, `source_tab`, `tier_name`, `stop_basis`, order-flow) are empty on most history (190 / 163 / 59 / 188 / 192 of 196); per-gate tables are only useful once new trades accumulate.
- Entry slippage review says `not_material` (mean -0.034 %): the limit-order walk is not needed. Kronos: `NOT PROVEN` (passed trades did worse). Leave both off.

## Earlier status (group 469)

## Built in groups 466-469 (needs your side to verify, see "Not verified")
Close-all and disarm (the group466-closeall zip: real-trade + position-stocks, dashboard), whole-book performance block, charges share of gross profit,
log-only Mansfield RS + breadth, entry-slippage review script, calibration snapshot / compare (also an opt-in step in vm_calibrate.sh), approve-first mode in real-trade AND position-stocks, per-exit trigger price + exit slippage on every record, window drawdown in the daily message.
Details: `docs/GROUP468_PERFORMANCE_EVIDENCE_APPROVE_FIRST.md`.

## Do on the VM (no code)
1. `BROKER_STOP_ENABLED=true` was seen live in the 19 Oct switch snapshot (admin override); confirm it survives a restart (`.env`).
   (E30: overrides are saved in the `control-state-rt` volume and re-applied at boot; real-trade now also sends a Telegram alert at boot if the stop is OFF or the override store is not persistent. After the next restart, check that no such alert arrives.)
2. Confirm the VM's static IPv4 is registered with Dhan and `/outbound-ip` returns it.
3. One live check that market exits and the `STOP_LOSS_MARKET` broker stop behave under Market Price Protection; grep the logs for MPP rejections.
4. Confirm no AMO orders are sent; one live pass of the token-expiry and auto-disarm path.
5. One DEMO close-all and one small REAL close-all before relying on it (group 466).
6. Run, on your machine: `npm install && npm run build`, and `bash scripts/docker_test.sh` (or pytest in each service). The group 466-468 code was only
   run through a stand-in test runner in a sandbox without pytest, sqlalchemy, httpx or node_modules.

## Wait for data, then decide
- Mansfield RS and breadth are recording on new candidates (`CANDIDATE_LOG_CONTEXT=1`). After about 100 closed REAL trades run
  `scripts/records_report.py` and read the two "log-only" tables. Build a rule only if a bucket is clearly better or worse.
- Entry slippage: run `python3 scripts/entry_slippage_review.py rt_*.json` once there are 30+ trades. Build the limit-order walk only on a `material` verdict.
- Calibration: the weekly pack now snapshots and compares automatically. For a settings change, run the page jobs `Calibration snapshot` before and after, then `Compare the last two snapshots`.
- Friday pack now includes the Entry slippage review (verdict needs 30+ trades).
- Exit slippage fills in only for trades closed after this version is deployed (older records have no trigger price).
- Optional: turn on approve-first (`APPROVE_FIRST_N=3`) the next time you change any STRUCTURE_/CANDIDATE_/ENTRY_/RISK_ setting on REAL.

## Not built
- Nothing from the group 466-469 list is left unbuilt. Open design questions only: a limit-order walk (only if the slippage review says `material`),
  and any rule based on Mansfield RS / breadth (only if a report shows an edge).

---

# Older notes (rewritten in group 416, after group 415)


The earlier list in this file (market-structure engine, "after group 349") described work that now exists: the policy
(`structure_policy.py`), the real-trade gate, the frontend panel, observation logging, the backtest tooling and the retest
branch. It was deleted rather than patched. Current state of the engine: `docs/GROUP339_MARKET_STRUCTURE_ENGINE.md`.
Current defaults: `docs/GROUP415_EVIDENCE_GATED_STRUCTURE_DEFAULTS.md`.

## Evidence gates (decide whether the structure rules earn their place)
| Gate | Status |
|---|---|
| G1 correctness | Not verified on real data: corporate actions on >= 10 real splits/bonuses, truncation invariance, `/analyze` p95 latency (live half: `scripts/analyze_latency_check.py`, group 436) |
| G2 benefit | Not met. A/B verdict `no_edge` (group 375: +0.54 pp, CI [-0.01, 1.29]; group 410: +0.35 pp, CI [-1.63, 2.44], 71 signals) |
| G3 stability | Tooling built (group 436: `structure_gate_check.py` prints G3 and a per-rule table). No result yet: needs the calibration run (>= 2 complete seeds) |
| G4 live | Not enough observed days |

Until G2 passes: `STRUCTURE_PREPARE_UPGRADE`, `STRUCTURE_STOP_MODE` stay off and `STRUCTURE_EXIT_MODE` stays `log` (group 415).
Run `scripts/structure_gate_check.py` on >= 2 seeds once the calibration run finishes (re-read seed 13's `summary.txt`; the earlier
`no_edge` may not be from that seed).

## Group 436: TimesFM parameter plan (built, OFF, unproven)
Service, log-only Gate 4g, offline probe and nightly batch exist (`docs/GROUP434E_TIMESFM_PARAMETER_PLAN.md`). Still to do, in order: run `selfcheck.py` on the VM (the model adapters have never run on real weights), run `scripts/tsfm_probe.py` on seeds 7 / 11 / 13 and `--combine` (Gate A), then - only on a pass - the nightly batch + `TIMESFM_GATE_DEMO_MODE=log` and a replay (Gate B). There is no enforce mode and none should be added before Gate B.
Calibration: use `RESUME=1 bash scripts/vm_calibrate.sh`; `python3 scripts/calibration_status.py --out-dir calibration_out` says which seeds are really complete.

## Group 422: policy record-only path (shipped, OFF by default)
To measure a stricter rule before it costs money: set `STRUCTURE_POLICY_SHADOW_RULES=P7,P8` (or `STRUCTURE_POLICY_MODE=record` for everything) on decision-prediction-service, let observations accumulate, then compare `features.would_be` with `final_decision`. Note that shadowing P7 / P8 LOOSENS live BUY NOW gating (those rules stop applying), so only do it deliberately. The real-trade `structure_gate` and the sniper gate are separate gates and are not covered. Group 423 shipped the strict independent-group count (see below).

## Group 423: strict independent groups (shipped, OFF by default)
Workflow: `STRUCTURE_POLICY_PARAMS_JSON={"groups_strict_min":2}` + `STRUCTURE_POLICY_SHADOW_RULES=P7X` on decision-prediction-service, let observations accumulate (the field only exists for scans made after the engine copy is deployed), then compare outcomes of days where `features.groups_strict` < 2 versus the rest before going live. Not wired into `ms_ab_report` / replay (`backtest.POLICY_RULES`) because old stored summaries lack `ms_groups_strict`; add that once enough new rows exist. No evidence yet that it helps (gate G2 is no_edge), so do not enable it live on theory.

## Open design question (from the 16 Oct hold-time check, group 421)
The daily-structure stop / target are multi-day levels, but real-trade squares off at ~15:00 unless the label is overnight-hold eligible, so structure R:R is never realised for BUY NOW / PREPARE entries. The card text now says so (group 421). Whether to add a swing-carry path for structure-confirmed entries is undecided and needs same-day vs next-day vs 2-5 day data: run `scripts/hold_bucket_check.py rt_*.json` on all exports.

## Not built
- `independent_groups` counting in `scoring.py` (the policy has its own group count). (Intraday 15m/5m structure is now built as record / evidence only, group 441, see below.)
- Walk-forward calibration from real data; `groups_min=2` is a judgement call.
- WS-2: position-stocks still keeps legacy board bodies behind `BOARD_USE_MS_ENGINE`.
- Retest/CHOCH entry branch exists (group 414, `STRUCTURE_RETEST_ENTRY`, default OFF) but has no replay evidence yet. The decision card shows a retest badge since group 417; the Buy Sniper card carries `retest_entry` since group 426 (badge shows only when the branch is on).

## Checks that need your VM / live market
- `python3 scripts/env_structure_check.py ~/stockky-v2/.env --strict` (group 434d; replaces the plain grep) - old `STRUCTURE_EXIT_MODE=exit`, `STRUCTURE_STOP_MODE=on`, `STRUCTURE_PREPARE_UPGRADE=1`,
  `STRUCTURE_MAX_AGE_DAYS=7` override the group 415 defaults.
- `grep -c 16283` over 24h of logs. A hit on an entry, target or super-order leg means the 0.01 tick band below Rs 250 is wrong there too.
- `/ops/db-latency` (Oracle writes up to 8 s seen in the exit loop).
- REAL watchlist reject histogram; `legacy_gate_backtest.py` with a candidates `limit` above 500 (500 was returned, so counts were truncated).
- `monday_check` during market hours on Mon 19 Oct (depth20 and session verdict are meaningless after the close).
- Re-check `STRUCTURE_MAX_AGE_DAYS=5` in December when the 2027 NSE holiday list is out (worst 2026 case is 4.65 days on Mon 28 Dec).
- Set `HF_MODEL` in `.env` to a model your token can use (the default Mistral-7B is rejected; no default is guessed from here).

## Known data issues
- 28 of 150 history fetches failed (delisted, new IPOs, odd tickers): survivorship bias in the A/B.
- The A/B baseline is a technical-only proxy, so a full-decision backtest is not possible.
- `ms_loc_score` is not monotonic against returns (rule off since group 410).

## Not verified anywhere
Frontend `npm run build`, Python 3.11/3.12 with pinned dependencies, real pytest runs of every suite (use `bash scripts/docker_test.sh`),
anything needing live Dhan.

## Minor
- The broker-stop SL-M-rejected flag is in memory: the first stop after each restart fails once and retries. Harmless.
- Engine caveats: `compat_board` was rebuilt, not recovered; golden tests freeze behaviour, they do not prove thresholds.

## Group 427: order-flow record (shipped, record only)
Let it collect for about two weeks of real trading days, then read `GET /records` (real-trade) `by_imbalance_5`, `by_imbalance_20`, `by_spread`, and position-stocks' positions `order_flow`. Only if a bucket is clearly worse on 100+ trades is a rule worth building (as a log-first gate, like group 425). Not recorded: cumulative volume delta / aggressor volume / absorption (no trade-by-trade feed). Backtest status (hist400, 359 symbols): entry_rules and legacy_gate ran on 18 Oct; their calendar / floor rows were distorted by outliers, so group 428 added robust verdicts - re-run both and read the `verdict (robust)` tables and Part 1b. `structure_backtest.py` was started in the background (`hist400/structure.log`, `hist400/structure_out.json`); its report is still to be read.

## Group 434
Compose default `STRUCTURE_POLICY_MODE` is now `record` (docs/GROUP434_POLICY_RECORD_DEFAULT.md). Also added: `GET /structure/rejection-stats` (structure / rr / chase counters). Items 1-3 below still need VM output (seed 13 `summary.txt`, hist400 reports, `structure.log` / `structure_out.json`); they were not in the group 433 zip.

## Open after group 428 (priority order)
1. Re-run `entry_rules_backtest.py` and `legacy_gate_backtest.py` on hist400 and paste both reports (robust tables + Part 1b decide which 18 Oct findings were real).
2. Read the `structure_backtest.py` report (`hist400/structure.log`, `hist400/structure_out.json`).
3. DONE in group 440: the robust 52-week rows say `hurts` in weak and strong markets, so `CANDIDATE_52W_LOG_ONLY` now defaults to 1 (compose). Still open: after about two weeks read `GET /range52w/status` (`recent`), compare those names' outcomes, then keep log-only, go hard-off, or roll back (`=0`). Check the VM `.env` with `scripts/env_structure_check.py` - a leftover `CANDIDATE_52W_RULE_ENABLED=0` means nothing is counted.
4. Collect about two weeks of order-flow records (group 427), then read `by_imbalance_5` / `by_imbalance_20` / `by_spread`; build a log-first gate only if a bucket is clearly worse on 100+ trades.
5. Confirm weekly expiry dates (Thursday before 2025-09-01, Tuesday from) against the NSE circulars; the calendar rows depend on it.
6. DONE in group 429: the Kronos wiring test is fixed in the zip. On the VM discard your local edit of that file before pulling (`git checkout -- services/real-trade-service/tests/test_group425_kronos_entry_wiring.py`).
7. Intraday rules (opening, VWAP, depth gates) cannot be backtested on daily bars; they need recorded intraday data.
8. Older open items: real NSE pre-open board, catalyst check, window auto-disable + width tuning, structure evidence gates G1-G4, swing-carry design question (group 421).

## Group 430 follow-ups
- Export fresh records with `python3 scripts/export_records.py` (the 18 Oct run saw only 1 trade since 1 October: stale files), then run `python3 scripts/records_report.py rt_*.json --since 2026-10-01`; compare with the September picture (63 trades, net -1,439, the 09:xx-10:xx entry cluster lost 1,598 net, gaps through stops). That decides whether any live rule change is warranted.
- Still open from group 428: the structure_backtest report (the entry_rules / legacy_gate robust tables were read in group 440; the 52-week gate moved to log-only). Also open: the group 438 doubled-quantity query result.

## Group 441: evidence tools and the legacy-board retirement (built, nothing switched on)
All of it needs data from the VM; none of it has run on real history.
1. P8 retest: `python3 scripts/p8_retest_evidence.py HIST_DIR` on the hist400 folders (seeds 7 / 11 / 13). Only `retest_ok` on more than one seed justifies `STRUCTURE_POLICY_PARAMS_JSON={"retest_entry":1}`, record-only first. Same for P7X: `ms_ab_report.py` now replays `p7x_indep` (`groups_strict_min`), but old stored summaries have no `ms_groups_strict`, so use freshly replayed history.
2. Intraday: after deploy, position-stocks records 1-minute bars to the `bar-records` volume (check `GET /board/status`, bar recorder block, after the first session). After >= 20 complete sessions run `scripts/export_intraday.py BAR_DIR` (look at `coverage.md` first) and `scripts/intraday_gate_backtest.py BAR_DIR`. `insufficient_sample` means wait, not "the gate is fine". Copy the volume off the VM before any `docker volume prune`.
3. ML V2: `python3 scripts/ml_v2_walkforward.py HIST_DIR`. `pass` is evidence that the V2 features rank entries out of sample, not a model to deploy; promotion needs the registry, the evidence gates and a live-model comparison.
4. Legacy board removal: `curl .../board/status > s.json; python3 scripts/board_parity_check.py --file s.json` must say GO first (>= 200 comparisons over >= 3 IST days, no fallbacks). Then `python3 scripts/retire_legacy_board.py --file s.json` (dry run), then `--apply`, run the position-stocks tests, redeploy, and only after a few clean sessions remove the A/B and the fallback in `screening/ms_engine_switch.py` by hand (the script deliberately does not touch it).

## Group 442: Real Money Calibration page (built; needs one deploy and a first look)
- Deploy: rebuild real-trade-service, position-stocks-service, calibration-runner-service, frontend; install nginx `/calibration/`; run `npm run build` (not run in the authoring sandbox). See `docs/GROUP442_REAL_MONEY_CALIBRATION_CONTROL.md`.
- Not done by design: restart from the page; switches for G5-G8 / F3 (they do not exist in the repo).
- Open: confirm on the VM that each LIVE switch really changes behaviour on the next cycle (the static proof covers the code, not a live trade); watch the first scheduled packs before leaving the timetable on.

## Group 470 follow-ups (20 Oct review)
- Confirm BROKER_STOP_ENABLED=true on the VM and test a resting stop on a small position.
- Done in group 471: whole-day stop-out guard (day pause + no same-day re-buy). Still not built: per-fill overshoot measurement in the exit loop (needs fill vs stop join; record exit slippage already exists from group 469); flow stamping on skipped candidates; live opening-range entry (needs 30+ shadow would-enter rows with results).
- Consider DEFAULT_RISK_PER_TRADE_PCT 5 -> 1-2 and a 25% position cap (about Rs4,500) with min 3 shares, only after stop fixes show positive expectancy.
- Run the full real-trade pytest on CI (sandbox could not run the entry integration suite this pass).

- Done in group 472: minimum 3 shares per REAL entry. Still open: 25% position cap (about Rs4,500, same-day only) after the stop fixes show positive expectancy; DEFAULT_RISK_PER_TRADE_PCT 5 -> 1-2 decision.
- Done in group 473: GET /review-fixes/status on real-trade (check it on the VM after deploy: broker_stop_enabled must be true).
- Note: DEFAULT_RISK_PER_TRADE_PCT only seeds a NEW db row; the live risk % is the stored row (set it on the Real Money page / risk config), and group322 already caps one trade at the daily-loss % (3). Not a code fix.
- Done in group 474: by_orb_shadow in the trade-records report. Read it after 30+ closed trades with a verdict: if 'orb_would_enter' beats 'orb_would_skip:*' after charges, consider a live opening-range entry; else drop it.
- Done in group 475: env_review_check.py. Run it on the VM before and after deploy. Flow stamping on skipped candidates: already covered for every gate after the quote-age check (order_flow.record runs before Gate 3); only never-queued watchlist rows have no flow, they have no decision row to hold it - left as is.
