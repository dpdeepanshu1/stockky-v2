# Session 112, round 15 (2026-09-25): fix 3 failing tests in `tests/test_db.py` (position-stocks-service)

Round 14 added `tests/test_db.py` for `db.py` but couldn't run it locally
(no `sqlalchemy` in that sandbox — verified by stubbing + `py_compile`
only). First real run on the VM: 3 failed, 1861 passed. All three were in
`TestEnsureColumns`. Two were bugs in the test file itself; one was a real
(if minor) bug in `db.py`'s `_ensure_columns`.

## 1. Real bug: `inspect(engine)` built once, outside the per-entry guard

`_ensure_columns`'s whole contract is "never let a migration failure crash
startup" — every table/column entry is wrapped in its own `try/except` so
one bad `ALTER` doesn't stop the rest. But `inspector = inspect(engine)`
was built **once, before the loop**, so if that single call raised (a
bad/uninitialized engine object, or a transient failure while SQLAlchemy
introspects the connection), the exception propagated straight out of
`_ensure_columns()` — and from there out of `init_tables()` — with nothing
caught or logged. `test_inspector_has_table_failure_is_caught_per_entry`
caught this directly: a fake engine whose `.connect()` raises made
`inspect()` raise `NoInspectionAvailable` before the loop even started.

**Fix:** moved `inspector = inspect(engine)` inside the per-entry `try`
block, so it's rebuilt (and re-guarded) on every iteration — a failure
there is now logged and skipped exactly like a failed `ALTER TABLE`, and
one entry's failure doesn't prevent the next entry's inspector from being
tried fresh.

## 2. Test bug: `test_existing_column_is_a_no_op` only pre-created one column

Pre-created `scalp_gate_state` with just `service_enabled` present, then
asserted **zero** ALTERs — which could only pass if every other
`scalp_gate_state` column in `_COLUMN_MIGRATIONS` (12 of them) was also
silently skipped. It was really testing "one column already exists,
eleven don't" while claiming to test the no-op path, and only "passed"
by coincidence of whatever order/short-circuit the pre-fix code happened
to have. Fixed to pre-create **every** `scalp_gate_state` column from
`_COLUMN_MIGRATIONS` (same pattern the neighboring
`test_missing_column_with_no_default_is_added_nullable` already uses for
its "all but one" setup), so the assertion now means what it says.

## 3. Test bug: `flaky_begin`'s `ctx.__enter__` override was never invoked

`test_a_failed_alter_is_logged_and_does_not_abort_the_rest` tried to make
the first `engine.begin()` raise by monkeypatching `ctx.__enter__` as an
**instance** attribute on the context manager `eng.begin()` returned.
Python's `with` statement resolves dunder methods on the *type*, not the
instance, for implicit protocol dispatch — so that override was silently
never called, `_ensure_columns` never saw a failure, and the test's
`assert _warnings(...) or any(ERROR...)` failed because nothing was ever
logged. Rewrote `flaky_begin` as a real `@contextlib.contextmanager`
generator (raises before the first `yield` on the first call, delegates
to the real `engine.begin()` after that) — `contextlib`'s generated
context manager has genuine class-level `__enter__`/`__exit__`, so the
`with engine.begin() as conn:` in `db.py` now actually sees the simulated
failure on the first table and succeeds on the rest, matching what the
test name says it checks.

## Verification

Still no working local `sqlalchemy`+`pytest` in this sandbox (no
network access to install them), so these fixes are reasoned through and
statically checked (`py_compile` on both `db.py` and `tests/test_db.py`)
rather than re-run here. Please re-run:

```
cd services/position-stocks-service
python3 -m pytest -q --cov=. --cov-report=term-missing
```

and confirm all `TestEnsureColumns` cases pass (previous run: 1861
passed, 3 failed — all three addressed above; no other files touched).

## Next by priority (unchanged from round 13/14's list)

`execution/dhan_client.py` 21%, `feed/*`, and `main.py`.
