# Audit status at E148 - what is complete, what remains

Against "Stockky v2 - Full Service, Function & UI Style Audit Plan" (25 Oct 2026). Written from what was run in the E148 sandbox, not from memory.
The sandbox has no package registry, so backend `pytest`, `tsc` and `vite build` did **not** run in E148; their last green result is E147.

## Complete

### Findings closed (code changed, tested)
| ID | Sev | Result |
| --- | --- | --- |
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
- **UI coverage matrix (plan UI step 2-3):** `python3 scripts/audit_ui_coverage.py` - header and v6/v7 vs v3-v5 class counts for all 13 tabs plus system-check, service-manager and login.
- **Route match for the five discover / system screens (correctness check 5):** IPO Tracker, Hot Picks, Data Feed, Surprise Momentum, Training Lab - every call has a backend route (`scripts/audit_tabs.py`). The earlier four tabs: unchanged (0 missing, except 6 prefix-built calibration paths that must be confirmed by hand, and the word "/Surprise" in a comment).
- **F-07:** safe today (Real Trade positions have no side column); stays a note.
- **Backend suites (from E147, not re-run):** real-trade 5,373, position-stocks 4,315, api-gateway 9,044, market-data 1,733, analysis-intelligence 2,643, calibration-runner 258, notification-scheduler 214, decision-prediction 152 + 62 + 173, timesfm 32, kronos 11, scripts 523.
- **Static repo checks (re-run in E148, all ok):** compile of every service, secrets, merge conflicts, requirements drift, compose gateway URL, compose loopback ports, table-name collisions, shared copies (148 known differences, no new), market-structure package, holiday lists (note: 2027 dates not yet published), price-source imports, market-data URL default, control catalog.
- **Frontend (re-run in E148):** 34 suites pass; esbuild bundles `main.tsx` with no error.

## Partly done
| Item | Done | Not done |
| --- | --- | --- |
| UI v7 on all 13 tabs (decision in the plan) | Header / hero on 12 of 13 | Dashboard has its own `dash-hero`, not the shared one; the **cards inside** every tab except Auto Trade, Position Stocks and Calibration are still v3-v5 (coverage script: Training 11, Data Feed 8, Surprise 7, Dashboard 7, Settings 6 legacy card classes, others 1-3, v6/v7 pieces: 0). |
| F-01 duplicated shared files | Evidence collected: `kv_cache.py` has 6 copies in 3 versions (4 identical; market-data = same + 11 lines; api-gateway differs most, about 74 lines removed and 360 added, mostly the durable store path). `circuit_breaker.py` 5 files of 102-403 lines and `rate_limiter.py` 4 files of 312-740 lines are different implementations, not copies. | Deciding which kv_cache copy is right, whether the breakers / limiters should be one module. Plan says report and rank only, no merging in this pass. |
| Correctness audit of the 5 screens | Route match; read `pick_ranking.py`, `normalize_feed_payload`, the IPO scanner's function list and `analyze_ipo` start; found F-11 and F-13 on the way | Source match (10-20 values per screen against the raw response), freshness / stale labels, rule re-computation (rank, surprise measure, IPO gain), empty and error states, a logic test per screen, live compare on the VM. |
| Tier A / B function review | Automated pass on all; the 45 silent handlers read | Line-by-line review of the 38 long or complex order / capital / stop functions: `attempt_entry` (701 lines), `_send_real_sell` (801), `evaluate_mode` entry (1,318) and exit (557), `evaluate_manual_order`, `run_exit_reconciliation`, `holdings_sync_reconcile`, `reconcile_real_orders`, `run_eod_squareoff`, `_run_cycle`. |

## Remaining

### Needs your VM (cannot run in the sandbox)
1. `./stockky_full_test.sh` - all backend suites on Python 3.14, including the new tests `test_e148_falsy_zero.py`, `test_e148_tech_score_zero.py`, `test_e148_feed_nonfinite.py`, `test_e148_training_proxy_guard.py`, and in real-trade-service `tests/test_e148_silent_guards_warn.py`.
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
- Gateway route-auth matrix: 85 mutating routes, 14 had the admin dependency in the decorator before E148 (the training proxy now checks inside the handler). The other 71 include feed repair / stop / start-bulk / refill, `/scan/stop-all`, `/scan/start`, `/stockky-hot/run`, `/stockky-hot/stop`; decide which need the login (the UI calls several without one, so adding it means a frontend token change like gatewayAuth).

### UI work not started
- Move the cards inside the other 10 tabs to the v6/v7 pieces (KPI strip, AI strip, sub-tabs, ...). Order in the plan: Hot Picks, Surprise, IPO Tracker, Data Feed, Training, then Dashboard, Paper Trades, Watchlist, Alerts, Settings, then login / admin / system-check / service-manager.
- Dashboard: shared hero or keep `dash-hero` (decide), and an "Animations" switch reachable from it.
- F-08: nine components draw stop / target outside the true-scale rail (DecisionCard, ConvictionCard, ShortShadowCard, SignalStream, Trades, ManualTradeTicket, ScanPanel, SurpriseStocks, BuySniperModal): decide per component.
- F-18: one label for stop and target ("Stop loss" x4, "Stop" x9, "Target" x8).
- Screenshots at 390 / 768 / 1440 px, dark and light, TV mode, reduced motion (needs a built app and sample data).
- Accessibility: contrast of text on the new fills in both themes, keyboard focus on the sub-tabs and switches, a words-equivalent for each drawing.
- A code check that new components never animate money numbers (today only the CSS file is guarded).

### Open findings (not fixed)
| ID | Sev | Finding | Fix size |
| --- | --- | --- | --- |
| F-18 | S3 | Stop / Target wording differs across screens | S |
| F-01, F-02, F-03, F-04, F-08, F-10 | see plan | open |

### Decisions needed from you
1. Training Lab buttons now need the admin login (F-12): keep, or put the Training Lab behind the admin gate as well so the buttons are not shown to a logged-out visitor.
2. Should the next step be (a) the VM run list above, (b) restyling the card bodies tab by tab, or (c) the service sheets for real-trade and position-stocks?

## VM checklist (copy)
```
./stockky_full_test.sh
cd frontend && npm ci && npx tsc --noEmit && npm test && npm run build
python3 scripts/audit_tabs.py && python3 scripts/audit_ui_coverage.py && python3 scripts/audit_functions.py
python3 scripts/check_shared_copies.py && python3 scripts/check_secrets.py
```
