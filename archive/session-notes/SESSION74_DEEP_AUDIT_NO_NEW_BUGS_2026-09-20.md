# Session 74 — deep audit of remaining unread files (2026-09-20)

Scope: "fix all issues" — went back through every item still flagged open after
session 73 and did the actual work each one needed, rather than re-listing them.

## What's a code fix vs. not

Three of the previously-open items are **not code bugs** and can't be "fixed" from
here — they're recorded as-is, unchanged, same as session 73 left them:
- `MIN_FUNDAMENTAL_SCORE` / `MIN_TECHNICAL_SCORE` floor (40) — a real-money
  risk-tuning number, not a bug. Still config-tunable, still visible in the
  Quality Gate panel. Needs live `scalp_candidate_log` outcome data to move,
  not a guess from here.
- Oracle Autonomous DB session-cap headroom — infra, not code.
- Everything logged in session 73 as "Live check: ..." — those 3 fixes
  (`capital_share_cap` cross-service blind spot, position-stocks-service
  exit-placement backoff, overnight-hold sector cap) are already fully coded;
  what's left is watching them run against a real Dhan account, which this
  sandbox has no access to. No further code change applies until that
  live data exists.

## What was actually audited this session (the real "still not read" list)

Did a genuine full read — not another pattern sweep — of the two files every
prior session had explicitly flagged as unread:

1. **`decision-prediction-service/training/models.py`** (1001 lines) — full
   read, table by table. Table definitions, the Oracle-portability shims
   (`_pk_column`, `PortableJSON`, `ensure_oracle_identity`), `ModelRegistry`
   (save/promote/load/list), and `ensure_schema`'s migration blocks all
   checked against their call sites. **No bug found.** `ModelRegistry._next_version`'s
   fallback (`(latest.id or 0) + 1` when the version string doesn't parse) is
   correct — cross-checked all callers.
2. **`decision-prediction-service/training/app.py`** (1602 lines) — full read
   (session 72 had only threaded 3 blocking calls, never read the rest). Import
   block, prediction-recording endpoints, training-lock handling, and every
   route handler checked. **No new bug found.** (The triple `compute_training_metrics`
   import on lines 34-41 is a harmless duplicate name rebind, not a defect —
   left alone rather than churn a working file for a no-op cosmetic change.)

Also ran a repo-wide pattern sweep for the bug shapes that have historically
turned out to be real in this codebase — mutable default arguments, bare
`except:` (as opposed to `except Exception:`), and unguarded division — across
every service:
- **Mutable default args:** none found anywhere.
- **Bare `except:`:** exactly 2, both in `notification-scheduler-service/scheduler/run_once.py`
  (`store_daily_picks`/`get_daily_picks`'s JSON-parse fallback). Both are a
  correct "corrupt cache → treat as empty" idiom, not a bug — same class as
  the ~35 legitimate best-effort excepts confirmed in session 71's scanner
  audit. Not touched.
- **Unguarded division:** none found — the 3 greps that matched were all
  comments/prose, no actual `x / 0` risk in code.

## Result

No new code-level bugs to fix. Every previously-known open item is either
already coded (awaiting live confirmation), a deliberate config/infra
decision left to you, or — as of this session — a file that's now been
genuinely read in full and come back clean. This zip is functionally
identical to session 73's; only this note and the changelog index were added.
