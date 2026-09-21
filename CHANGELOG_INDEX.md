# Changelog index

Per-session fix notes live in `archive/session-notes/` (one file per
session, dated). This file is just an index so they're discoverable
without 50+ files cluttering the repo root.

Most recent first — see each file for full detail:

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
