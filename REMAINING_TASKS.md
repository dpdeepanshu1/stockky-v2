E197 (2026-10-26): real-trade invalid-IP early return now writes WAIT rows for unreached picks and closes the shortlist (audit fix, no money impact). See CHANGES_2026-10-26_E197_invalid_ip_early_return_bookkeeping.md
> E219 (includes E218): `_decide_impl` re-read; a live win rate of 0.0 was treated as missing in 3 places and is fixed (training `/training-score`, decision flags and `_decide`). E189/E190 had already read `_decide_impl`, so it is no longer on the unread list. Still unread line by line: the other long position-stocks functions named in E173. Your call: E218 reverses an E188 test (set QUOTE_BULK_ANGELONE_CLOSE_FALLBACK=1 to keep the old way). pytest not run here.
> E220 (includes E219): manual BUY now refused when the market is closed or after the EOD sweep; Swing lane '[object Object]' label fixed. Still unread in position-stocks: `_run_cycle` (main.py), `run_exit_reconciliation`, `short_live._attempt`. Shadow->live: short selling needs the Dhan SELL probe + 100 would-be shorts on 20 days; swing needs 30 closed trades on 20 days.
> E218 (includes E217): real-trade `entry evaluate_mode` (E217) and market-data `_get_quotes_bulk_core` (E218) read line by line. Still unread line by line: decision-prediction `_decide_impl` (decision/main.py 1321-1935) and the other long position-stocks functions named in E173. Your call: E218 reverses an E188 test (previous close as a live price when ltp is 0); set QUOTE_BULK_ANGELONE_CLOSE_FALLBACK=1 to keep the old way. E181/E183 need a column + migration; E180/E184 need real Dhan responses. pytest not run here.
> E214 (includes E213): Trades CSV loss-analysis columns (E213); target partial no longer sold on a stale tick (E214). Left for your call: E186 `publish_exposure` force=True; E181/E183 need a column + migration; E180/E184 need real Dhan responses. Cycle tests for E212/E214 not run here (no pytest).
> E196 (includes E195): CI's two test_eod_squareoff failures fixed (wall-clock / missing gate row, test bugs); position-stocks `attempt_entry` read, an exchange intraday-cutoff rejection now pauses new entries for the day. Still unread line by line: real-trade `entry evaluate_mode` (1,318 lines). Your call: structure gate shows no measured edge (G2 / G3 FAIL in the weekly pack).
> E195 (includes E194): `evaluate_manual_order` read; an unfilled manual LIMIT sell is no longer resent as MARKET after the exit window. Still unread line by line: real-trade `entry evaluate_mode` (1,318 lines); position-stocks `attempt_entry`. Your call (not changed): manual BUY accepts a target at or below entry.
> E194 (includes E193): real-trade `exit evaluate_mode` read line by line; a blocked SELL (backoff / intraday cutoff) no longer writes a new FULL_EXIT / EMERGENCY_EXIT audit row every cycle. Still unread line by line: real-trade `entry evaluate_mode` (1,318 lines), `evaluate_manual_order`; position-stocks `attempt_entry`. Your call (not changed): time stop counts calendar days; negative-PE neutral 50 (E193).
> E174 (includes E173): CI failures explained (stale tracked files). In your repo run `python3 scripts/check_stale_files.py | grep '^git rm' | sh` once, then re-run CI. Remaining line-by-line reads unchanged (see E173).
> E173 (includes E172): holdings_sync_reconcile read and guarded. Still unread line by line: real-trade `entry evaluate_mode` (1,318 lines), `exit evaluate_mode` (557), `evaluate_manual_order`; position-stocks `attempt_entry` and three others; `_decide_impl`; `_get_quotes_bulk_core`. Your side: `git rm` the two stale frontend files (E171); F-29 decision; E172 question about PLACED orders missing from Dhan's list.
> E172 (includes E171): reconcile_real_orders read and fixed (2 bugs). Still unread line by line: real-trade `entry evaluate_mode` (1,318 lines), `exit evaluate_mode` (557), `holdings_sync_reconcile`, `evaluate_manual_order`; position-stocks `attempt_entry` and three others; `_decide_impl`; `_get_quotes_bulk_core`. Still to do on your side: `git rm` the two stale frontend files (E171). F-29 needs your decision. Open question noted in E172: PLACED orders that never appear in Dhan's list.
> E171 (includes E170): build import fixed; `git rm` the two stale frontend files (see CHANGES E171); oversell sync fixed in `_send_real_sell`. Still unread line by line: the other five long real-trade money functions, `attempt_entry` and three others in position-stocks, `_decide_impl`, `_get_quotes_bulk_core`. F-29 (open paper-ledger writes) still needs your decision. NOT run here: a browser, other services' suites.
# Remaining tasks

> E159 (this zip, includes E158): Service Manager strip and removal of fixed-text health tiles; login / admin gate / system-check need no strip. Summary strips are finished. Next: card bodies tab by tab (Hot Picks first; a screenshot from you after each), Dashboard hero decision, F-08 stop / target drawings. Sheet title still says Render / Supabase (your call). NOT run here: `tsc` (whole app), `vite build`, a browser, backend pytest.
> E158 (includes E157): Settings summary strip. Strips now done for every data tab (Dashboard and Watchlist keep their own v5 summaries). Next: login / admin gate / system-check / service-manager screens, then card bodies tab by tab (screenshot per tab). NOT run here: `tsc` (whole app), `vite build`, a browser, backend pytest.
> E157 (includes E156): Alerts summary strip; Watchlist needs none (v5 pulse). Strips: Hot Picks, Surprise, IPO, Data Feed, Training, Paper Trades, Alerts. Next: Settings, then login / admin / system-check / service-manager, then card bodies (screenshot per tab). NOT run here: `tsc` (whole app), `vite build`, a browser, backend pytest.
> E156 (includes E155): Paper Trades summary strip; Dashboard needs none (AI Insights deck). Strips: Hot Picks, Surprise, IPO, Data Feed, Training, Paper Trades. Next: Watchlist, Alerts, Settings, then card bodies (screenshot per tab). NOT run here: `tsc` (whole app), `vite build`, a browser, backend pytest.
> E155 (includes E154): Training Lab summary strip; strips now on all five discover screens; next: card bodies tab by tab (needs a browser screenshot after each), then Dashboard. NOT run here: `tsc` (whole app), `vite build`, a browser, backend pytest.
> E154 (includes E153): Data Feed summary strip; strips now on Hot Picks, Surprise, IPO Tracker, Data Feed; next in the plan's order: Training. NOT run here: `tsc` (whole app), `vite build`, a browser, backend pytest.
> E153 (includes E152): IPO Tracker summary strip; strips now on Hot Picks, Surprise, IPO Tracker; next in the plan's order: Data Feed, Training. NOT run here: `tsc`, `vite build`, a browser, backend pytest.

> E152 (includes E151): Surprise Momentum summary strip; Hot Picks (E151) and Surprise have strips, the next screen in the plan's order is IPO Tracker. NOT run here: `tsc`, `vite build`, a browser, backend pytest.

> E151 (includes E150): Hot Picks summary strip (picks, top score, live / stored + stale, universe); pick cards and the other 9 tabs still v3-v5. NOT run here: `tsc`, `vite build`, a browser, backend pytest - commands in docs/AUDIT_STATUS_E151.md.

> E150 (includes E149): Telegram "send top picks" buttons (IPO, Surprise, Hot Picks), IPO "Delete Non-Equity Rows" and the universe-cache clear now need the admin login (log in once on Auto Trade / Position Stocks / Calibration; buttons say so on a 401); one "Stop loss" label on 8 more screens. Decisions taken and what is not done: docs/AUDIT_STATUS_E150.md, docs/ROUTE_AUTH_MATRIX_E149.md. NOT run here: backend pytest (incl. services/api-gateway/tests/test_admin_guard.py), `tsc`, `vite build`.

> E149 (includes E148): gateway route-auth matrix (`python3 scripts/audit_route_auth.py`, triage in docs/ROUTE_AUTH_TRIAGE.json, decisions in docs/ROUTE_AUTH_MATRIX_E149.md): 109 mutating routes, 20 need the admin login, 89 do not; `/ops/qstash/publish` and the two `purge-over-cap` routes (no caller anywhere in the repo) now need the admin login (F-19). Open: guard the other 17 uncalled routes (F-20) after you check the VM crontab / Upstash schedules; decide which of the 59 UI-called routes need the login. Complete / remaining list: docs/AUDIT_STATUS_E149.md. NOT run here: `services/api-gateway/tests/test_admin_guard.py` (no fastapi / pytest in the sandbox).

> E148 (includes E147): audit round 2 - training-proxy login gap closed (F-12), three "0 treated as missing" scoring defaults fixed (F-11), NaN/inf no longer stored from feed uploads (F-13), v7 hero header on 12 of 13 tabs (F-05), saved "Animations: Off" applied at start-up (F-09), three new audit scripts (CHANGES_2026-10-25_E148_audit_fixes_v7_all_tabs.md; full complete / remaining list in docs/AUDIT_STATUS_E148.md). Checked here: 34 frontend suites, esbuild bundle, compile, stdlib repo checks. NOT run here: any backend pytest suite, `tsc`, `vite build`, a browser (no package registry in the sandbox) - commands are in the E148 note. Behaviour change: the Training Lab's Train / Clear-lock / Promote now need the admin login.

> E147 (this zip, includes E146): audit closed - every service suite passes in a sandbox (see CHANGES_2026-10-25_E147_audit_close_catalog_check_speed.md); `check_control_catalog.py` now takes ~1 min instead of 5+. The E146 rail was rendered in headless Chromium (dark + light, no errors). Not run: live-credential checks, a click-through of the real pages with live data.

> E146 (this zip, includes E145): the risk rail now also draws when the stop has been trailed to or above the entry (CHANGES_2026-10-25_E146_ui_v7_trailed_stop_rail.md). `tsc`, `vite build` and all 25 frontend suites pass here; not looked at in a browser.

> E144 (this zip, includes E143): the audit's "no screen" gaps are closed on the Real Money Calibration page - Swing lane, Reconciliation (hourly drift report + stuck pending fills) and Status probes, all read-only (CHANGES_2026-10-25_E144_audit_screens.md). `tsc`, `vite build` and all 23 frontend suites pass here. NOT run: the live-field checker against your services, a click-through in the browser. Still API-only on purpose: the write actions and the parameterised look-ups listed in the E144 note.

> E137 (this zip, includes E136): the swing scan no longer picks a stock whose own last bar is stale (CHANGES_2026-10-25_E137_swing_stale_bar_pick.md).

> E136 (previous zip, includes E135): S4 probe fixed - a failed open no longer blocks every later open, a failed BUY releases the symbol lock, a held share is reported (CHANGES_2026-10-25_E136_swing_probe_deadlock_and_lock_leak.md).

> E135 (previous zip, includes E134): `/swing/report?since=YYYY-MM-DD` reads the shadow verdict for one stock list only (CHANGES_2026-10-25_E135_swing_report_since.md).

> E134 (previous zip, includes E133): each swing scan now records which stock list it ranked (`scan_universe` on `/swing/status`); see CHANGES_2026-10-25_E134_swing_scan_universe_tag.md.

> E133 (previous zip, includes E132): swing lane now has a built-in default universe (118 names) so shadow mode records from the first evening; set `SWING_UNIVERSE_SYMBOLS` (runner job `swing_universe`) to rank the S2 replay's stocks (CHANGES_2026-10-25_E133_swing_default_universe.md). NOT run: SqlRepo / live.py / probe / `/swing/*` routes / auto_pilot and exit-engine edits under pytest (no SQLAlchemy / FastAPI / PyPI in the sandbox) - run the real-trade suite on the VM. Still needed from you: plan S0 answers, S4 probe, weeks of shadow data.

> E88 (this zip, includes E87): gateway router split finished - E87 moved the 5 test-pinned handlers to `gateway_support/bound_routes_l.py` (docs/E87_GATEWAY_ROUTER_BATCH_L.md) and E88 moved the 3 framework hooks (CORS stamp middleware, exception handler, `/ws`) to `bound_routes_m.py` (docs/E88_GATEWAY_ROUTER_BATCH_M.md). `main.py` declares no route or hook any more. Docker `pip install ./shared` is deliberately not done: Render builds each service from its own folder (see the E88 doc). NOT run: gateway suites that import main.py (no pytest / fastapi in the sandbox) - run `cd services/api-gateway && bash run_tests.sh` on the VM and compare `/openapi.json`; also check the `/ws` handshake and the CORS headers on a normal response and a 500.

> E86 (this zip, includes E83-E85): gateway router split batch K - the 5 handlers the earlier batches had to leave (`market_trending`, `/surprise/premarket`, `start_bulk_feed`, `data_feed_refill_additional`, `data_feed_run`) moved to `gateway_support/bound_routes_k.py` (docs/E86_GATEWAY_ROUTER_BATCH_K.md); every pin was resolved, no new yfinance allowlist entry. 8 remain in `main.py`: 5 handlers whose tests read their source (`stock_chart_context`, `get_stock_decision`, `data_feed_structure_refresh`, `data_feed_structure_refresh_status`, `data_feed_symbol`) and 3 framework hooks (middleware, exception handler, websocket). NOT run: gateway suites (no pytest / fastapi) - run `cd services/api-gateway && bash run_tests.sh` on the VM and compare `/openapi.json`. Still open: Docker `pip install ./shared`.

> E85 (previous zip, includes E83-E84): gateway router split batch J - 12 handlers with nested functions (17 route registrations) moved to `gateway_support/bound_routes_j.py` (docs/E85_GATEWAY_ROUTER_BATCH_J.md) with a scope-aware rewrite; 13 remain in `main.py`: `market_trending` (nested yfinance call), `start_bulk_feed`, `data_feed_refill_additional`, `data_feed_run` (each pinned by a test or the yfinance guard), `api_surprise_premarket_proxy` (`global`), the 5 test-pinned handlers, and the middleware / exception handler / websocket. AST-equal bodies, symtable proof, route order identical, before/after sweep unchanged. NOT run: gateway suites (no pytest / fastapi) - run `cd services/api-gateway && bash run_tests.sh` on the VM, compare `/openapi.json`, and check `/notifications/send-picks` still needs the admin key. Still open: Docker `pip install ./shared`.

> E84 (previous zip, includes E83): `proxy_quote` (`GET /quote/{symbol}`) moved to its own `gateway_support/bound_routes_i.py` (docs/E84_GATEWAY_ROUTER_BATCH_I.md); body unchanged (market-data / Dhan -> AngelOne first, direct yfinance only as the flagged last fallback); only that one file was added to the yfinance guard allowlist. 25 handlers remain in `main.py`. AST-equal, route order identical, new `test_gateway_support_split_i.py`. NOT run: gateway suites (no pytest / fastapi) - run `cd services/api-gateway && bash run_tests.sh` on the VM and compare `/openapi.json`. Held back: 5 test-pinned handlers, middleware / exception handler / websocket, nested-function handlers (need hand moves). Still open: Docker `pip install ./shared`.

> E83 (previous zip): gateway router split batch H - 11 handlers (12 route registrations) moved to `gateway_support/bound_routes_h.py` (docs/E83_GATEWAY_ROUTER_BATCH_H.md); 26 remain in `main.py`. Route order (163), AST-equal bodies, static name proof and a before/after sweep of ~520 runnable tests checked here. NOT run: the gateway suites (no pytest / fastapi) - run `cd services/api-gateway && bash run_tests.sh` on the VM and compare `/openapi.json`. Held back: `proxy_quote` (direct yfinance use; `test_yfinance_callers_guard.py` allowlist decision needed), the 5 test-pinned handlers, middleware / exception handler / websocket, ~17 nested-function handlers. Still open: Docker `pip install ./shared`.

> E81 (previous zip): gateway router split batch G - the 11 admin-guarded handlers (14 route registrations) moved to `gateway_support/bound_routes_g.py` (docs/E81_GATEWAY_ROUTER_BATCH_G.md); 37 remain in `main.py` (E80's "49" was one too high; same count gives 48). Route order (163), AST-equal bodies, static name proof and a stub-fastapi build run checked here; `test_admin_guard.py` sweep updated to read the factories. NOT run: the gateway suites (no pytest / fastapi) - run `cd services/api-gateway && bash run_tests.sh` on the VM, compare `/openapi.json`, and check that the 14 routes still answer 401 without the admin token (GET `/ops/circuit-reset` stays open). Held back: `send_picks_to_telegram` (nested function), `data_feed_structure_refresh_status` and `data_feed_symbol` (pinned by `test_group360_structure_refresh.py`). Still open: nested-function and pydantic-model handlers, Docker `pip install ./shared`; the group387 scan now covers `gateway_support/bound_routes_*.py` (E82).

> E80 (previous zip): gateway router split batch F - 11 handlers (16 route registrations) moved to `gateway_support/bound_routes_f.py` (docs/E80_GATEWAY_ROUTER_BATCH_F.md); 49 remain in `main.py`. Route table (163), AST-equal bodies and a static name-completeness proof checked here. NOT run: the gateway suites (no pytest / fastapi) - run `cd services/api-gateway && bash run_tests.sh` on the VM and compare `/openapi.json` before deploying. Held back: `data_feed_structure_refresh_status` and `data_feed_symbol` (pinned by `test_group360_structure_refresh.py`). Still open: the rest of the split (Depends routes, nested functions, pydantic-model parameters), Docker `pip install ./shared`.

> E79: gateway router split batch E - 16 handlers (18 route registrations: watchlist, `/scan/last`, `/scan/status/{task_id}`, market top-gainers / losers / most-active, `/data-feed/meta`, `/stockky-hot/{status,result,premarket/status,stop}`, `/events/{symbol}`, `/searched`, `/wake-all`, `/market/momentum-movers`) moved to `gateway_support/bound_routes_e.py` (docs/E79_GATEWAY_ROUTER_BATCH_E.md); 60 remain in `main.py`. Full route table (163 registrations), bodies and 3,648 side-by-side scenarios checked here. NOT run: the gateway suites (no pytest / fastapi) - run `cd services/api-gateway && bash run_tests.sh` on the VM and compare `/openapi.json` before deploying. Still open: the rest of the split (Depends routes, nested functions, pydantic-model parameters), Docker `pip install ./shared`.

> E78: gateway router split batch D - 11 handlers (22 routes) with `Request` / `Query` / `BackgroundTasks` in the signature moved to `gateway_support/bound_routes_d.py` (docs/E78_GATEWAY_ROUTER_BATCH_D.md); 76 remain in `main.py`. Full route table (163 registrations), bodies and 1,280 side-by-side scenarios checked here. NOT run: the gateway suites (no pytest / fastapi) - this is the first batch that changes signatures, so run `cd services/api-gateway && bash run_tests.sh` on the VM and compare `/openapi.json` before deploying. Still open: the rest of the split, Docker `pip install ./shared`.

> E77: gateway router split batch C - 11 handlers (20 routes) moved to `gateway_support/bound_routes_c.py` (docs/E77_GATEWAY_ROUTER_BATCH_C.md); 87 remain in `main.py`. Route table (139 registrations), bodies and 1,550 side-by-side scenarios checked here; `test_group349_ms_passthrough.py` updated for the moved structure route. NOT run: the gateway suites (no pytest / fastapi) - run `cd services/api-gateway && bash run_tests.sh` on the VM before deploying. Still open: the rest of the split, Docker `pip install ./shared`.

> E76: gateway router split batch B - 11 handlers moved to `gateway_support/bound_routes_b.py` (docs/E76_GATEWAY_ROUTER_BATCH_B.md); 98 remain in `main.py` (16 more qualify for the same automatic move). Route table, bodies and 1,056 side-by-side scenarios checked here; NOT run: the gateway suites (no pytest / fastapi) - run `cd services/api-gateway && bash run_tests.sh` on the VM before deploying. Still open: the rest of the split, Docker `pip install ./shared`.

> E75 (this zip): Real Trade now has reason + feeling trade notes like Position Stocks (docs/E75_REAL_TRADE_JOURNAL_NOTES.md; table `trade_journal_notes`, switch `JOURNAL_NOTES_ENABLED`, card on Real Auto Trade -> Activity). Code-side audit of Real Trade and Position Stocks found nothing else open besides what E74 lists (router split and Docker `./shared` need the VM; C21, regime rules and `STRUCTURE_EXIT_MODE` wait for evidence). Not run here: the new routes under pytest, `tsc` / `vite build`.

> E74 (this zip): the E73 open items are decided (docs/E74_OPEN_ITEM_DECISIONS.md). Real Trade already enforces R:R 2.0 (`ENTRY_MIN_REWARD_RISK`), `STRUCTURE_MIN_RR` stays 0; regime rules, `STRUCTURE_EXIT_MODE` and C21 stay log-only. The 22 requirements files are not merged; a cross-service pin guard (`check_requirements_drift.py` + `requirements_cross_baseline.json`) is in CI instead. Still open, needs the VM: gateway router split (109 handlers; run `bash run_tests.sh` after each batch) and Docker `pip install ./shared` (needs repo-root build contexts and a docker build). E73's not-verified list is unchanged.

> E73 (this zip): (A) everything live - Real Trade structure gate + breakout volume and the Position Stocks breakout volume now ship `enforce` (docs/E73_LIVE_GATES_AND_ML_WIRING.md); Real Trade still runs `log` until `STRUCTURE_ENFORCE_MIN_OOS` (15) outcomes exist (B15). Not switched: regime rules (no enforce mode exists) and `STRUCTURE_EXIT_MODE` (log). (B) D22-D24 are wired into `train.py --legacy-ohlcv` (`ml_extras` block) and the F&O downloader exists (`training/fo_download.py`; its two NSE URLs were not exercised from the sandbox - run it on the VM). Still open: a general Real Trade R:R floor (decision 2 - `STRUCTURE_MIN_RR` still 0), gateway router split (109 handlers), merging the 22 requirements files, Docker `pip install ./shared`, C21 live (needs VM evidence). Not verified here: the exchange downloads, real xgboost training, frontend `tsc` / `vite build`, and the suites that need sqlalchemy / httpx / websockets (run on the VM).

> E72 (this zip): the log-only path and breakout-volume check for Position Stocks are built (docs/E72_PS_STRUCTURE_LOG_MODE_BREAKOUT_VOLUME.md); Position Stocks still ships `enforce`, nothing live changed. Still waiting on your answer: (1) keep the Position Stocks gate on `enforce`, or ship `PS_STRUCTURE_GATE_MODE=log`, (2) a general R:R floor for Real Trade and its value. Still open: D22-D24 ML wiring + F&O ban / OI downloaders, gateway router split (109 handlers), merging the 22 requirements files, Docker `pip install ./shared`, C21 live (needs VM evidence). Not verified here: frontend `tsc` / `vite build` (E71), and the full real-trade / analysis-intelligence / position-stocks pytest suites (pytest and sqlalchemy / httpx are not installable in the sandbox) - run them on the VM.
> E71 (this zip): items 3-6 built (docs/E71_FIB_VOLUME_NET_REGIME.md): Fibonacci on the candle chart, breakout-volume check (log only), net-of-charges P&L on the paper Trades page, regime rules (log only). Still waiting on your answer: (1) keep the Position Stocks structure gate forced to `enforce` (unchanged here), (2) a general R:R floor for Real Trade and its value (`STRUCTURE_MIN_RR` still 0). Still open: D22-D24 ML wiring + F&O ban / OI downloaders, gateway router split (109 handlers), merging the 22 requirements files, Docker `pip install ./shared`, C21 live (needs VM evidence). Run `npm ci && npx tsc && npm test && npx vite build` on the VM (NOT run here: no npm registry access) plus the backend suites (pytest not available here).

> E70 (this zip): E31 step 14 - `App.tsx` is 354 lines: tab switch in `components/app/TabContent.tsx`, state / handlers in `hooks/useWatchlist.ts`, `useBackendStatus.ts`, `useScanRunner.ts` (docs/E70_APP_HOOKS_AND_TAB_SWITCH.md). E31 file splitting is done for the frontend. The unused `TabButton`, `BackendStatusDot`, `SettingsBanner` are deleted. Behaviour fix: "Fix in Settings" / "Check backend settings" now open the Settings page. Run `npm ci && npx tsc && npx vite build` on the VM (not run here); then dashboard search / Quick scan / Stop / Full scan / reload mid-scan, stop the gateway and use the banner buttons, open every tab once.

> E69 (this zip): E31 step 13 - the dashboard view moved to `components/app/DashboardView.tsx` (docs/E69_APP_DASHBOARD_VIEW.md); `App.tsx` is now 1,163 lines. Still open for E31: the state / effects / handlers of `App` (~990 lines: scan runner, watchlist, realtime, backend checks) and the tab switch (~100 lines). Still waiting on your answer: delete the unused `TabButton`, `BackendStatusDot`, `SettingsBanner` (E67)? Run `npm ci && npx tsc && npx vite build` on the VM (not run here); on the dashboard run a symbol search, a Quick scan (watch the progress card), Stop, and a Full scan.

> E68 (this zip): E31 step 12 - navigation and top bars moved to `components/app/AppNav.tsx` (docs/E68_APP_NAV_SPLIT.md); `App.tsx` is now 1,477 lines. Still open for E31: the rest of the `App` component (state / effects, bottom nav, dashboard view with the stock / scan results). Still waiting on your answer: delete the unused `TabButton`, `BackendStatusDot`, `SettingsBanner` (E67)? Run `npm ci && npx tsc && npx vite build` on the VM (not run here); click every sidebar item, open and close the mobile drawer (narrow window), click the backend status chip and the theme button.

> E67 (this zip): E31 step 11 - the helper components at the bottom of `App.tsx` moved to `components/app/` (docs/E67_APP_HELPERS_SPLIT.md); `App.tsx` is now 1,675 lines, almost all of it the `App` component itself. Still open for E31: cutting the `App` component (state, effects, nav, dashboard view). Decision for you: `TabButton`, `BackendStatusDot` and `SettingsBanner` are unused - delete them? Run `npm ci && npx tsc && npx vite build` on the VM (not run here); open Settings (power-off button is the only thing to NOT click), check both clocks tick.

> E66 (this zip): E31 step 10 - the Overview tab moved to `components/realAuto/tabs/OverviewTab.tsx` (docs/E66_REAL_AUTO_TRADE_OVERVIEW_TAB.md). `RealAutoTrade.tsx` is now 879 lines and every tab is its own component. Still open for E31: `App.tsx` (2,147 lines). Run `npm ci && npx tsc && npx vite build` on the VM (not run here); open Real Trade -> Overview and click through login, arm / disarm, Dhan connect, risk save, TPIN / eDIS, network check, close-all dialog.

> E65 (this zip): E31 step 9 - the Board tab moved to `components/realAuto/tabs/BoardTab.tsx` (docs/E65_REAL_AUTO_TRADE_BOARD_TAB.md). Still open for E31: the overview tab of `RealAutoTrade` (~394 lines, 1385 left in the file) and `App.tsx` (2,147 lines). Run `npm ci && npx tsc && npx vite build` on the VM (not run here); open Real Trade -> Board once and check the three panels and the hatched tiles.

> E64 (this zip): E31 step 8 - the Activity tab moved to `components/realAuto/tabs/LogTab.tsx` (docs/E64_REAL_AUTO_TRADE_LOG_TAB.md). Still open for E31: board and overview tabs of `RealAutoTrade` (1429 lines left in the file) and `App.tsx` (2,147 lines). Run `npm ci && npx tsc && npx vite build` on the VM (not run here).

> E63 (this zip): E31 step 7 - the Charges tab moved to `components/realAuto/tabs/ChargesTab.tsx` (docs/E63_REAL_AUTO_TRADE_CHARGES_TAB.md). Still open for E31: board, overview and log tabs of `RealAutoTrade` (1460 lines left in the file) and `App.tsx` (2,147 lines). Run `npm ci && npx tsc && npx vite build` on the VM (not run here); open Charges once and compare the totals with the previous build.

> E62 (this zip): E31 step 6 - the Pipeline tab moved to `components/realAuto/tabs/PipelineTab.tsx` (docs/E62_REAL_AUTO_TRADE_PIPELINE_TAB.md). Still open for E31: board, overview, charges and log tabs of `RealAutoTrade` (1769 lines left in the file) and `App.tsx` (2,147 lines). Run `npm ci && npx tsc && npx vite build` on the VM (not run here).

> E61 (this zip): E31 step 5 - the Live Dhan tab moved to `components/realAuto/tabs/LiveTab.tsx` (docs/E61_REAL_AUTO_TRADE_LIVE_TAB.md). Still open for E31: board, overview, pipeline, charges and log tabs of `RealAutoTrade` (2038 lines left in the file) and `App.tsx` (2,147 lines). Run `npm ci && npx tsc && npx vite build` on the VM (not run here).

> E60 (this zip): E31 step 4 - the Watchlist tab moved to `components/realAuto/tabs/WatchlistTab.tsx` (docs/E60_REAL_AUTO_TRADE_WATCHLIST_TAB.md). Still open for E31: board, overview, live, pipeline, charges and log tabs of `RealAutoTrade` (2,313 lines left in the file) and `App.tsx` (2,147 lines). Run `npm ci && npx tsc && npx vite build` on the VM (not run here).

> E59 (this zip): E31 step 3 - the Positions tab moved to `components/realAuto/tabs/PositionsTab.tsx` (docs/E59_REAL_AUTO_TRADE_POSITIONS_TAB.md). Still open for E31: board, overview, live, watchlist, pipeline, charges and log tabs of `RealAutoTrade` (2,459 lines left in the file) and `App.tsx` (2,147 lines). Run `npm ci && npx tsc && npx vite build` on the VM (not run here).

> E58 (this zip): E31 step 2 - the Orders tab of `RealAutoTrade.tsx` moved to `components/realAuto/tabs/OrdersTab.tsx` (docs/E58_REAL_AUTO_TRADE_ORDERS_TAB.md). Still open for E31: the other 8 tabs of `RealAutoTrade` (~2,500 lines left in the file) and `App.tsx` (2,147 lines). Gateway: `main.py` still has 106 route functions (151 route decorators; E55 doc said 109). On the VM run `npm ci && npx tsc && npx vite build` (not run here) plus the backend test suites.

> E57 (this zip): E31 step 1 - `RealAutoTrade.tsx` helpers and leaf panels moved to `components/realAuto/` (docs/E57_REAL_AUTO_TRADE_SPLIT.md). Still open for E31: the `RealAutoTrade` component by tab (~2,100 lines) and `App.tsx` by route. On the VM run `npm ci && npx tsc && npx vite build` (not run here) plus the backend test suites.

> E56 (this zip): five small switches added to the calibration page (docs/E56_CATALOG_OPERATIONS_SWITCHES.md). Correction to older notes: the Position Stocks entries/day cap (E39, 10) and the Real Trade general entry cutoff (`ENTRY_LAST_TIME_IST`, 14:00) already exist, so neither is open. Still open: pattern-alert switches (scheduler service) on the page, E26 badges, E28 server-side baseline, E31 route splitting, F33, C21 going live. Run `cd services/api-gateway && bash run_tests.sh` and the two services' tests on the VM - nothing here was run under pytest.

> E55 (this zip, includes E54): `gateway_support/` with `MainRef` (docs/E55_GATEWAY_SUPPORT.md) - 6 more routes moved; 109 handlers remain in `main.py`, 98 of them can move in batches now. NOT run here: the 110 existing gateway test files (no pytest/fastapi in the sandbox) - run `cd services/api-gateway && bash run_tests.sh` on the VM before deploying. E54: fix for `fetch_history` (re-run it, then `g1_check`). Still open: `SCHEDULER_CONTROL_KEY`, your VM items, C21 live (blocked on B-tools evidence), candle work (backend + frontend, every timeframe).

> E54 sniper split (merged into this zip): `find_actionable_buys` + `_sniper_attach_structure` + `_sniper_observe` now in `gateway_routers/sniper.py`; its `bind()` hooks live in `gateway_support/__init__.py` (docs/E54_GATEWAY_SUPPORT_AND_SNIPER.md). Run `cd services/api-gateway && bash run_tests.sh` on the VM - the merged tree was NOT run under pytest/fastapi here.

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
