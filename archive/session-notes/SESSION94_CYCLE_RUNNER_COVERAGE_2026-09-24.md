# Session 94 (2026-09-24): cycle_runner.py — 7% → 100%

## What was already in this zip coming in

The pasted VM transcript (`services/real-trade-service`,
`pytest -q --cov=. --cov-report=term-missing`) confirmed session93's
`intraday_eligibility.py` work is clean: **1720 passed, 1 xfailed, 92% total,
`intraday_eligibility.py` 100%.** Nothing was failing. (The transcript's
first line, `python: command not found`, is just Ubuntu having no `python`
alias — `python3` is the right command, as the rest of the run shows.)

## Why cycle_runner.py was picked

Ranked by (risk of the module) × (size of the gap) from that transcript:

| # | Module | Cover | Note |
|---|---|---|---|
| 1 | `cycle_runner.py` | 7% (114/122 missed) | every REAL cycle goes through it |
| 2 | `resilience/local_cache.py` | 45% | home of the session-46 frozen-snapshot bug |
| 3 | `auth/dhan_credentials.py` | 18% (159 missed) | token / TOTP handling |
| 4 | `db.py` | 7% (482 missed) | migrations — session-11 bug class |
| 5 | `market_feed/feed.py`, `entry_engine/entry.py` tails | 67% / 84% | scattered branches |

`cycle_runner.py` is the one place every caller converges: the manual Run
Cycle route, Auto-Pilot's timer and enter-at-open all call `run_cycle_core`,
and every existing test mocks it out at its own boundary — so the function
itself had never run under test. It also holds the **session48b
`asyncio.gather` concurrency change**, which had no test at all.

## What was added

New `tests/test_cycle_runner.py` (64 tests). Every collaborator is a
recording fake sharing one ordered call log, so tests assert exact
sequencing. `cycle_runner.py`: **7% → 100%** (122/122).

- **Wrapper** — manual-trigger market-hours warning (closed / open /
  `autopilot` and `enter_at_open` never consult the clock / notify failure
  and clock failure both non-fatal / warning also attached to the early
  token-reject result), mode upper-casing, every `pipeline_status` call
  best-effort, failure path (`end_cycle(error=...)` then re-raise; a raising
  `end_cycle` cannot mask the original exception).
- **REAL token pre-flight** — `token_needs_refresh` gating (2026-09-01 fix:
  was ~130 Dhan token calls/day), TOTP failures non-fatal, token rejection →
  early `auto_disarmed` result with **nothing downstream executed** (no
  equity sync, no candidates, no entry, no exit lock, no reconcile),
  `sync_real_equity` before any candidate work, DEMO skips all of it.
- **Ordering + concurrency** — the dynamic_universe→watchlist chain and the
  candidates refresh genuinely overlap (proven with `asyncio.Event`s in both
  directions — a regression to sequential execution fails the test rather
  than merely reordering a log); entry never starts until both finish;
  chain-internal order preserved; exact tail
  `entry → fills → expire → snapshot → [exit lock: exit → reconcile →
  mark_reconciled] → pstat_end`; REAL `fills` come from reconcile, not
  `check_pending_fills`.
- **Stage error isolation** — dynamic-universe failure / snapshot-save
  failure non-fatal; each of the watchlist stage's three steps failing
  returns `{"error": ...}`, skips the remaining steps, never blocks the
  cycle; position snapshot receives `mode` explicitly (2026-09-16 fix) and is
  still written for an empty position list; snapshot/`open_positions`
  failure non-fatal.
- **Exit lock** — acquired before exit, released after reconcile, released
  on exit failure and on reconcile failure; plus a test using the **real**
  `threading.Lock` proving it is genuinely held during exit evaluation.
- **Real `pipeline_status`** — a completed cycle lands in dashboard history
  with the right counts; a running cycle is visible at stage `entry`; a
  failed cycle is recorded with its error.

## Verification

- Full suite, run exactly as on the VM: **1784 passed, 1 xfailed** (1720 + 64),
  overall coverage 92% → 93%, no regressions. `pyflakes` + `py_compile` clean.
- **Mutation-checked**: 16 deliberate regressions applied to
  `cycle_runner.py` one at a time (sequential instead of concurrent in both
  orders; entry no longer waiting for the chain; unconditional TOTP refresh;
  token reject not returning early; exit lock not released on exception;
  REAL fills from the wrong source; snapshot losing its `mode`; warning
  check applied to every trigger; mode not upper-cased; missing
  `end_cycle(error=)`; watchlist failure escaping; `sync_real_equity`
  removed; `_mark_reconciled` removed; dynamic-universe snapshot never
  saved; `gate_armed` hard-coded) — **all 16 caught, 0 survivors**;
  `cycle_runner.py` restored byte-identical afterwards.
- Two sloppy spots in my own first draft were cleaned before running
  (a leftover always-true assertion, and `monkeypatch.undo()` hacks in the
  real-lock / real-pstat tests, replaced by a shared `_stub_stages` helper).

**No production code changed.**

## Finding (not fixed — needs a decision): stage timings are wrong since session48b

`pipeline_status` keeps ONE "current stage" per mode. Since session48b,
`dynamic_universe`, `watchlist` and `candidates` run concurrently and each
calls `set_stage`, so they overwrite each other. Probe with realistic
durations (universe 50 ms, watchlist chain ~100 ms, candidates 300 ms):

    stage_timings_ms: {'dynamic_universe': 0.0, 'candidates': 50.7,
                       'watchlist': 249.5, ...}

i.e. the slowest stage (candidates, the one worth watching) reports ~1/6 of
its real time while `watchlist` is over-reported. Total `duration_ms` is
still correct. **Impact is observability-only**: the frontend uses
`stage_timings_ms` only as "done" dots (presence, not value), so what is
actually visible is the live stage label flipping among the three concurrent
stages and wrong numbers in the `/pipeline` API response. Not fixed here
because the right fix (per-stage start/end timestamps in `pipeline_status`,
or a single combined stage label) touches `pipeline_status.py` semantics and
possibly `RealAutoTrade.tsx`'s stage dots.

## Still open, in priority order

1. `resilience/local_cache.py` (45%)
2. `auth/dhan_credentials.py` (18%)
3. `db.py` (7% — migrations; session-11 bug class)
4. `market_feed/feed.py` (67%), `entry_engine/entry.py` (84%)
5. small modules: `notifier.py` 23%, `pipeline_status.py` 35%, `symbol_master.py`
   29%, `shared_adaptive.py` 27%, `boot_forensics.py` 18%, `admin_auth.py` 61%,
   `shared_exposure.py` 76%, `event_depth_local.py` 40%
6. `offline_test_harness.py` (288 stmts, 0%) is a dev harness, not
   production code — consider excluding it from coverage (`.coveragerc`
   `omit`) so the headline number reflects real code.
