# Session 27 — shared-module drift audit + cross-service contract re-verification (2026-09-12)

## Scope
Continuing the sweep with the remaining named areas: rest of api-gateway
(`ipo_scanner.py`, `surprise_scanner.py`, `data_feed.py`, `hotpicks_store.py`,
`instant_scanner.py`, `buy_sniper.py`), the api-gateway `/stockky-hot` and
`/scan/universe` endpoints, decision-prediction-service's `circuit_breaker.py`,
and a full-repo syntax pass. Given the size of what remains (~80k lines of
Python across the repo), this round prioritized (a) a technique proven to
catch real bugs last round — checking shared "should be identical" modules
for drift, and cross-service field contracts against actual source — over a
shallow pass across everything. See "Honest scope remaining" below for what
this does **not** cover yet.

## Found & fixed — one real bug

**`oracle_compat.py`'s `exec_ddl_safe()` swallowed a smaller set of Oracle
error codes in six of its seven copies than in api-gateway's.** This module
is duplicated verbatim across api-gateway, real-trade-service,
decision-prediction-service (`decision/` and `training/`),
notification-scheduler-service, analysis-intelligence-service, and
market-data-service — confirmed identical by hash before this fix. Only the
api-gateway copy had been updated to also swallow `ORA-02260` ("table can
have only one primary key") and `ORA-02264` ("name already used by an
existing constraint"), with a docstring explaining these matter for tables
that declare a **named table-level constraint** (e.g. a composite PRIMARY
KEY) — re-running that DDL trips on the constraint name rather than the
table name, and the other five services never got that fix propagated to
their copy. Currently no service other than api-gateway declares a named
table-level PK constraint (checked via grep), so this hasn't caused an
observed failure yet — but it's a live landmine: if any service's schema
setup grows one of these (or the table gets copied from api-gateway's schema
verbatim), a normal idempotent re-run of that DDL on service restart would
raise instead of being swallowed, exactly like the original ORA-00955 case
this function exists to handle. Propagated the two extra codes + updated
docstring to all six lagging copies; all seven `oracle_compat.py` files are
now byte-identical again (verified via `md5sum`) and `py_compile`-clean.

## Checked and confirmed correct (no bug found) — with what was verified

- **`rate_limiter.py` (4 copies) and `circuit_breaker.py` (4 copies) also
  differ across services**, but this is legitimate per-service evolution,
  not drift: e.g. market-data-service's copy has AngelOne-specific buckets
  and circuit-breaker integration api-gateway doesn't need in the same way,
  and api-gateway's copy has `pipeline_scope`/fail-fast/symbol-alias
  wiring specific to its own scan endpoints. Left alone — forcing these back
  to identical would itself be a bug.
- **Re-verified session 26's IPO/surprise fixes against the actual serving
  endpoints**, not just the scanner modules: traced `/surprise/ipo/list` →
  `ipo_scanner.get_ipo_list()` (emits `ipo_score`/`current_price`, confirmed),
  `/surprise/scan?cached=true&limit=20` → confirms the `limit` param really is
  honored and the response really is wrapped under `"stocks"`, and
  `/stockky-hot` → confirmed `news_driven`/`results_driven`/
  `bulk_insider_driven` items really do carry `decision`, `score`, `price`,
  and `close` exactly as `candidate_engine/candidates.py`'s
  `_rows_from_hot_picks()` expects (including the two-pass price-enrichment
  fallback). `/scan/universe`'s `momentum_movers` key also confirmed against
  `_rows_from_volume_shock()`'s expectation. No mismatches found in this
  contract this round.
- **decision-prediction-service's `circuit_breaker.py`** read in full — the
  half-open/opened_at restart bug it documents fixing is correctly handled
  (state-transition guard prevents concurrent in-flight failures from
  perpetually re-stamping `opened_at`). No new issue found.
- **Full-repo syntax check**: every `.py` file in the repo compiles cleanly
  (`py_compile`, zero errors).

## Honest scope remaining
This round did NOT do a line-by-line read of:
- decision-prediction-service: `decision/main.py` (72K), `decision/kv_cache.py`,
  `decision/horizons.py`, and the entire `training/` and `prediction/`
  subtrees (model training/walk-forward code, ~1M+ lines of joblib/pkl
  artifacts aside, still tens of thousands of lines of Python).
- notification-scheduler-service: `scheduler/run_once.py`,
  `overnight_orchestrator.py`, `governance_check.py`, `weekend_hydrator.py`,
  `symbol_master_sync.py`, `fundamentals_batch.py`, and `notification/main.py`
  (36K) — the actual scheduling/notification logic, as opposed to the shared
  `rate_limiter.py`/`kv_cache.py`/`oracle_compat.py` utility modules checked
  above.
- api-gateway: the internals of `ipo_scanner.py`, `surprise_scanner.py`,
  `data_feed.py`, `hotpicks_store.py`, `instant_scanner.py`, `buy_sniper.py` —
  this round verified their *output contracts* against known consumers, not
  their internal logic.
- The frontend (~22.5k lines) — not touched this round.

Recommendation unchanged from session 26: this is real-money-adjacent code,
so each further round should stay scoped to one or two services with an
honest per-round report, rather than one blanket "audited everything" claim.
