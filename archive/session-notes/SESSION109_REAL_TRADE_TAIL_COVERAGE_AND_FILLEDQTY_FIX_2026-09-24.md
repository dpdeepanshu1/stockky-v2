# Session 109 (2026-09-24): real-trade-service — production code at 100%, two silent-approval/ghost-fill bugs fixed, coverage config

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

## Production fix 4 (round 2) — `risk_engine/engine.py`: NaN slipped past the "absolute veto authority"

The VM run after the first zip (2638 passed, 8218 stmts, **1 missed: `engine.py`
368**) left only the "unreachable" guard `per_share_risk <= 0` ("Stop price equals
entry price"). Working out why it was unreachable exposed a real hole: check 4c
rejects `stop >= entry`, but every comparison against NaN is False, so **NaN
passes 4c and then silently skips every downstream cap** (`order_risk >
max_trade_risk` is False for NaN, so no per-trade downsize). Reproduced on the
old code before changing anything:

    stop_price=NaN      -> APPROVED, 100 shares, all_checks_passed
    entry_price=NaN     -> APPROVED, 100 shares, all_checks_passed
    qty=NaN             -> APPROVED, approved_qty=nan
    adj_risk_pct=NaN    -> APPROVED, 100 shares (per-trade cap bypassed)

Realistic sources: a NaN ATR feeding a computed stop, or a JSON `NaN` through
`POST /risk-engine/check` / a manual ticket (pydantic accepts NaN by default).
Fix: 4c now rejects a BUY (`invalid_order`, "Non-finite value in order intent
(qty=…, entry=…, stop=…, adj_risk_pct=…)") if qty, entry, stop or a supplied
`adj_risk_pct` is not finite. SELLs are deliberately untouched — exits must never
be blocked by an entry-only check (test pins this). Finite-but-odd values (stop 0
or negative) are unchanged: they only *increase* per-share risk, so sizing shrinks
the order (conservative), pinned by a test rather than changed.

Then line 368: with 4c now guaranteeing a finite stop strictly below a finite
entry, the inline guard is provably dead *through* `evaluate()`. Rather than
delete a divide-by-zero guard or hide it behind `# pragma: no cover`, the sizing
step is extracted as `_qty_within_risk_cap(per_share_risk, max_trade_risk)`, which
returns 0 (→ the caller's existing "Even 1 share risks …" rejection) for a
non-positive / non-finite per-share risk or budget, and is tested **directly**
(including that `1000.0 // 0.0` really raises, i.e. the guard matters). Same
fail-closed behaviour if the check order is ever rearranged; the only wording
change is on the unreachable path. 30 new tests in `tests/test_risk_engine.py`
(`TestNonFiniteInputs`, `TestQtyWithinRiskCap`), 10 mutations, 0 survivors
(guard removed, each of the four fields dropped from the check, each of the three
helper conditions dropped, floor→round, guard applied to SELL too).

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

real-trade-service: **2667 passed, 2 skipped, 1 xfailed; 8226 statements, 0
missed — 100%** (production code; `.coveragerc` omits the dev harness and
`tests/`). The 2 skips are optional here (`pgserver`, `psycopg2` not installed in
the sandbox — on the VM `psycopg2` is present, expect 1 skip). Before this
session's second round the VM showed 2638 passed / 8218 stmts / 1 missed.
position-stocks-service untouched: 1337 passed. Mutation-checked: 19 deliberate
regressions across both rounds, all caught.

## Not changed / open

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
