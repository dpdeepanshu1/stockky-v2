# Changelog index

Per-session fix notes live in `archive/session-notes/` (one file per
session, dated). This file is just an index so they're discoverable
without 50+ files cluttering the repo root.

Most recent first — see each file for full detail:

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
