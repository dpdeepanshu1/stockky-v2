# Changelog index

Per-session fix notes live in `archive/session-notes/` (one file per
session, dated). This file is just an index so they're discoverable
without 50+ files cluttering the repo root.

Most recent first — see each file for full detail:

- 2026-10-03 (group 34) — api-gateway `requirements.txt`: added `pyjwt==2.8.0` (same pin as
  real-trade-service / position-stocks-service). It was only in `requirements-test.txt`, so the
  Docker image had no PyJWT and `qstash_client.verify_signature` hit its ImportError path and
  accepted every `/ops/qstash/tick` callback unverified (warning log only). New drift guard
  `tests/test_requirements_pins.py` (3 tests: PyJWT in production requirements, exact pin, matches
  the other services). `requirements-test.txt` comment updated. The fail-open-on-ImportError
  behaviour in `qstash_client.py` is unchanged. NEEDS REBUILD: `docker compose build api-gateway`
  then restart; confirm with `docker compose exec api-gateway python -c "import jwt;print(jwt.__version__)"`.
  Tests: test_requirements_pins + test_qstash_client 31 passed (sandbox, httpx/pytest only; full
  `bash run_tests.sh` not run here, re-run on the VM).
- 2026-10-02 (group 33) — debt-to-equity scale fixed at the source. `market-data-service/main.py`
  `_normalize_de_ratio` gained `yahoo_percent=False`; the Yahoo `info["debtToEquity"]` call site
  now passes `True`, because Yahoo's field is ALWAYS a percent (30 = 0.3x), so non-financials are
  divided by 100 at any magnitude (before, a Yahoo "30" stayed 30 and was scored as 30x leverage
  and penalised). Financial-sector rule and the default (flag off) heuristic unchanged; the NSE
  `secInfo` fallback is untouched (its scale is unknown). The `analysis-intelligence-service` pin
  in `test_fundamental_main.py` is rewritten as an input contract (values <=50 are taken as
  already-normalised multiples; rescaling there would double-divide). IMPACT: stocks with Yahoo
  D/E under ~50% will now score as low leverage, the fundamental score rises for them, and
  `decision-prediction-service` features built from stored `debt_to_equity` change scale for new
  rows (models trained on old rows, and 24h-cached values, see the old scale until retrained /
  expired). The one-line call site is not covered by any test (no existing test exercises
  `_get_fundamentals_inner`); the helper has new unit tests. market-data suite 577 passed.
- 2026-10-02 (group 32) — removed the remaining hardcoded "Aug-2026: Nifty -7% 6m, FII net-short"
  claims that never tracked real data: `real-trade-service/candidate_engine/candidates.py`
  (`market_note` is now `""`), `api-gateway/surprise_scanner.py` (`market_note` is now
  "high buy_pct = strong signal" on hits and "thresholds raised for quality" on the scan summary),
  `decision-prediction-service/decision/main.py` (regime label for market_score < 38 is now
  "Correction (weak market regime)"). Keys/shapes unchanged; the frontend does not read
  `market_note`. Tests updated in `test_surprise_scanner.py` and `test_candidates_analysis.py`.
  Comments/docstrings mentioning Aug-2026 left alone. decision-prediction-service has no test
  suite (syntax-checked only). api-gateway `run_tests.sh` exit 0 / 100%; real-trade-service
  `pytest --cov=.` 2751 passed, 1 skipped, 100%.
- 2026-10-02 (group 31) — api-gateway `main.py` `/ops/check-alert`: added a per-process cool-down.
  The same problem set (same open-circuit set / error-rate condition) is notified at most once per
  `OPS_ALERT_COOLDOWN_SEC` (default 900; 0 disables; bad value -> 900). A new problem alerts
  immediately, recovery clears the state, and the cool-down is only recorded after a SUCCESSFUL
  delivery (a failed send retries next call). Suppressed calls return `alerted: false,
  suppressed: true, cooldown_remaining_sec`. State is in-memory, so a restart can repeat one alert.
  Pin in `test_main_ops_routes.py` rewritten + 7 new tests. `bash run_tests.sh` for api-gateway:
  exit 0, 100% coverage.
- 2026-10-02 (group 30) — api-gateway `ipo_scanner.py`: dropped the hardcoded "Market context
  (Aug-2026): Nifty -7% in 6m, FII net-short" sentence from every IPO `buy_suggestion.rationale`
  (it never tracked real data and was already stale); the rationale now ends with the bars in
  force: "Decision bars: BUY_NOW≥70, PREPARE≥58." Removed the matching stale comment in
  `_build_ipo_suggestion`. Pin in `test_ipo_scanner.py` rewritten. NOT changed: the same stale
  Aug-2026 market text still appears in `real-trade-service/candidate_engine/candidates.py`
  (`market_note`), `api-gateway/surprise_scanner.py` (`market_note`, 2 places),
  `decision-prediction-service/decision/main.py` (a label) and in docstrings. `bash run_tests.sh`
  for api-gateway: exit 0, 100% coverage.
- 2026-10-02 (group 29) — api-gateway `main.py` `/ops/qstash/tick`: removed the no-op "warm" list
  (it only appended the names `/health` and `/ops/keepalive`, nothing was called) and the unused
  `_get_http_client()` call; the response is now `{"ok": true, "source": "qstash"}` plus
  `keepalive` / `keepalive_error` when the body asks for a wake. Nothing in the repo reads
  `warm`. Pin in `test_main_ops_routes.py` rewritten. The alert cool-down pin is left as is (needs
  a de-dup state/TTL design). `bash run_tests.sh` for api-gateway: exit 0, 100% coverage.
- 2026-10-02 (group 28) — api-gateway `main.py` `/ops/qstash/tick`: a crash inside the QStash
  signature check now fails CLOSED (503 "QStash signature verification unavailable", logged with
  traceback, nothing runs) instead of being swallowed and running the tick unauthenticated. Missing
  signing keys / PyJWT in `qstash_client.verify_signature` still accept by design (unchanged).
  Pin in `test_main_ops_routes.py` rewritten; log test added. Two other NOT FIXED pins in that file
  (alert cool-down, "warm" list) left as is, they need a product decision. `bash run_tests.sh` for
  api-gateway: exit 0, 100% coverage.
- 2026-10-02 (group 27) — real-trade-service, tests only, no production code changed. Fixed the 26
  failing tests in the full suite: (1) `test_feed_remaining_coverage.py` `_run` used
  `asyncio.get_event_loop()`, which raises on Python 3.12+ after any earlier file calls
  `asyncio.run()`; it now uses its own loop per call (19 tests). (2) `test_afterhours_scan_orchestration.py`
  hardcoded `pubDate="2026-09-24"` while `run_afterhours_scan` drops news older than
  `AFTERHOURS_SCAN_MAX_NEWS_AGE_DAYS` (5) against the real clock; the default is now yesterday
  (7 tests; these failed even when the file was run alone). `pytest --cov=.` in a clean venv:
  2751 passed, 1 skipped, 100% coverage.
- 2026-10-02 (group 26) — real-trade-service `auth/dhan_credentials.py`: `refresh_if_totp_enabled`
  now also rejects a missing/empty `DHAN_PIN` before any HTTP call (was sent as `pin=""`, wasting a
  TOTP attempt on a request Dhan can only reject). Pin in `test_dhan_credentials.py` rewritten
  (PIN added to the missing-credential parametrize, plus an empty-PIN test; log message now names
  all three vars). `test_dhan_credentials.py`: 157 passed. (Group 26 note corrected in group 27:
  the 26 full-suite failures were 19 in `test_feed_remaining_coverage.py` + 7 in
  `test_afterhours_scan_orchestration.py`, not all in the feed file; both fixed in group 27.)
- 2026-10-02 (group 25) — analysis-intelligence-service `fundamental/peers.py`: `normalize_sector`
  is now idempotent (canonical names like "IT", "Finance", "Infra", "Capital Goods" map to
  themselves). `analyze()` feeds the normalised sector back into `peers_for()`, which returned no
  peers for those four sectors for any symbol outside the curated list. Pin in
  `test_fundamental_main.py` rewritten to assert peers are returned; idempotency tests added in
  `test_peers.py`. The other fundamental pin (debt-to-equity <= 50 not rescaled) left as is: the
  percent-vs-multiple heuristic is ambiguous, needs your call. `./run_tests.sh` in a clean venv:
  exit 0, 100% coverage.
- 2026-10-02 (group 24) — analysis-intelligence-service `news/main.py`: `_base_symbol` now
  upper-cases/strips before removing the `.NS`/`.BO` suffix (and only strips a trailing one), so
  `tcs.ns` resolves to `TCS` and finds its company-name hint instead of searching news for
  `TCS.NS`. Pin in `test_news_main.py` rewritten to assert the fix. `./run_tests.sh` in a clean
  venv: exit 0, 100% coverage.
- 2026-10-02 (group 23) — analysis-intelligence-service `fundamental/rate_limiter.py`:
  `_cfg` now parses `RL_<PROVIDER>_RPS` and `RL_<PROVIDER>_BURST` independently, so a typo in
  one no longer discards a valid value for the other (pin in `test_rate_limiter.py` rewritten to
  assert the fix). Also fixed `test_event_depth.py::test_earnings_days_out_propagated`, which
  built its date from a hardcoded `_NOW` while the code reads the real clock (failed on the
  group 22 zip too). `./run_tests.sh` in a clean venv: exit 0, 100% coverage.
- `SESSION126_ANALYSIS_INTEL_INDIANAPI_FALLBACK_COVERAGE_2026-09-29.md` —
  analysis-intelligence-service `fundamental/indianapi_fallback.py` 93% -> expected 100%: kv_cache
  import fallback, in-process pacing sleep, suggested_timeout failure, redis-client guard. Tests only.
  35/35 passed via a hand-rolled pytest stand-in with line tracing (all 8 target lines hit); NOT run
  under real pytest/coverage — re-run on the VM. Next: `event/event_depth.py` (97%).
- `SESSION125_ANALYSIS_INTEL_MAIN_SENTIMENT_COVERAGE_2026-09-29.md` —
  analysis-intelligence-service: root `main.py` (real source executed against a temp fake-sub-app
  tree so failure branches count for the real file) and `sentiment/main.py` (batch/individual
  fallbacks, adjustment failures, double-checked cache, `__main__` block). Tests only. Passed in
  an earlier sandbox (1741 passed, both files 100%); re-applied here and compile-checked only —
  re-run on the VM. Next: `fundamental/indianapi_fallback.py` (93%).
- `SESSION124_ANALYSIS_INTEL_RATE_LIMIT_REPORT_COVERAGE_2026-09-29.md` —
  analysis-intelligence-service `rate_limit_report.py`: 35 test functions appended to
  `tests/test_rate_limit_report.py` covering the `_kv_get`/`_kv_set` sys.path fallback,
  stats/event edge cases, gateway POST and status extraction. Tests only; not run
  in-sandbox (no pytest) — confirm with the coverage command in the note.
- `SESSION123_ANALYSIS_INTEL_SERVICE_MAIN_COVERAGE_2026-09-29.md` —
  analysis-intelligence-service root `main.py`: new `tests/test_service_main.py`
  (fake sub-app trees + one real-mount smoke test; temp trees under `__pycache__` so
  they stay out of coverage). Fixed a wrong `''`-on-sys.path assertion. Tests only; not
  re-run in-sandbox (no pytest) — run `./run_tests.sh` to confirm. Session-122 roadmap
  is now complete.
- `SESSION112_ROUND29_CONFIG_ADMIN_HASH_B64_COVERAGE_2026-09-25.md` —
  position-stocks-service `config.py` 471-475 closed: the
  ADMIN_PASSWORD_HASH_B64 decode block, ported from real-trade-service's
  identical block + its `importlib.reload` fixture pattern. Pure stdlib —
  all 4 tests actually run for real in-sandbox (no stub needed), not just
  hand-traced. This clears the entire round-24 source-file list; only
  tests/*.py files' own coverage gaps remain, flagged round 28, still out
  of scope pending an explicit go-ahead.
- `SESSION112_ROUND28_REMAINING_ONE_LINE_GAPS_2026-09-25.md` — position-
  stocks-service: closed all five remaining 1-2 line source gaps from the
  round-24 list — `eod_squareoff.py` 147-148 (notify_critical exception
  swallow), `adaptive.py` 113 (ATR sample<2 guard, forced via monkeypatched
  `ATR_LOOKBACK`), `entry.py` 109 (reentry guard's unresolvable `closed_at`
  fail-open), `reconcile.py` 232 (legacy-backfill blank orderId skip),
  `screening/engine.py` 342 (non-positive composite score, forced via
  monkeypatched `_WINDOW_CONVICTION_MULT`). Tests only. `config.py`
  471-475 still open (importlib.reload risk); several `tests/*.py` files'
  own coverage gaps flagged but not yet in scope.
- `SESSION112_ROUND27_EOD_SQUAREOFF_STAGNATION_EXIT_COVERAGE_2026-09-25.md` —
  position-stocks-service `orders/eod_squareoff.py` 1044-1045 closed:
  `run_stagnation_exit`'s `except Exception: continue` guarding a
  malformed `opened_at`, via monkeypatched `as_aware` (same isolation
  convention as the class's other skip-and-continue tests). Tests only.
  Still open from round-24: `eod_squareoff.py`'s other 2 lines (needs a
  fresh coverage run to confirm), and the four 1-line gaps in
  `adaptive.py` / `entry.py` / `reconcile.py` / `screening/engine.py`.
- `SESSION112_ROUND26_WS_CLIENT_LOOP_COVERAGE_CLOSEOUT_2026-09-25.md` —
  position-stocks-service `feed/ws_client.py` last 4 missing lines closed
  (510-511, 542, 549): `_ws_loop`'s heartbeat-send exception guard, the
  stale-tick buffer-prune eviction, and the `_last_quote` write (needed a
  full ≥347-byte depth frame, ported from `test_ws_client.py`'s builder).
  Tests only. Next per the round-24 coverage run: `orders/
  eod_squareoff.py` (4), or the four 1-line gaps in `adaptive.py` /
  `entry.py` / `reconcile.py` / `screening/engine.py`.
- `SESSION112_ROUND25_DHAN_CLIENT_COVERAGE_CLOSEOUT_2026-09-25.md` —
  position-stocks-service `execution/dhan_client.py` all 10 remaining
  missing lines closed (156-157, 161-163, 244, 246, 252-253, 960-961):
  `_get_sdk_client`'s two SDK-version ImportError branches (forced via
  `sys.modules["dhanhq"]` patching), the CSV-fallback loop's exchange/
  instrument filters + per-row exception guard, and
  `edis_verification_summary`'s non-dict-row branch. Tests only. Next per
  the round-24 coverage run: `feed/ws_client.py` (4), `orders/
  eod_squareoff.py` (4), or the four 1-line gaps in `adaptive.py` /
  `entry.py` / `reconcile.py` / `screening/engine.py`.
- `SESSION112_ROUND24_DB_ORACLE_DDL_COVERAGE_2026-09-25.md` —
  position-stocks-service `db.py` `_ensure_columns`'s Oracle-dialect DDL
  branch (lines 296-298), tests only. New test in `tests/test_db.py`
  monkeypatches `DATABASE_URL` to an Oracle DSN to exercise the
  `is_oracle` ALTER-TABLE string. Picked off a real `pytest --cov` run
  (2257 passed) that confirmed round 23's fix and superseded the earlier
  unverified table. Next per that run: `execution/dhan_client.py` (10),
  `feed/ws_client.py` (4), `orders/eod_squareoff.py` (4), or the four
  1-line gaps in `orders/adaptive.py` / `entry.py` / `reconcile.py` /
  `screening/engine.py`.
- `SESSION112_ROUND23_CONFIG_GETTERS_COVERAGE_2026-09-25.md` —
  position-stocks-service `config.py` `_get_float`/`_get_int` malformed-env-var
  fallback branches (lines 31-32, 38-39), tests only. New
  `tests/test_config_getters.py` (8 tests). Picked up in a fresh
  conversation with no memory of the session that produced the prior
  coverage table — not re-verified against a real coverage run (no
  pytest/coverage in this sandbox, no network). See the note at the top
  of the session file. Next: re-run coverage for real before continuing
  down that table, since its line numbers may be stale.
- `SESSION112_ROUND16_DHAN_CLIENT_COVERAGE_2026-09-25.md` —
  position-stocks-service `execution/dhan_client.py` (the only module
  allowed to hold a decrypted Dhan credential / call Dhan's API) 21% → 
  target ~95%+, tests only, no production change. New
  `tests/test_dhan_client.py`: pure tick-rounding/classifier logic tested
  directly; every SDK-facing function (`place_order`, `place_super_order`
  incl. the MARKET direct-HTTP bump/clamp path, `get_trade_history`
  pagination, `place_cnc_stop_loss_market`'s 4 raise conditions, eDIS,
  `convert_position`, etc.) tested against a `SimpleNamespace` fake SDK
  client. No `sqlalchemy`/`httpx`/`dhanhq` in this sandbox — pure-logic
  math/classifiers re-verified standalone (all passed); SDK-facing tests
  traced by hand against the source, not executed — re-run pytest to
  confirm. No new production bug found this round. Next:
  `feed/scrip_master.py` (22%).
- `SESSION112_ROUND15_DB_TEST_FAILURES_FIX_2026-09-25.md` — first real
  pytest run of round 14's new `tests/test_db.py` (position-stocks-service)
  found 3 failures, all in `TestEnsureColumns`. **One real fix:**
  `_ensure_columns` built its `inspect(engine)` once, outside the
  per-entry `try/except`, so a failure there crashed `init_tables()`
  instead of being logged and skipped like every other migration failure
  — moved inside the loop. Two test-file bugs also fixed: a no-op test
  that only pre-created 1 of 12 columns, and a "failed ALTER" test whose
  `ctx.__enter__` override was an instance attribute the `with` statement
  never actually looked up (rewritten with `@contextlib.contextmanager`).
  `sqlalchemy`/`pytest` still unavailable in this sandbox (no network) —
  fixes are statically verified (`py_compile`) only; re-run pytest to
  confirm. Next: `execution/dhan_client.py` (21%).
- `SESSION112_ROUND7_SHARED_EXPOSURE_COVERAGE_AND_ROLLBACK_GUARD_2026-09-25.md`
  — position-stocks-service `capital/shared_exposure.py` 36% → 100% (whole
  `capital/` package now 100%). **One real fix:** `publish_own_exposure`'s
  except-handler called `db.rollback()` unguarded, so a dead connection made
  it raise despite its documented "never raises" contract, out of
  `ledger.sync_from_broker` and `POST /ledger/sync`; now guarded like its
  siblings (red test first, then fix). New `tests/test_shared_exposure.py`
  (36 tests) incl. unstubbed ledger→publish wiring and a cross-service drift
  guard. Suite 1577 → 1613 passed, 87%. 22 mutations, 0 survivors. Same
  unguarded rollback flagged (not changed) in real-trade-service's copy.
  Next: `tz_utils.py` (71%).
- `SESSION112_ROUND6_SHARED_ORDER_BUDGET_COVERAGE_2026-09-25.md` —
  position-stocks-service `capital/shared_order_budget.py` (cross-service Dhan
  order-rate guard) 55% → 100%, tests only. New
  `tests/test_shared_order_budget.py` (38 tests) incl. a stale-identity-map
  regression proving the cap is enforced by the atomic UPDATE, exits never
  gated, fail-open, `status()`. Suite 1539 → 1577 passed. 29 mutations, 0
  survivors. Flags `_get_or_create_row` as dead code (unchanged). Next:
  `capital/shared_exposure.py` (36%).
- `SESSION112_ROUND5_SHARED_SYMBOL_LOCK_COVERAGE_2026-09-25.md` —
  position-stocks-service `capital/shared_symbol_lock.py` (cross-service
  same-symbol guard) 30% → 100%, tests only. New
  `tests/test_shared_symbol_lock.py` (54 tests) incl. a REAL unique-constraint
  IntegrityError race (lost / won-by-self) and fail-open on every error path.
  Suite 1485 → 1539 passed, 84% → 86%. 36 mutations, 0 survivors. Flags one
  cosmetic `cleanup_stale` startup-log inaccuracy (not changed). Next:
  `capital/shared_order_budget.py` (55%).
- `SESSION112_ROUND4_LEDGER_COVERAGE_2026-09-25.md` — position-stocks-service
  `capital/ledger.py` (the money engine) 53% → 100%, tests only, no production
  change. New `tests/test_ledger_coverage.py` (82 tests) incl. the REAL
  `sync_peer_pnl` (shared fixture stubs it), capital-erosion add-back,
  kill-switch trip boundary + gate mirror, `EXIT_LEGS_REJECTED` handling.
  Suite 1403 → 1485 passed, 82% → 84%. 41 mutations, 0 real survivors (2
  provably equivalent). Next: `capital/shared_symbol_lock.py` (30%).
- `SESSION111_NOTIFY_FIRE_AND_FORGET_EXIT_PATH_FIX_2026-09-25.md` — `notify_sync`
  could block its caller for a ~42s worst case (service timeout + direct-Telegram
  timeout + its own HTML-retry timeout), and `exit_engine/exit.py` called it 14
  times inline while holding the per-mode exit lock — one slow Telegram delivery
  for one position's exit stalled protective stop-loss checks for every other
  open position in the same tick, and skipped the next 5-10s tick outright.
  Fixed with a new `notifier.notify_fire_and_forget` (same dedup + delivery,
  off a daemon thread, no return value) and a one-line import-alias change in
  `exit_engine/exit.py` — no call sites touched. real-trade-service 2725 passed,
  0 xfailed, 100% (8320 stmts). 30 mutations, 0 real survivors.
  `position-stocks-service`'s equivalent call sites not ported this round
  (flagged as open).
- `SESSION110_PARTIAL_FILL_INCREMENT_PRICING_AND_STAGE_TIMINGS_2026-09-24.md` —
  the last xfail is gone. **`execution/reconcile.py`** booked every partial-fill
  increment at Dhan's *cumulative* average price (5 @ 100 then 5 @ 102 booked as
  100 + 101: position average, cash and SELL P&L all drifted); each increment is
  now booked at its own price, derived from the previous poll's cumulative value
  (new nullable `trade_orders.broker_fill_notional`, additive migration), with
  fallbacks to the old behaviour whenever paise-rounding noise or inconsistent
  broker data makes the derivation untrustworthy; also wired into the
  `expire_stale_orders` late-fill path. **`pipeline_status`/`cycle_runner`:** stage
  timings for the three concurrent stages were misattributed since session48b —
  now exact. real-trade-service 2716 passed, 0 xfailed, 100% (8304 stmts).
  30 mutations, 0 real survivors.
- `SESSION109_REAL_TRADE_TAIL_COVERAGE_AND_FILLEDQTY_FIX_2026-09-24.md` —
  **real-trade-service production code 100%** (8226 stmts, 0 missed; 2667
  passed). Two silent-failure bugs fixed. (1) `execution/reconcile.py` stamped a
  `TRADED` order `FILLED` with no position/cash booked when Dhan's `filledQty`
  was non-numeric (never re-polled, so the fill vanished from the books) — now
  left pending and retried. (2) `risk_engine/engine.py`: a BUY with a **NaN**
  stop / entry / qty / adj_risk_pct was **APPROVED at full size** (NaN compares
  False, so every cap was skipped) — now rejected `invalid_order`; SELLs
  unaffected. The old "unreachable" sizing guard became the directly-tested
  `_qty_within_risk_cap()`. Also: never-awaited coroutine in
  `feed._schedule_atr_refresh`, legacy `Query.get()` in `exit._load_profile`, a
  vacuous test rewritten, `event_depth_local`/`return_sanity` 100% with a
  keyword-drift guard vs analysis-intelligence-service, new `.coveragerc`
  (omits dev harness + `tests/`, so not comparable with the old 98%).
  19 mutations, 0 survivors.
- `SESSION108_POSITION_STOCKS_ORACLE_COMPAT_COVERAGE_2026-09-24.md` —
  `position-stocks-service/oracle_compat.py` 15%→100% (110 stmts, 30/30
  branches; still 100% with the `# pragma: no cover` lines counted). Finishes a
  half-done draft (88%, 5 failing). New `tests/test_oracle_compat.py` (104
  tests): exact-string SQL for both dialects, the Postgres upsert executed for
  real on SQLite, real `exec_ddl_safe` DDL, the `connect` call-timeout listener
  fired from the pool's dispatch, lazy real Oracle engine build when `oracledb`
  is installed. position-stocks 1233→1337 passed, 78%→80%. 53 mutations, 0
  survivors. No production code changed. Next: `pipeline_status.py`,
  `capital/shared_symbol_lock.py`, `capital/shared_exposure.py`.
- `SESSION99_SECRET_IN_URL_LOG_LEAK_FIX_ROUND2_2026-09-24.md` — session98's
  "still open" secret-in-URL leak, closed in the two services it named, plus
  one more found along the way. **`market-data-service/main.py`:** TwelveData,
  Polygon and AlphaVantage API keys were in query strings, logged in full at
  INFO via `httpx` (same mechanism as session98). **`analysis-intelligence-
  service/news/main.py`:** same for the NewsAPI key (this service's first
  tests). Both fixed with an `httpx`-logger redaction filter, same shape as
  session98. **Also found:** `position-stocks-service/feed/ws_client.py` puts
  the AngelOne feed token and API key in the WS URL; the `websockets` library
  logs the full request line at DEBUG via its own `"websockets.client"`
  logger — lower severity (this service's `LOG_LEVEL` defaults to INFO, so it
  doesn't leak today) but fixed the same way since `LOG_LEVEL=DEBUG` is a real
  supported knob. All three reproduced live (real httpx `MockTransport` /
  real local `websockets` server-client round-trip) both leaking pre-fix and
  clean post-fix; regression tests fail on the old files. market-data-service
  24 passed (14+10 new); analysis-intelligence-service 8 passed (new);
  position-stocks-service 1232 passed (1225+7 new).
- `SESSION98_NOTIFIER_COVERAGE_AND_SECRET_IN_URL_LOG_LEAK_FIX_2026-09-24.md` —
  `notifier.py` 23%(unstable)→100%; new `tests/test_notifier.py` (64 tests, real
  httpx via MockTransport). **Security fix in 4 files / 3 services:** httpx logs
  every request URL at INFO and services run `basicConfig(INFO)`, so the
  Telegram **bot token** was in the logs on every send — and in
  `notification-scheduler-service` (the platform's *primary* alert path) also the
  **Discord/Slack webhook URLs** and **CallMeBot apikey**; a revoked webhook even
  returned its URL in the `/notify` response (`HTTPStatusError` embeds the URL).
  Fixed with an `httpx`-logger redaction filter (+ scrubbed error strings) in
  `real-trade-service/notifier.py`, `position-stocks-service/notifier.py`,
  `notification-scheduler-service/notification/main.py` and
  `scheduler/governance_check.py`; regression tests fail on the old code. **Check
  your logs and rotate secrets — see the note.** Same class still open in
  `market-data-service` / `analysis-intelligence-service` (API keys in query
  strings). real-trade-service 2253 passed/1 skipped/1 xfailed, 96%;
  position-stocks 1225 passed; notification-scheduler 21 passed (first tests
  there). 52 mutations, 0 survivors.
- `SESSION97_DB_MIGRATIONS_COVERAGE_AND_DRIFT_GUARD_2026-09-24.md` —
  `db.py` 7%→100% (521/521). New `tests/test_db.py` (217 tests): every one of
  the 22 boot-time migrations executed on a real *legacy* SQLite schema (Oracle
  branch captured via recorded SQL and checked for parity/type/length/default
  against `models.py`); legacy rows read back with the same defaults as new
  rows. **Drift guard:** adding a column to an existing model without an
  `_ensure_*` migration now fails a test (the session-11 bug class). Optional
  `tests/test_db_postgres_live.py` (5 tests, skipped without `pgserver`) runs
  the Postgres SQL on a real PostgreSQL. **One production fix:**
  `_normalize_pg_url` left `&&` when `channel_binding` sat mid-query, which
  libpq rejects. VM-equivalent run: 2189 passed, 1 skipped, 1 xfailed; 94%→96%.
  60 mutations, 0 survivors. Note: `notifier.py` coverage (52→48→23%) is
  incidental, not a regression — it has no direct tests; next candidate.
- `SESSION96_DHAN_CREDENTIALS_COVERAGE_AND_PIN_LEAK_FIX_2026-09-24.md` —
  `auth/dhan_credentials.py` 18%→100% (215/215). New
  `tests/test_dhan_credentials.py` (156 tests, real SQLite + real Fernet + real
  pyotp). **Two production fixes:** (1) `refresh_if_totp_enabled()` logged AND
  Telegrammed `str(HTTPStatusError)`, which contains the full request URL —
  `...generateAccessToken?dhanClientId=..&pin=<PIN>&totp=..` — so any 4xx/5xx
  leaked the Dhan PIN; now redacted via `_redact_secrets()` before log/notify
  (check your log/Telegram history — see note). (2) a swallowed DB failure in
  that function left the caller's Session in `PendingRollbackError` for the
  next query in `cycle_runner`; now healed via `_heal_session()` (rolls back
  only a poisoned session). Regression tests fail on the pre-fix code (24
  failures); mutation-checked (38 regressions, 0 survivors). Full suite: 1972
  passed, 1 xfailed; overall 93%→94%.
- `SESSION95_LOCAL_CACHE_COVERAGE_2026-09-24.md` — `resilience/local_cache.py`
  45%→100% (62/62). New `tests/test_local_cache.py` (32 tests) against a real
  in-memory SQLite DB: the 2026-09-12 two-writer `IntegrityError` race
  reproduced for real (loser's write lands via the UPDATE fallback), the
  2026-09-16 empty-positions snapshot regression, the 2026-09-12
  PARTIALLY_CLOSED reconcile fix, exact `RECONCILE_MISMATCH` audit detail,
  and a `String(64)` key-length audit of every key the service writes
  (longest 42 — no bug). Mutation-checked (14 regressions, 0 survivors).
  Full suite: 1816 passed, 1 xfailed; overall 93%. No production code
  changed. Observations: `json.dumps` sits outside `save_snapshot`'s `try`;
  startup reconcile is false-positive-prone by design (snapshot precedes
  exits).
- `SESSION94_CYCLE_RUNNER_COVERAGE_2026-09-24.md` — `cycle_runner.py`
  7%→100% (122/122). New `tests/test_cycle_runner.py` (64 tests) covers the
  function every REAL/DEMO cycle funnels through: manual market-hours
  warning, REAL token pre-flight (TOTP gating, early auto-disarm with nothing
  downstream executed), the session48b concurrent
  dynamic_universe→watchlist ‖ candidates stage (proven with events, not
  just call order), exit-lock acquire/release incl. a real `threading.Lock`,
  position snapshot, and the real `pipeline_status` contract.
  Mutation-checked (16 deliberate regressions, all caught). Full
  `real-trade-service` suite: 1784 passed, 1 xfailed; overall 92%→93%.
  No production code changed. **Finding, not fixed:** `pipeline_status`'s
  single "current stage" slot is overwritten by the three concurrent stages,
  so per-stage `stage_timings_ms` are misattributed since session48b
  (observability only) — see the note for numbers and options.
- `SESSION93_INTRADAY_ELIGIBILITY_COVERAGE_2026-09-24.md` —
  `intraday_eligibility.py` first direct coverage (every other test
  monkeypatched its public functions away, so its cross-service
  `scalp_intraday_restricted` mirroring had never run under test).
- `SESSION92_AFTERHOURS_SCAN_FINAL_COVERAGE_GAPS_2026-09-24.md` —
  `watchlist_engine/afterhours_scan.py` final 3 gaps closed (now 100%);
  VM run confirmed session91's rounds 1-3 (1694 passed, 1 xfailed).
- `SESSION91_AFTERHOURS_FETCHERS_COVERAGE_ROUND2_2026-09-24.md` — round 2:
  new `tests/test_afterhours_scan_fetchers.py` (20 tests) covers
  `_fetch_rss_items`, `_fetch_bulk_deal_hits`, and `_validate_symbols` —
  the three network/circuit-breaker-dependent functions in
  `watchlist_engine/afterhours_scan.py` — using the same
  `httpx.AsyncClient` + breaker-`.call()` mocking pattern already
  established in `tests/test_watchlist_sources.py`. **Not run through live
  pytest — no network in this sandbox**; hand-traced against the real
  source. Deferred to round 3: `run_afterhours_scan` and
  `finalize_nextday_watchlist`, the two DB-writing orchestrators. No
  production code changed.
- `SESSION91_AFTERHOURS_PURE_HELPERS_COVERAGE_ROUND1_2026-09-24.md` — new
  `tests/test_afterhours_scan_pure_helpers.py` (31 tests) covers
  `watchlist_engine/afterhours_scan.py`'s five self-contained helpers
  (`_has_uncontextualized_negative`, `_score_headline`,
  `_parse_item_datetime`, `_is_within_max_age`, `_parse_feed_items`) —
  only `_extract_symbol` (the MANINDS/EKC/OLAELEC/RAYMONDREL/UTLSOLAR
  name-alias fix, already present coming into this round) had direct tests
  before. **Not run through live pytest — no network in this sandbox**;
  written and hand-traced against the real source instead, same caveat as
  sessions 76/77/82c/86. Deferred: the two DB-writing orchestrators
  (`run_afterhours_scan`, `finalize_nextday_watchlist`) and the three
  httpx-calling fetchers. No production code changed.
- `SESSION88_CANDIDATES_COVERAGE_100_PERCENT_2026-09-23.md` —
  100%-coverage plan: `candidate_engine/candidates.py` finished, 65%→100%
  (689/689 statements). New `tests/test_candidates_orchestration.py` (24
  tests, round 3) covers the three DB-writing cycle orchestrators
  session87 deferred (`_refresh_standard_candidates`,
  `_refresh_volume_shock_candidates`, `refresh_candidates`) plus the 6
  stray single-line gaps session87's note called out by number. No
  production-code bugs found — pure coverage-closing pass. Full
  `real-trade-service` suite: 1150 passed, 1 xfailed, no regressions;
  overall repo coverage 78%→80%. `position-stocks-service` re-confirmed
  unchanged (1221 passed).
- `SESSION87_CANDIDATES_COVERAGE_ROUNDS_1_2_2026-09-23.md` — 100%-coverage
  plan follow-up: `candidate_engine/candidates.py`, 0%→65%, both rounds
  actually run through live pytest+coverage (this sandbox had working
  network/pip access). Landed session86's drafted-but-unlanded round 1
  (`tests/test_candidates_helpers.py`, 107 tests — sector-peer-history
  cache, adaptive-param refresh, HTTP fetch wrappers, quality gate, pure
  analysis helpers, source row-normalizers, dedupe-cooldown lookup),
  fixing one real bug the run caught in session86's own draft (a test
  meant to exercise the sector-relative reject path was actually being
  rejected earlier, by the absolute floor, because the floors were never
  lowered from their ~35 default). Added round 2
  (`tests/test_candidates_analysis.py`, 26 tests, new) covering
  `_multi_tf_analysis` and `_volume_shock_analysis` via a routing fake
  `httpx.AsyncClient`; the run caught two more bugs, both in this
  session's own first-draft test fixtures/assertions, not the production
  code. Full suite: 1126 passed, 1 xfailed, no regressions. Deferred to a
  follow-up round: `_refresh_standard_candidates`, `_refresh_volume_shock_
  candidates`, `refresh_candidates` — the DB-writing cycle orchestrators.
- `SESSION86_AUTO_PILOT_ORCHESTRATION_COVERAGE_2026-09-23.md` — 100%-coverage
  plan follow-up: second (and final) coverage round on `execution/
  auto_pilot.py`, targeting the cycle-orchestration layer session85
  deliberately left out. New `tests/test_auto_pilot_orchestration.py` (120
  tests) covers the lock wrappers, `_exit_only_tick_body`, `_full_tick_body`,
  `_select_overnight_holds`'s net-of-costs branch, `_requeue_overnight_
  priority_candidates`, `_inject_nextday_watchlist_candidates`, `_prepick`,
  `_enter_at_open`, `_edis_morning_check`, `_eod_squareoff`, `_eod_signal_
  scan`, `_schedule_tick_body` and all five of its scheduled automations,
  all five background loops, the remaining `_afterhours_scan_body` branches,
  the after-hours lock + manual trigger, and `start()`. Moves `auto_pilot.py`
  31%→~99% (pending VM confirmation). **Not run through live pytest this
  session** (no network in this sandbox, unlike session85's) — verified
  structurally instead: py_compile plus an AST sweep confirming every
  referenced `ap.<name>`, every dotted monkeypatch target, and every model
  kwarg/attribute actually exists in the target module. Same caveat as
  sessions 76/77/82c — user should confirm with a real pytest+coverage run.
  No production code changed, tests only.
- `SESSION85_AUTO_PILOT_HELPERS_COVERAGE_2026-09-23.md` — 100%-coverage plan
  follow-up: first coverage round on `execution/auto_pilot.py`, the largest
  remaining gap (750 stmts). New `tests/test_auto_pilot_helpers.py` (56
  tests) covers the self-contained helpers — locks, reconcile-throttle,
  `_summarize`, overnight-hold/edis gate toggles, `_needs_cnc_sell`,
  gate-off alerting, the afterhours-window/market-date calcs, and the full
  `_select_overnight_holds` eligibility/ranking/cap pipeline. Moves
  `auto_pilot.py` 19%→31%. **Actually executed this session** (sandbox now
  has working pytest + network, unlike prior sessions): full suite run —
  869 passed, 1 xfailed, no regressions; confirms session84's two modules
  and the three previously-100% modules are still 100%. Cycle orchestration
  (`_full_tick_body`, `_prepick`, `_eod_squareoff`, background loops) left
  for a follow-up round; `candidate_engine/candidates.py` (0%, 2077 lines)
  still untouched. No production code changed, tests only.
- `SESSION84_SHARED_ORDER_BUDGET_AND_SYMBOL_LOCK_COVERAGE_2026-09-23.md` —
  100%-coverage plan follow-up: `execution/shared_order_budget.py` (44%→100%)
  and `execution/shared_symbol_lock.py` (41%→100%) closed with 2 new test
  files; confirmed via a real VM pytest run that `exit_engine/exit.py`,
  `portfolio/portfolio.py`, `execution/dhan_client.py` are genuinely 100%;
  flagged `execution/auto_pilot.py` (19%, 611 lines) as the next, largest
  gap and `candidate_engine/candidates.py` (0%, never tested) after that;
  no production code changed, tests only
- `SESSION83_CLAMP_FOR_ATR_IMPORTERROR_COVERAGE_2026-09-21.md` — 100%-coverage
  plan, Phase 1 #1 closed out: `_clamp_for_atr`'s `return_sanity` ImportError
  fallback (the last zero-coverage item session82c flagged) now has 2 direct
  tests; `exit_engine/exit.py` confirmed at 84% coverage via a real pytest run
  (sandbox had pypi egress this session); no new bugs found
- `2026-09-21-session82c-eval-mode-isolation.md` — real bug: `evaluate_mode`'s
  per-position loop had no exception isolation, so one bad position could
  abort stop/target evaluation for every other open position that cycle;
  fixed with try/except + HOLD audit log per position; also added first-ever
  direct tests for `_load_profile`/`_trail_atr_mult`
- `SESSION82_ANGELONE_CROSS_LOOP_LOCK_READTIMEOUT_ROOT_CAUSE_2026-09-21.md` —
  root cause of the ReadTimeout storm: `AngelOneSession`'s single shared
  `asyncio.Lock` bound to whichever event loop touched it first, crashing the
  ws-feed background thread for good on any cross-loop contention; fixed with
  a per-event-loop lock (then a follow-up fix, 82b, to stop it leaking memory
  via one-shot `asyncio.run()` loops using a `WeakKeyDictionary`)
- `SESSION81_STALE_TEST_FIXES_AFTER_SESSION79_80_CHANGES_2026-09-21.md` — 3
  tests updated to match two already-deliberate production changes (session79's
  `MIN_TRADE_VALUE` default drop, session80's dead-exit-leg detection now
  requiring every leg dead, not just one) — no application code changed
- `SESSION77_COVERAGE_PLAN_PHASE1_PART2_2026-09-21.md` — 100%-coverage plan,
  Phase 1 continued: 21 new tests for `exit_engine/exit.py`'s
  CDSL/insufficient-funds/oversell(×3)/exchange-not-allowed branches, both
  `_cutoff_key` siblings (intraday-cutoff, security-intraday-restricted),
  and the generic-rejection streak escalation state machine; no new bugs found
- `SESSION77_COVERAGE_PLAN_PHASE1_PART1_2026-09-21.md` — 100%-coverage plan,
  Phase 1 continued: 12 new tests for `exit_engine/exit.py`'s
  `expire_stale_exit_orders()` and `_send_real_sell`'s success/invalid-IP/
  pre-migration-fallback paths, all previously 0% direct; no new bugs found
- `SESSION76_CIRCUIT_LIMIT_EXIT_RESEND_FIX_2026-09-21.md` — `_send_real_sell`'s
  circuit-limit rejection branch never set the `_cutoff_key` resend-suppression
  flag its sibling branches use, so a circuit-locked position's SELL was
  resent to Dhan every exit cycle all day instead of once; fixed, 2 new tests
- `SESSION75_STUCK_RECONCILE_STATUS_FILTER_FIX_2026-09-20.md` — real bug found
  from live `/reconcile/pending` data: `resolve_stuck_pending()`'s status
  filter silently excluded STOP_HIT/TARGET_HIT rows from ever being
  self-healed or aged-out, so a stale EOD_SQUAREOFF sentinel could sit
  forever on an already-correctly-resolved position; fixed, 4 new tests
- `SESSION74_DEEP_AUDIT_NO_NEW_BUGS_2026-09-20.md` — full (not pattern-swept)
  read of decision-prediction-service's `training/models.py` and
  `training/app.py`, plus a repo-wide sweep for mutable-default-args/bare-except/
  unguarded-division; no new bugs found — remaining open items are config
  decisions, infra, or awaiting live verification, not code
- `SESSION73_CROSS_SERVICE_AUDIT_FIXES_2026-09-20.md` — capital_share_cap
  blind spot to the other service's holdings (new shared-exposure table),
  position-stocks-service exit-placement retry backoff (mirrors
  real-trade-service's session40 fix), overnight-hold sector diversification cap
- `SESSION24_ROOT_DEDUP_CLEANUP.md` — removed root-level duplicates left
  behind by a previous zip repackage (files were already archived but
  never deleted from root)
- 2026-09-11 — quality gate, intraday-restriction list, Dhan P&L summary,
  overnight orchestrator, Reset Failures fix (this session — see the PR/
  commit this shipped in, not yet filed as its own archive note)
- `SESSION23_EOD_SAME_DAY_ENTRY_AND_US_SECTOR_SIGNAL.md`
- `SESSION22_EOD_SQUAREOFF_TIME_AND_OVERNIGHT_SIGNAL_SCAN.md`
- `SESSION21E_LIVE_EVIDENCE_INTRADAY_FIXES.md`
- `SESSION21D_REAL_TRADE_SERVICE_AUDIT.md`
- `SESSION21C_EOD_SQUAREOFF_AND_SELFHEAL_FIXES.md`
- `SESSION21_REAL_TRADE_FIXES.md`
- `OVERSELL_SYNC_IMPORTERROR_FIX.md`
- `INSUFFICIENT_FUNDS_SELL_FIX.md`
- `GATE6_CALIBRATION_AND_CIRCUIT_BREAKER_FIX.md`
- `CDSL_SAME_DAY_EXIT_FIX.md`
- `TICK_SIZE_FLOAT_PRECISION_FIX.md`
- `CLAUDE_SESSION.md`, `CLAUDE_SESSION3.md`

Older notes (pre-Sept 2026) are one level deeper, already archived from a
prior cleanup — same folder, just look for the earlier dates.

Setup/deploy docs stay at repo root, not here: `README.md`,
`DEPLOY_GUIDE.md`, `SETUP-GUIDE.md`, `ORACLE_SETUP_GUIDE.md`.
