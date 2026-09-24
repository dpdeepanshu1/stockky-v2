# Changelog index

Per-session fix notes live in `archive/session-notes/` (one file per
session, dated). This file is just an index so they're discoverable
without 50+ files cluttering the repo root.

Most recent first — see each file for full detail:

- `SESSION109_REAL_TRADE_TAIL_COVERAGE_AND_FILLEDQTY_FIX_2026-09-24.md` —
  real-trade-service's last uncovered production lines closed
  (`event_depth_local.py` 40%→100%, `return_sanity.py` 82%→100%,
  `reconcile.py`, `auto_pilot.py`); 41 new tests. **Bug fixed:**
  `execution/reconcile.py` stamped a `TRADED` order `FILLED` with no position and
  no cash booked when Dhan's `filledQty` was present but non-numeric (reconcile
  never re-polls FILLED, so the fill vanished from the books) — now left pending
  and retried like every other unusable-fill case. Also: never-awaited coroutine
  in `feed._schedule_atr_refresh`, legacy `Query.get()` in `exit._load_profile`,
  one vacuous test rewritten (it patched a session the code under test never
  used), keyword-table drift guard vs analysis-intelligence-service, new
  `.coveragerc` (omits the dev harness + `tests/`; production-only 8218 stmts,
  1 missed — not comparable with the old 98%). 2637 passed, 2 skipped,
  1 xfailed. 9 mutations, 0 survivors.
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
