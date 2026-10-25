# Audit status at E155 - what is complete, what remains

Against "Stockky v2 - Full Service, Function & UI Style Audit Plan" (25 Oct 2026). Written from what was run in the E149 sandbox, not from memory.
The sandbox has no package registry (no `fastapi`, `pytest`, `sqlalchemy`), so backend `pytest`, `tsc` and `vite build` did **not** run in E148 to E155; their last green result is E147.

## Complete

### Findings closed (code changed, tested)
| ID | Sev | Result |
| --- | --- | --- |
| F-05f | S3 | Training Lab: v6 summary strip (model, last training age, dataset, walk-forward win rate, live run) replaces the three plain stat cards; the rest of the page unchanged. New in E155. Completes the strips on the five discover screens. |
| F-05e | S3 | Data Feed: v6 summary strip (stocks in feed, last success with age and stale warning, job, progress, live quote feed) replaces the old four hand-made tiles; buttons, progress bar and alerts below unchanged. New in E154. |
| F-05d | S3 | IPO Tracker: v6 summary strip (tracked, upcoming, average listing gain, buy signals, awaiting data) under the lifecycle overview; list below still v3-v5. New in E153. |
| F-05c | S3 | Surprise Momentum: v6 summary strip (hits, top score, top RVOL, scan quotes / universe with a no-live-quotes warning) above the table; table below still v3-v5. New in E152. |
| F-05b | S3 | Hot Picks: v6 summary strip (picks, top score, source / freshness, universe) under the hero; cards below still v3-v5. New in E151, first screen of the restyle order. |
| F-20 | S2 | Seven more open routes now need the admin login: the four `notify-top-picks` Telegram senders, `/ipo/purge-non-equity` (+ `/surprise/ipo/` twin) and `DELETE /scan/universe/cache`. Buttons show a log-in hint on 401. New in E150. |
| F-18 | S3 | One label for the stop price: "Stop loss" on 8 more screens (Target unchanged); `labels.test.mjs` guards it. New in E150. |
| F-19 | S2 | Gateway route-auth matrix (109 mutating routes). Three open routes with no caller anywhere in the repo now need the admin login: `POST /ops/qstash/publish` (relayed to any URL with your QStash token), `POST /api/feed/purge-over-cap` and `/data-feed/purge-over-cap` (deleted feed rows). New in E149. |
| F-12 | S2 | `/training/*` proxy: train, train-run, models-promote, lock clear, admin and debug-abort now need the admin login (reads and UI routes stay open). New. |
| F-11 | S2 | "0 treated as missing" fixed in surprise 52-week distance, instant-scanner sentiment and decision technical score. |
| F-13 | S2 | NaN / inf in an uploaded feed row become "missing" instead of being stored. |
| F-05 | S3 | UI v7 hero on 12 of 13 tabs (all except the Dashboard) plus Settings. The hero has one visually hidden `h1`. |
| F-09 | S3 | Saved "Animations: Off" is applied at start-up on every tab. |
| F-14 | S3 | `topPicks.ts` / `TopPicks.tsx` case clash removed. |
| F-16 | S3 | Stop-out guard (day pause / symbol block): still fail-open, now logs one warning when its setting is broken. |
| F-17 | S4 | Malformed `REGIME_RULES_TABLE_JSON`: still falls back to the default table, now logs one warning. |
| F-15 | S3 | 8 test files that were not in `npm test` are now in it (34 suites). |

### Audit work finished
- **Function baseline (plan step 1):** 7,582 non-test functions measured by syntax tree: 201 over 100 lines, 313 with branch count over 25, 3,615 `except Exception`, 0 bare `except:`, 0 mutable defaults. Reproduce with `python3 scripts/audit_functions.py`.
- **Zero-as-missing sweep:** 38 candidates listed (`--falsy`); 3 were real bugs (fixed); the one with a non-zero default left (`swing/live.py:255`, `stop_pct or 3.0`) is safe.
- **Silent-exception review of order / capital / exit / reconcile code:** all 45 sites classified (25 rollback guards, 4 alert sends, 16 status / record-only / parse). No S1. The two fail-open guards without a log line (F-16) now log a warning.
- **Gateway route-auth matrix (service sheet check 6, api-gateway) - E149:** `python3 scripts/audit_route_auth.py` lists all 109 mutating routes: 27 need the admin login (17 before + 3 in E149 + 7 in E150), 82 do not. Each open route is triaged in `docs/ROUTE_AUTH_TRIAGE.json` (ui-open 55, uncalled 17, machine 9, machine-signed 1) with what it can do, and `--check` fails on a new untriaged open route. None of the open routes places or cancels a broker order. Details and decisions: `docs/ROUTE_AUTH_MATRIX_E149.md`.
- **UI coverage matrix (plan UI step 2-3):** `python3 scripts/audit_ui_coverage.py` - header and v6/v7 vs v3-v5 class counts for all 13 tabs plus system-check, service-manager and login.
- **Route match for the five discover / system screens (correctness check 5):** IPO Tracker, Hot Picks, Data Feed, Surprise Momentum, Training Lab - every call has a backend route (`scripts/audit_tabs.py`). The earlier four tabs: unchanged (0 missing, except 6 prefix-built calibration paths that must be confirmed by hand, and the word "/Surprise" in a comment).
- **F-07:** safe today (Real Trade positions have no side column); stays a note.
- **Backend suites (from E147, not re-run):** real-trade 5,373, position-stocks 4,315, api-gateway 9,044, market-data 1,733, analysis-intelligence 2,643, calibration-runner 258, notification-scheduler 214, decision-prediction 152 + 62 + 173, timesfm 32, kronos 11, scripts 523.
- **Static repo checks (re-run in E148, all ok):** compile of every service, secrets, merge conflicts, requirements drift, compose gateway URL, compose loopback ports, table-name collisions, shared copies (148 known differences, no new), market-structure package, holiday lists (note: 2027 dates not yet published), price-source imports, market-data URL default, control catalog.
- **Frontend (re-run in E148):** 34 suites pass; esbuild bundles `main.tsx` with no error.

## Partly done
| Item | Done | Not done |
| --- | --- | --- |
| UI v7 on all 13 tabs (decision in the plan) | Header / hero on 12 of 13 | Dashboard has its own `dash-hero`, not the shared one; the **cards inside** every tab except Auto Trade, Position Stocks and Calibration are still v3-v5 (coverage script: Training 11, Data Feed 8 before E154, Surprise 7, Dashboard 7, Settings 6 legacy card classes, others 1-3, v6/v7 pieces: 0). |
| F-01 duplicated shared files | Evidence collected: `kv_cache.py` has 6 copies in 3 versions (4 identical; market-data = same + 11 lines; api-gateway differs most, about 74 lines removed and 360 added, mostly the durable store path). `circuit_breaker.py` 5 files of 102-403 lines and `rate_limiter.py` 4 files of 312-740 lines are different implementations, not copies. | Deciding which kv_cache copy is right, whether the breakers / limiters should be one module. Plan says report and rank only, no merging in this pass. |
| Correctness audit of the 5 screens | Route match; read `pick_ranking.py`, `normalize_feed_payload`, the IPO scanner's function list and `analyze_ipo` start; found F-11 and F-13 on the way | Source match (10-20 values per screen against the raw response), freshness / stale labels, rule re-computation (rank, surprise measure, IPO gain), empty and error states, a logic test per screen, live compare on the VM. |
| Tier A / B function review | Automated pass on all; the 45 silent handlers read | Line-by-line review of the 38 long or complex order / capital / stop functions: `attempt_entry` (701 lines), `_send_real_sell` (801), `evaluate_mode` entry (1,318) and exit (557), `evaluate_manual_order`, `run_exit_reconciliation`, `holdings_sync_reconcile`, `reconcile_real_orders`, `run_eod_squareoff`, `_run_cycle`. |

## Remaining

### Needs your VM (cannot run in the sandbox)
1. `./stockky_full_test.sh` - all backend suites on Python 3.14, including `services/api-gateway/tests/test_admin_guard.py` (E149: checks the three new guards on the live app) and the new tests `test_e148_falsy_zero.py`, `test_e148_tech_score_zero.py`, `test_e148_feed_nonfinite.py`, `test_e148_training_proxy_guard.py`, and in real-trade-service `tests/test_e148_silent_guards_warn.py`.
2. `cd frontend && npm ci && npx tsc --noEmit && npm test && npm run build`.
3. Look at the new hero on the 8 re-headed tabs (Alerts, Training Lab, Paper Trades, Data Feed, Hot Picks, Surprise, IPO, Watchlist) and Settings, dark and light, 390 px and desktop.
4. Training Lab: while logged out, Train must answer "Missing admin session token"; after logging in on Auto Trade it must start a run; the progress poll must work in both cases.
5. E146 trailed-stop rail on a real Position Stocks and Auto Trade position (red stop tick right of the entry tick).
6. Live compare per screen (market hours): open it, compare five values with your broker or exchange page.
7. Tools not installed here: `ruff`, `vulture`, `radon`, `bandit`, `mypy` / `pyright`, `pytest --cov`, `jscpd` (plan phase 1).

### Audit work not started
- One-page service sheets (plan method, 10 checks): 0 of 10 filled. Order: real-trade, position-stocks, api-gateway, decision-prediction, market-data, notification-scheduler, calibration-runner, analysis-intelligence, kronos, timesfm, shared, scripts.
- Review of the 148 known differences in `scripts/shared_copies_baseline.json` (F-02).
- Review of the other 3,570 `except Exception` handlers (F-03); the 563 silent ones outside order paths are listed by `audit_functions.py --silent`.
- Split recommendations for the very large files (`decision/main.py`, `training/app.py`, `kv_cache.py`) and the long functions (F-04).
- Gateway route-auth: matrix done (E149) and decided (E150, see `docs/ROUTE_AUTH_MATRIX_E149.md`). Only a separate service key for the machine callers (`/catalysts/alert`, `/ops/check-alert`, wake) is left; it needs a secret on the gateway and in the GitHub workflow.

### UI work not started
- Move the cards inside the other 10 tabs to the v6/v7 pieces (KPI strip, AI strip, sub-tabs, ...). Order in the plan: Hot Picks, Surprise, IPO Tracker, Data Feed, Training (all five strips done; card bodies next), then Dashboard, Paper Trades, Watchlist, Alerts, Settings, then login / admin / system-check / service-manager.
- Dashboard: shared hero or keep `dash-hero` (decide), and an "Animations" switch reachable from it.
- F-08: nine components draw stop / target outside the true-scale rail (DecisionCard, ConvictionCard, ShortShadowCard, SignalStream, Trades, ManualTradeTicket, ScanPanel, SurpriseStocks, BuySniperModal): decide per component.
- Screenshots at 390 / 768 / 1440 px, dark and light, TV mode, reduced motion (needs a built app and sample data).
- Accessibility: contrast of text on the new fills in both themes, keyboard focus on the sub-tabs and switches, a words-equivalent for each drawing.
- A code check that new components never animate money numbers (today only the CSS file is guarded).

### Open findings (not fixed)
| ID | Sev | Finding | Fix size |
| --- | --- | --- | --- |
| F-21 | S4 | 17 open routes have no caller in the repo (alias twins of open routes, `/ops/refresh-static-params`); left open on purpose, see the matrix doc | S |
| F-01, F-02, F-03, F-04, F-08, F-10 | see plan | open |

### Decisions taken in E150 (you asked me to decide; each is reversible)
1. Training Lab buttons: **keep** the admin login on the buttons (F-12); the page stays readable when logged out, and a 401 now says how to log in.
2. Stop wording: **"Stop loss" and "Target"** (F-18).
3. Route login: Telegram senders, IPO row delete, universe-cache clear need the login; job start / stop routes and the 17 no-caller twins stay open (matrix doc has the reasons).
4. Shared-file merging (F-01, F-02) stays report-only, as the plan says.
5. Next step after the VM run list: card bodies tab by tab in the plan's order (Hot Picks first), then the service sheets for real-trade and position-stocks. Not started: it is a large visual change that should be seen in a browser after each tab.

### Not done in E150, and why
- Card restyle: Hot Picks (E151), Surprise (E152), IPO Tracker (E153), Data Feed (E154) and Training Lab (E155) have their summary strips; their cards / tables and the other 5 tabs do not (order: Dashboard, Paper Trades, Watchlist, Alerts, Settings); F-08 (nine stop / target drawings), screenshots, accessibility check: need a built app and a browser.
- Service sheets, tier A / B line-by-line review, F-03 / F-04: days of reading, no code change to make yet.
- A service key for the machine callers: needs a secret in the GitHub workflow.
- Every backend `pytest` suite, `tsc`, `vite build`: no registry in the sandbox (see the VM checklist).

## VM checklist (copy)
```
./stockky_full_test.sh
cd frontend && npm ci && npx tsc --noEmit && npm test && npm run build
python3 scripts/audit_tabs.py && python3 scripts/audit_ui_coverage.py && python3 scripts/audit_functions.py && python3 scripts/audit_route_auth.py --check
python3 scripts/check_shared_copies.py && python3 scripts/check_secrets.py
```
