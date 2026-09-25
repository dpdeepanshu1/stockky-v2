# Session 112, Round 28 — five remaining 1-2 line source gaps (position-stocks-service)

Confirmed via a fresh pasted `pytest --cov` run (2266 passed, 99% overall,
32 statements missing total) that round 27 landed correctly — `eod_squareoff.py`'s
1044-1045 no longer appears — and pinned down what's actually left. This
round closes the five source-file gaps flagged for round-24 follow-up:
`eod_squareoff.py` 147-148, and the four 1-line gaps in `adaptive.py`,
`entry.py`, `reconcile.py`, `screening/engine.py`.

## What was covered

- **`orders/eod_squareoff.py` 147-148** (`_run_edis_precheck`): the
  `except Exception as _ne: logger.warning(...)` guarding
  `notifier.notify_critical(...)` in the `verified_today is False` branch.
  No existing test made `notify_critical` itself raise. New test in
  `tests/test_edis_precheck.py` monkeypatches it to throw and asserts the
  call is swallowed (`_run_edis_precheck` never raises, no entry lands in
  the `notifications` list) with only a warning logged.
- **`orders/adaptive.py` 113** (`_atr_proxy_pct`): `if len(sample) < 2:
  return None`. Unreachable under real config — `ATR_LOOKBACK` is 20 and
  the function already requires `len(prices) >= 4` before slicing, so
  `sample` can never end up shorter than 2 through real ticks. New test
  monkeypatches the module-level `ATR_LOOKBACK` constant down to 1 (it's
  read as a bare global at call time, same trick as monkeypatching
  `_WINDOW_CONVICTION_MULT` below) so `prices[-1:]` collapses the sample
  to one price despite 4 valid ticks in the buffer.
- **`orders/entry.py` 109** (`_reentry_guard_reject`): `if closed_at is
  None: return None` after `as_aware(last.closed_at)`. The query already
  filters `closed_at.isnot(None)`, so this only guards a corrupted value
  making it through `as_aware` unNone'd-back-to-None — not reachable via
  any real DB state. New test monkeypatches `entry.as_aware` to always
  return `None` and asserts the guard fails open (allows the entry)
  rather than raising on the subsequent arithmetic.
- **`orders/reconcile.py` 232** (`_backfill_legacy_eod_exit_order_ids`):
  `if not oid: continue` — a matched legacy SELL row whose `orderId`/
  `order_id` is blank. New test feeds a matching row via the existing
  `sell()` helper with an empty order id and asserts the position is
  left unadopted (`backfilled == 0`, `dhan_exit_order_id` stays `None`)
  instead of raising or silently storing `""`.
- **`screening/engine.py` 342** (`scan`): `if score <= 0: continue`.
  Unreachable under real config — every passing `pct` is `>= threshold >
  0` and every multiplier constant (`_RPOS_*`, `_VWAP_EXTENDED_MULT`,
  `_CONSISTENCY_*_MULT`, `_WINDOW_CONVICTION_MULT`) is positive, so their
  product can't go non-positive. New test in `TestScoreMultipliers`
  monkeypatches `_WINDOW_CONVICTION_MULT[5]` to `-1.0` (same isolate-one-
  multiplier convention the rest of that class already uses) and asserts
  the 5m window yields no candidate rather than one with a negative
  `composite_score`.

## Not done this round

- `config.py` 471-475 — still open, same `importlib.reload` risk noted
  since round 23.
- Everything else in the fresh coverage paste is inside `tests/*.py`
  themselves (`test_db.py`, `test_dhan_client.py`, `test_edis_precheck.py`
  97-98 — pre-existing, unrelated to this round's addition — `test_entry.py`,
  `test_eod_squareoff.py`, `test_main.py`, `test_oracle_compat.py`,
  `test_screening_support.py`, `test_scrip_master.py`, `test_tz_utils.py`,
  `test_ws_client*.py`), not source files — out of scope for this plan so
  far (every prior round targeted `--cov=<source package>`, never the test
  suite's own coverage). Flagging rather than silently skipping in case
  the plan should now extend to test-file self-coverage too.
- No `sqlalchemy`/`pytest`/`websockets`/`httpx` in this sandbox —
  `screening/engine.py` and `orders/adaptive.py` both fail to import here
  (`ws_client` needs `websockets`, `adaptive` needs `execution.dhan_client`
  which needs `httpx`), so those two new tests are verified only by
  `py_compile` plus a pure-Python hand-reimplementation of the exact
  arithmetic path (both attached inline above, and run for real in this
  sandbox against plain functions, not the actual module). The
  `eod_squareoff.py`/`entry.py`/`reconcile.py` tests were hand-traced
  against the real function bodies line-by-line instead, same as every
  round since 23. Re-run pytest on the real box to confirm all five.
- PB FinTech `legDetails` root-cause thread still untouched.
