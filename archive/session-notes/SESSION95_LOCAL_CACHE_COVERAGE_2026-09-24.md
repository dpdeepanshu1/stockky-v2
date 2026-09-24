# Session 95 (2026-09-24): resilience/local_cache.py — 45% → 100%

## What was already in this zip coming in

The pasted VM transcript confirmed session94 on the live box: **1784 passed,
1 xfailed, 93% total, `cycle_runner.py` and `tests/test_cycle_runner.py` both
100%.** Nothing failing. Next item off the session94 priority list:
`resilience/local_cache.py` (62 stmts, 45%, 34 missed) — the module where the
session46 / 2026-09-16 "snapshot froze forever at zero open positions" bug
lived, and the store behind every alert-cooldown, resend-suppression and
reject-streak flag in `exit_engine/exit.py`.

## What was added

New `tests/test_local_cache.py` (32 tests) against a REAL in-memory SQLite DB
(real `ResilienceCache` / `TradePosition` / `TradeAuditLog` tables, real audit
logger) — the code under test is never mocked.

- **`save_snapshot`** — insert; in-place upsert (asserted to be a clean
  SELECT→UPDATE with **no rollback**); `default=str` for non-JSON types; the
  2026-09-12 two-writer race, reproduced for real (a second session commits
  the row after our SELECT saw nothing, so our INSERT hits the PRIMARY KEY —
  a genuine `IntegrityError`) and shown to fall back to UPDATE with the
  loser's payload actually landing; the retry-after-conflict itself failing
  (logged, swallowed, other writer's row untouched); generic DB failure
  (rollback + warning, never raises, nothing persisted).
- **`load_snapshot`** — miss / hit / corrupt JSON / DB failure.
- **`snapshot_open_positions`** — payload shape, `None` stop/target kept,
  DEMO/REAL key isolation, and the **2026-09-16 regression**: an empty list
  must still write, replacing the stale non-empty snapshot.
- **`reconcile_on_startup`** — no snapshot; match; both mismatch directions
  with the exact `RECONCILE_MISMATCH` audit detail (sorted lists,
  `snap_as_of`); the **2026-09-12 PARTIALLY_CLOSED** fix; CLOSED not live;
  mode isolation both ways; `{}` snapshot skipped; snapshot with no
  `positions` key; an **end-to-end** proof the 2026-09-16 fix removes the
  spurious post-restart mismatch; whole-function failure swallowed.
- **Key-length invariant** — `trade_resilience_cache.key` is `String(64)`;
  SQLite ignores that, Postgres/Oracle enforce it, and `save_snapshot`
  swallows the error, so an over-long key would silently never persist in
  production while passing every test here. Checked every key format written
  anywhere in the service (`exit.py`, `reconcile.py`, `auto_pilot.py`,
  `feed.py`, …) at worst-case id widths: **longest is 42 chars — no bug.**

## Verification

- Full suite as on the VM: **1816 passed, 1 xfailed** (1784 + 32), overall
  93%, no regressions. `pyflakes` clean.
- **Mutation-checked**: 14 deliberate regressions to `local_cache.py` (drop
  the UPDATE fallback; no rollback on either failure path; always-INSERT;
  corrupt JSON raising; re-introduce the 2026-09-16 skip-on-empty bug; mode
  missing from the key; re-introduce the 2026-09-12 OPEN-only compare; no
  mode filter; mismatch not audited; detail dropped; outer failure not
  swallowed; falsy snapshot not skipped; subset-only comparison). First run
  caught 13; the survivor (always-INSERT) was an *equivalent-outcome* mutant —
  the IntegrityError fallback repairs it so the final data is identical — and
  was killed by asserting a routine upsert triggers no rollback. Final:
  **0 survivors**; `local_cache.py` restored byte-identical.

**No production code changed.**

## Observations (not changed)

1. **`json.dumps` sits outside `save_snapshot`'s `try`.** Every DB failure is
   swallowed, but a payload JSON cannot encode even with `default=str`
   (e.g. non-string dict keys) raises `TypeError`. No current call site can
   trigger it (all payloads are str-keyed dicts); pinned by a test so any
   future change is a conscious choice.
2. **Startup reconcile is false-positive-prone by construction.** The
   snapshot is written in `cycle_runner` *before* exit evaluation/reconcile,
   so anything that changes positions afterwards (same-cycle exits, a manual
   Close Position, a fill) makes the next restart report a
   `RECONCILE_MISMATCH` until the next cycle refreshes the snapshot. That
   matches the module's stated "visibility, never auto-correct" intent, so
   treat a single mismatch after an active session as expected noise; a
   mismatch that persists across cycles is the real signal.
3. Carried over from session94: `pipeline_status` stage timings are
   misattributed since session48b (observability only) — still open.

## Still open, in priority order

1. `auth/dhan_credentials.py` (18%, 159 missed) — token/TOTP handling
2. `db.py` (7%, 482 missed) — migrations; the session-11 model-vs-migration
   bug class
3. `market_feed/feed.py` (66%), `entry_engine/entry.py` (84%)
4. small modules: `notifier.py` 52%, `symbol_master.py` 29%,
   `shared_adaptive.py` 27%, `boot_forensics.py` 18%, `admin_auth.py` 61%,
   `shared_exposure.py` 76%, `event_depth_local.py` 40%, `pipeline_status.py`
   91%
5. `offline_test_harness.py` (288 stmts, 0%) is a dev harness — consider
   excluding it from coverage.
