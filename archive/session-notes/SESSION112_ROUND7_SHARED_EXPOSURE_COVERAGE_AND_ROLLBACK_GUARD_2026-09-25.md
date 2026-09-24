# Session 112, round 7 (2026-09-25): `capital/shared_exposure.py` coverage 36% → 100% + one real fix

Next item by priority after round 6. This is this service's half of the
cross-service exposure table (`stockky_shared_service_exposure`):
`publish_own_exposure` runs at the tail of every `ledger.sync_from_broker`, and
real-trade-service reads the value back to complete the account total its 50%
`capital_share_cap` is checked against. Until now only a stub of it existed in
the ledger tests: 36%, 16 missing lines (64-73, 80-85) — the whole upsert and
both fail-open branches.

Baseline (round 6 zip): 1577 passed, 86% overall.
After: **1613 passed, 87% overall, `capital/shared_exposure.py` 28 stmts / 0
missed (100%)** — and with this, every module in `capital/` is at 100%.

## The bug (found by the new tests, fixed)

`publish_own_exposure` documents "Fail-open — never raises", but its `except`
handler called `db.rollback()` **unguarded**. If the connection is dead — the
very situation the handler exists for — `commit()` fails and then the cleanup
`rollback()` fails too, and that second exception escapes the function, out of
`ledger.sync_from_broker()` (it is the last statement there) and from there
into `POST /ledger/sync`. Both sibling shared-table modules
(`shared_order_budget`, `shared_symbol_lock`) already wrap their rollback in
`try/except`; this one had been missed.

Reproduced first as a red test on the unfixed code
(`test_failing_rollback_inside_the_handler_does_not_escape`:
`RuntimeError: rollback failed too` propagating), then fixed:

```python
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("shared-exposure: failed to publish own exposure: %s", e)
```

Severity: low (visibility-only table; the ledger row itself is committed
before this runs, and a failed sync retries next cycle) — but it broke a stated
safety contract and the fix is four lines.

## What was added

`tests/test_shared_exposure.py` (36 tests):

* **Upsert** — creates when absent, updates in place (never duplicates),
  committed and visible to a second session, never touches the other service's
  row, `0` is a real value that overwrites a stale non-zero one, input
  normalisation (`None`/`0`/negative → `0.0`, numeric string coerced, int →
  float), bad input swallowed with the previous value intact and no
  half-written row on first publish, session usable afterwards.
* **Fail-open** — real `OperationalError` (table missing), query error, commit
  error, success path never rolls back, and the new rollback-failure case.
* **Reader** — no row / null value / zero → `0.0` **without** a warning ("peer
  hasn't published yet" is normal); reads only the other service's row;
  always a `float`; DB failure, query error and garbage stored value → `0.0`
  with a warning.
* **Wiring** — the REAL (unstubbed) publish through `ledger.sync_from_broker`:
  publishes committed capital of OPEN + EXIT_LEGS_REJECTED positions (CLOSED
  excluded), a flat book overwrites a stale value with 0, and a broken
  exposure table does not break the ledger sync.
* **Drift guard** — mirror of the one in real-trade-service's copy: service
  names are mirror images, both models map the same table/key column, names
  fit `String(32)`. Skipped if real-trade-service isn't next to this service.

## Verification

* Full position-stocks-service suite: 1613 passed, 0 failed. `pyflakes` clean
  on the new test file and the changed module.
* Mutation check: 22 hand-written mutations of the fixed module (wrong service
  name on lookup/insert/constants, inverted `row is None`, dropped `db.add` /
  `commit`, removed clamp / `or 0.0` / `float()`, replaced value with `0.0`,
  unguarded or removed rollback, swallowed-vs-raised handlers, wrong fallback
  values, dropped guards on the reader). 21 killed first pass; 1 survivor
  (dropping the reader's `row and value` guard is equivalent in return value —
  the `AttributeError`/`TypeError` is caught and yields `0.0` — but changes
  logging) was closed by asserting no warning is emitted on the normal
  no-row / null-value paths. 0 survivors.

## Flagged — not changed (real-trade-service)

`services/real-trade-service/execution/shared_exposure.py` is the intentional
duplicate and has the identical unguarded `db.rollback()` (its `equity_sync.py`
is the caller). Not touched this round: it's the other service and its suite
was not part of this pass. Same 4-line fix as above; its
`tests/test_shared_exposure.py` (31 tests, currently green) has no
rollback-failure case. Say the word and I'll apply + test it there so the two
copies stay identical.

## Next by priority

`tz_utils.py` 71% (12 missing), `boot_forensics.py` 70% (41),
`auth/admin_auth.py` 29% and `auth/dhan_credentials_ro.py` 46%,
`pipeline_status.py` 31%, then `db.py` 16%, `execution/dhan_client.py` 21%,
`feed/*` and `main.py`.
