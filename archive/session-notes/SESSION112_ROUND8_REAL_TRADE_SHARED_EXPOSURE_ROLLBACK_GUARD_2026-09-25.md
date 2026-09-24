# Session 112, round 8 (2026-09-25): apply round 7's rollback-guard fix to real-trade-service's copy

Round 7 flagged that `services/real-trade-service/execution/shared_exposure.py`
is the intentional duplicate of `capital/shared_exposure.py`
(position-stocks-service) and has the identical bug: `publish_own_exposure`'s
`except` handler called `db.rollback()` **unguarded**, so on a dead
connection the cleanup `rollback()` could itself raise and escape the
function — breaking its documented "fail-open, never raises" contract, out of
`equity_sync.py` and from there whatever triggered the sync cycle. Both
sibling shared-table modules (`shared_order_budget`, `shared_symbol_lock`)
already guard this in both services; this was the one module where only one
of the two copies had been fixed.

## Fix

Same four-line change as round 7, applied to
`execution/shared_exposure.py`:

```python
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("shared-exposure: failed to publish own exposure: %s", e)
```

## Test added

`tests/test_shared_exposure.py::TestPublishOwnExposure::test_failing_rollback_inside_the_handler_does_not_escape`
— mirrors position-stocks-service's round-7 test: mocks `db.query` and
`db.rollback` to both raise, asserts `publish_own_exposure` does not raise
and logs the warning instead.

## Verification

pytest/sqlalchemy are not installed in this sandbox and there's no network to
install them (same constraint as every prior round). Verified instead by
stubbing `sqlalchemy.orm.Session` and `models.SharedServiceExposure` with
minimal shims, importing the real `execution/shared_exposure.py` unmodified,
and running the new scenario plus two existing ones directly against it:

* Rollback-failure scenario (the new test): before the fix this raised
  `RuntimeError: rollback failed too` out of `publish_own_exposure`;
  confirmed it no longer raises. Not run against the pre-fix file this round
  (round 7 already established that red/green cycle on the twin file) — the
  live run is the direct reproduction that the fixed code doesn't raise.
* Success path still never calls `rollback()`.
* A commit error still calls `rollback()` exactly once (existing guarded
  path unaffected by this change).

`py_compile` clean on both the module and the test file. `pyflakes` not
available (no network to install), same as prior rounds.

Both services' `shared_exposure.py` copies are now identical apart from the
service-name constants and docstrings, and both test suites cover the
rollback-failure branch. User should run the real-trade-service suite on
their own VM to confirm the new test passes alongside the existing 1577+
(round 6 baseline was position-stocks-service's count; real-trade-service's
own full-suite count wasn't re-verified this round in this sandbox — flagging
that for confirmation on the VM run).

## Next by priority (unchanged from round 7 — this round was the flagged
follow-up, not the next item on the list)

`tz_utils.py` 71% (12 missing), `boot_forensics.py` 70% (41),
`auth/admin_auth.py` 29% and `auth/dhan_credentials_ro.py` 46%,
`pipeline_status.py` 31%, then `db.py` 16%, `execution/dhan_client.py` 21%,
`feed/*` and `main.py`.
