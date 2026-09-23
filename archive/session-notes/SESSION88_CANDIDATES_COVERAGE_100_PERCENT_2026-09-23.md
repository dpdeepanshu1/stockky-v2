# Session 88 (2026-09-23) — candidate_engine/candidates.py: 65% → 100%

Follow-up to session87 (rounds 1-2: 0%→65%, self-contained helpers +
`_multi_tf_analysis`/`_volume_shock_analysis`). This session closed the
file out completely: the three deferred DB-writing cycle orchestrators
plus every stray single-line gap the session87 note flagged.

Run through live pytest + coverage this session (sandbox had working
pip/network access).

## What was added

New `tests/test_candidates_orchestration.py` (24 tests), round 3:

- **`_refresh_standard_candidates`** (9 tests): no-source-rows short
  circuit, exclude-set filtering, intraday-restricted-symbol filtering
  (both the success path and the lookup-failure fallback), the mcap-floor
  reject vs MTF reject vs insert three-way split, the `data_starved`
  majority diagnostic warning (>50% of a cycle's symbols), the
  quote-only-failed diagnostic warning (≥3 symbols with a resolvable
  history but no quote), and the `pipeline_status.set_source` failure
  being swallowed.
- **`_refresh_volume_shock_candidates`** (10 tests): no-candidates
  short-circuit, restricted-symbol filtering + lookup failure, a full
  six-symbol pipeline exercising an analysis-task exception, a plain
  no-quote reject, a quality-gate rejection, and all three tier labels
  (`VOLUME_SHOCK_UPPER_CIRCUIT` / `_HIGH_CONVICTION` / plain
  `VOLUME_SHOCK`) landing in the DB with the right `decision_label`, the
  no-quote-majority diagnostic warning, the whole quality-gate scoring
  pass raising (client construction fails on its *second* open — the
  first, unrelated `httpx.AsyncClient()` for the universe/prefetch phase
  has to keep succeeding, which needed a call-counting fake client to
  isolate correctly) and falling back to an ungated pass-through, the
  `late_exclude_event`/`late_exclude_holder` wiring actually skipping a
  symbol the standard track already claimed, and — closing the last 6
  lines — `adaptive_market_params.record_metric` raising for both
  `universe_atr_pct` and `universe_adx` (both best-effort/non-fatal) plus
  one gate symbol's own `_fetch_fund_tech_score` raising inside the
  gathered quality-gate tasks without taking the whole batch down.
- **`refresh_candidates`** (1 test): the top-level orchestrator's own
  logic — computing `open_syms`/`cooldown_syms`/`shock_cooldown_syms`
  from real DB rows, wiring the `standard_seen_event`/`holder` so the
  shock track's insert loop genuinely waits on and reads the standard
  track's seen-symbols set, and summing both tracks' return counts —
  verified with the two sub-functions themselves monkeypatched (they're
  already covered directly by their own test classes above).
- **6 stray-line fills** the session87 note called out by number:
  `_volume_is_healthy`'s `avg20<=0` branch (only reachable via
  *negative*-volume bad data, since the truthiness filter already drops
  zero-volume candles before the average is taken — the existing
  all-zero-volume test was actually hitting the earlier `len(vols)<10`
  branch, not this one), `_near_resistance`'s `recent_high<=0` branch (no
  candle in the window carries a truthy `"high"` key), `_multi_tf_analysis`'s
  quote-task-raises→`quote=None` normalization, `_rows_from_ipo`'s
  non-dict/symbol-less item skip, `_volume_shock_analysis`'s equivalent
  `avg20<=0` branch (same negative-volume trick), and
  `_recently_candidated_symbols`'s gate6-requeue-shrink lookup itself
  raising (verified the fail-safe: the full-cooldown exclusion set
  computed by the first query is kept, not lost).

Approach matches `auto_pilot.py`'s own two-part split and this file's own
rounds 1-2: rather than re-simulating every chained `httpx` call these
three orchestrators make (already covered directly by rounds 1-2's tests
of the functions they call), each orchestrator's own lower-level
dependencies (`_fetch`, `_multi_tf_analysis`, `_fetch_market_cap_cr`,
`_prefetch_quotes_bulk`, `_fetch_volume_shock_universe`,
`_volume_shock_analysis`, `_fetch_fund_tech_score`,
`intraday_eligibility.get_restricted_symbols`, `pipeline_status`,
`adaptive_market_params.record_metric`) are monkeypatched directly, so
these tests exercise the orchestration layer's own control flow, not the
lower-level pieces a second time.

## Bugs found this session

None — this was a pure coverage-closing pass over already-shipped logic,
not an audit. No production code in `candidate_engine/candidates.py`
was touched.

## Result

`candidate_engine/candidates.py`: **689/689 statements, 100% coverage**
(0 missing). Full `real-trade-service` suite: **1150 passed, 1 xfailed**
(up from session87's 1126 — the 24 new tests, nothing else changed, no
regressions). Overall `real-trade-service` repo coverage: 78% → 80%.
`position-stocks-service`'s own suite re-run unchanged: 1221 passed
(confirms this session's changes, all scoped to
`services/real-trade-service/tests/`, didn't touch it).

## Commands to test

Just this file's three rounds:
```bash
cd services/real-trade-service
python3 -m pytest tests/test_candidates_helpers.py tests/test_candidates_analysis.py tests/test_candidates_orchestration.py -q --cov=candidate_engine.candidates --cov-report=term-missing
```
Expected: `689 stmts, 0 miss, 100% cover`, `157 passed`.

Whole service, full suite with coverage:
```bash
cd services/real-trade-service
python3 -m pytest -q --cov=. --cov-report=term-missing
```
Expected: `1150 passed, 1 xfailed`, overall 80%.

Whole system (both services, from repo root):
```bash
for s in position-stocks-service real-trade-service; do
  echo "=== $s"
  (cd services/$s && python3 -m pytest tests -q -p no:cacheprovider | tail -1)
done
```
Expected: `position-stocks-service` 1221 passed; `real-trade-service`
1150 passed, 1 xfailed.

## Not done / still open (broader coverage list, unchanged from session87)

`candidate_engine/candidates.py` is now fully done — remove it from the
priority list. Remaining, from `AUDIT_REPORT.md`'s priority table,
largest gaps first:

- `main.py` — 0% (851 lines, the whole API surface)
- `watchlist_engine/*` — mostly 0% (`dynamic_universe.py`, `sources.py`,
  `watchlist.py`) except `decay.py` at 67%
- `db.py` — 7%
- `cycle_runner.py` — 7%
- `auth/dhan_credentials.py` — 14%, `oracle_compat.py` — 15%
- `adaptive_market_params.py` / `adaptive_thresholds.py` — 20% each
- `market_context/sector_signal.py` — 17%
- `notifier.py` — 23%
- `shared_adaptive.py` — 27%, `symbol_master.py` — 29%
- `watchlist_engine/afterhours_scan.py` — 13%
- `auth/admin_auth.py`, `boot_forensics.py`, `offline_test_harness.py` —
  all 0% (likely lower priority: admin/boot/offline-only code, not live
  trading logic)
- The known `--cov=intraday_eligibility` flag issue (module is at repo
  root, not `execution.intraday_eligibility`) — still flagged since
  session84, needs a corrected flag to see its real number
