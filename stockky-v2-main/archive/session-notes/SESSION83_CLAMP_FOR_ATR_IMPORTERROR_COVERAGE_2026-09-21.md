# Session 83 — `exit_engine/exit.py`'s `_clamp_for_atr` ImportError fallback (2026-09-21)

100%-coverage-plan Phase 1 #1, the last item session82c left open: the
module-level

```python
try:
    from return_sanity import clamp_for_atr as _clamp_for_atr
except ImportError:
    def _clamp_for_atr(x):
        return None if x is None or abs(x) > 30.0 else x
```

fallback (lines ~67-69) had zero direct coverage — every existing test just
imports `exit_engine.exit` normally, which always takes the `try` branch
since `return_sanity.py` is present in this service's own directory.

## What got added

`tests/test_exit_clamp_for_atr_importerror_fallback.py` (2 tests):

1. **Sanity check on the normal branch** — confirms `ex._clamp_for_atr is
   return_sanity.clamp_for_atr` under an ordinary import, so the fallback
   test below is provably exercising the *other* branch, not accidentally
   re-testing the same one.
2. **The fallback itself** — patches `builtins.__import__` to raise
   `ImportError` for the name `"return_sanity"` only (everything else falls
   through to the real `__import__`), then `importlib.reload`s
   `exit_engine.exit` under that patch. Confirms the reloaded module's
   `_clamp_for_atr` is no longer `return_sanity.clamp_for_atr` (i.e. the
   `except` branch really ran), then exercises the fallback's own contract
   directly: `None` in → `None` out, values within ±30 pass through
   unchanged, the boundary (`30.0`, kept) vs. just past it (`30.1`,
   excluded) on both signs, and an obvious corporate-action-sized jump
   (`1000.0`) excluded.

Restores the real import and reloads the module again in a `finally`
(both around the patch itself and around the whole test body) so every
other test module sharing this pytest process gets the production
`return_sanity`-backed `_clamp_for_atr`, not the fallback — verified with
an explicit assertion at the very end of the test, not just assumed.

## Verification

Ran for real this session (sandbox had pypi egress this time —
`pip install sqlalchemy pytest pytest-cov httpx pydantic pyjwt argon2-cffi
cryptography pyotp python-dotenv python-dateutil`, matching
`requirements.txt`):

```
tests/test_exit_clamp_for_atr_importerror_fallback.py::test_clamp_for_atr_uses_return_sanity_when_importable PASSED
tests/test_exit_clamp_for_atr_importerror_fallback.py::test_clamp_for_atr_importerror_fallback_behaves_correctly PASSED

592 passed, 1 xfailed in 27.61s   (full services/real-trade-service/tests suite — no regressions)
```

Coverage re-run:

```
exit_engine/exit.py     492     81    84%   179-180, 577, 688-689, 706, 724-729,
                                             745, 839-840, 861-868, 880, 901-906,
                                             934, 975-980, 1001, 1044-1049, 1060,
                                             1091-1092, 1197-1198, 1300-1303,
                                             1371-1398, 1435-1438, 1474-1482,
                                             1527-1533, 1541-1548
```

Up from 67% at the start of session77 (session77 parts 1-2 and session82c's
position-isolation/profile work already closed most of the gap; this
session's item was the last one session82c explicitly flagged as still
open). No new bugs found — the fallback behaves exactly as its inline
logic promises.

## Still open per `100_PERCENT_COVERAGE_PLAN.md` (Phase 1 #1, `exit.py`)

`exit.py` is now at 84%, comfortably past the plan's 90%-is-the-goal
statement is not yet met but the remaining 81 missed lines are a different
shape than before — no more "zero coverage" branches, just scattered
individual lines/short ranges across `evaluate_mode`'s trail/breakeven/
partial-exit tail (`1300-1303`, `1371-1398`, `1435-1438`, `1474-1482`,
`1527-1533`, `1541-1548`) and a handful of error-path lines inside
`_send_real_sell` (`688-689`, `706`, `724-729`, `745`, `839-840`, `861-868`,
`880`, `901-906`, `934`, `1044-1049`, `1060`, `1091-1092`) that look like
specific sub-branches within the already-tested classification ladder
(session77 part 2) rather than untested classes of behavior — needs
`--cov-report=annotate` to confirm which exact conditions they are before
writing more tests, same "confirm, don't guess" approach part 2 used.

Everything from Phase 1 #2 onward (`portfolio.py`, `manual_engine.py`,
`execution/dhan_client.py`'s classifier table, `auto_pilot.py` sub-targets,
`candidate_engine/candidates.py`) is unchanged — see the plan doc for the
suggested sequence.
