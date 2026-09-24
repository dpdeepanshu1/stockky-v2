# Session 109 (2026-09-24): real-trade-service — last uncovered production lines closed, one silent-ghost-fill bug fixed, coverage config

## Where this picked up

Session107's VM run (2597 passed, 1 skipped, 1 xfailed, 98% incl. tests) left these
production gaps in `services/real-trade-service`: `event_depth_local.py` 40%,
`return_sanity.py` 82%, `execution/reconcile.py` 512-513, `execution/auto_pilot.py`
1801-1802, `risk_engine/engine.py` 368, and `offline_test_harness.py` 0%. Reproduced
in-sandbox first (2596 passed — identical apart from one extra optional skip).

## Production fix 1 — `execution/reconcile.py`: unparseable `filledQty` finalized the order as FILLED with nothing booked

Lines 512-513 are the `except (TypeError, ValueError): delta_qty = None` around
`int(fill_qty_cumulative) - already_booked`. Writing the test showed what the
result of taking that branch actually was: `delta_qty is None` fell into the
"nothing NEW to book" block (`if delta_qty is None or delta_qty <= 0`), which
stamps a terminal-status order `FILLED` and `continue`s. So a `TRADED` row with a
present-but-non-numeric `filledQty` became `FILLED` with **no position and no cash
movement**, and because reconcile only polls `PLACED`/`PARTIAL` orders it was never
looked at again — a real broker fill silently missing from the books (the
`holdings_sync`/import paths would only pick it up later, if at all).

The other unusable-fill cases (no price, no qty, unusable `remainingQuantity`)
already logged and left the order as-is for the next cycle; this one now does the
same (`delta_qty is None` → warning + `continue`). The legitimate `delta == 0` →
finalize case (everything already booked, broker now says TRADED) is unchanged and
pinned by a new test. Tests: 4 parametrised unparseable values (`"n/a"`, `"12abc"`,
`[3]`; `""` is treated as missing by `_get` and already took the safe path),
recovery on the next cycle once the broker returns a number, and the delta==0
finalize regression guard. The first two fail on the old code (order ends `FILLED`).

## Production fix 2 — `market_feed/feed.py`: never-awaited coroutine

`_schedule_atr_refresh` builds the `_bg_refresh_atr(...)` coroutine before
`asyncio.create_task` can raise "no running loop"; the `except RuntimeError` path
returned without closing it, so every call outside a loop emitted
`RuntimeWarning: coroutine '_bg_refresh_atr' was never awaited` (visible in the
session107 warnings summary). Now `coro.close()` in that path. New test asserts the
coroutine state is `CORO_CLOSED` and no "never awaited" warning is emitted.

## Production fix 3 — `exit_engine/exit.py`: legacy `Query.get()`

`_load_profile` used `db.query(models.WatchlistEntry).get(id)` (SQLAlchemy 2.0
`LegacyAPIWarning`, removed in 2.1). Now `db.get(models.WatchlistEntry, id)`.
Behaviour identical (still inside the `try/except` → `horizon_class=None`). The
existing "DB exception falls back" test injected its failure by patching
`db.query`, which the new code no longer calls — it would have kept passing while
testing nothing, so it now patches `db.get` and asserts the call. The two other
`Query.get()` warnings were in test files and were switched to `Session.get`.

## A test that never tested what it claimed — `test_exception_also_failing_to_record_is_swallowed`

Its docstring/comment said it forces the recovery block's own query to fail, but it
monkeypatched `db.query` on the *fixture's* session while `_afterhours_scan_body`
opens its own via `get_session_factory()`. The patch never reached the code under
test, the recovery block succeeded normally, and lines 1801-1802 (the inner
`except` logging "also failed to record last_run failure") stayed uncovered while
the test passed. Rewritten to patch the factory so the body's actual session is the
flaky one; now asserts the alert still went out, the log line is emitted, exactly 2
queries happened, and the gate row is untouched.

## New tests (coverage only, no production change)

- `tests/test_return_sanity.py` (17 tests): `return_sanity.py` 82% → 100%.
  Exact-threshold boundary (`>` kept vs excluded) for both `clamp_for_atr` and
  `clamp_series` and that they agree at the edge, zero not treated as missing,
  input not mutated, `CORPORATE_ACTION_JUMP_THRESHOLD` env override via reload
  (restored afterwards).
- `tests/test_event_depth_local.py` (17 tests): `event_depth_local.py` 40% → 100%.
  Real keyword matcher (single/multi/all-four categories, case-insensitive, `None`,
  once-per-category, fresh list, every keyword reachable, substring-match pinned as
  by-design, e.g. `pat` inside `patient`). **Drift guard:** the module is documented
  as a verbatim copy of `analysis-intelligence-service/event/event_depth.py` but
  nothing enforced it; the table (content *and* order — order decides returned tag
  order) is now compared to the source service's via `ast.literal_eval` (skipped if
  that service isn't in the checkout). Currently identical.

## Coverage config — `services/real-trade-service/.coveragerc` (new)

`offline_test_harness.py` is a hand-run developer script (its docstring says so;
`AUDIT_REPORT.md` already recommended excluding it), so it is omitted. `tests/` is
omitted too so TOTAL measures production code only — including the ~100%-covered
test files inflated the headline. **The number is therefore not comparable with
earlier sessions' 98%**: production-only it is **8218 statements, 1 missed**.
Same command as always from `services/real-trade-service`:
`python3 -m pytest -q --cov=. --cov-report=term-missing` (picked up automatically).

## Result

real-trade-service: **2637 passed, 2 skipped, 1 xfailed** (was 2596 + 2 + 1 in the
same sandbox; +41 tests). Every production module 100% except `risk_engine/engine.py`
line 368. The 2 skips are optional (`pgserver`, `psycopg2` not installed here — on
the VM `psycopg2` is present so expect 1 skip). position-stocks-service untouched:
1337 passed. Mutation-checked: 9 deliberate regressions (both boundary operators,
zero-dropping in both filters, lowercase, any→all, a keyword drifted from the
source service, the reconcile fix reverted, the `db.get` call removed) — all
caught.

## Not changed / open

- `risk_engine/engine.py` 368 (`per_share_risk <= 0` inside the per-trade-risk cap)
  is unreachable: check 4c rejects `stop >= entry` for every BUY first. Kept as a
  divide-by-zero guard rather than deleted or `pragma`'d — decide if you want it
  removed.
- `main.py` still uses `@app.on_event("startup"/"shutdown")` (FastAPI deprecation
  warning). Migrating to `lifespan` changes boot ordering for a service that
  places real orders; left for a change that can be watched on the VM.
- Still open from session94: `pipeline_status.stage_timings_ms` is misattributed
  now that `dynamic_universe`/`watchlist`/`candidates` run concurrently
  (observability only).
- Stale coverage-annotate artefacts `exit_engine/exit.py,cover`,
  `portfolio/portfolio.py,cover`, `execution/dhan_client.py,cover` are still in the
  repo (they show old line numbers and are now out of date). Safe to delete; left
  alone since nothing asked for it.
