# Round 2 bug fixes — 2026-09-21

## Files changed

| File | Change |
|---|---|
| `services/real-trade-service/entry_engine/entry.py` | Fix `market_score` is-None check; fix `expire_stale_orders` post-cancel fill booking |
| `services/real-trade-service/tests/test_rt_entry_helpers.py` | Convert 2 xfail pinned tests to passing regression tests |
| `AUDIT_REPORT.md` | Updated with round-2 results |
| `production_fixes_round2.patch` | Diff of the 2 source files changed |

## Bugs fixed

### Bug 8 — `market_score = 0` treated as 50 (`entry_engine/entry.py:163`)
**Root cause:** `int(data.get("market_score") or 50)` — Python's `or` operator treats
`0` as falsy, so the worst possible market score silently becomes 50 (neutral).

**Fix:** Explicit is-None check:
```python
_raw_score = data.get("market_score")
score = int(_raw_score) if _raw_score is not None else 50
```

**Test:** `TestMarketRegime.test_a_reported_score_of_zero_is_returned_as_zero__BUG_FIXED`
(was: `..._CURRENT_BEHAVIOUR` asserting `== 50`; now asserts `== 0`)

### Bug 9 — Shares filled just before a stale-order cancel are never booked (`entry_engine/entry.py:expire_stale_orders`)
**Root cause:** `cycle_runner` runs `expire_stale_orders` before `reconcile_real_orders`.
After a successful Dhan cancel, the function immediately marked the order EXPIRED.
`reconcile_real_orders` only queries PLACED/PARTIAL orders, so it never saw the
now-EXPIRED order. Shares that filled between the last reconcile and the cancel
landed were permanently invisible: no position, no cash debit, no alert.

**Fix:** After a successful cancel, call `dhan_client.get_order_list()`, compute
`delta = filled_at_broker - already_booked`, and call `_book_fill_delta()` if
`delta > 0`. Wrapped in try/except — any failure logs a warning and lets reconcile
retry next cycle.

**Test:** `TestKnownGaps.test_shares_filled_just_before_the_cancel_should_still_be_booked`
(was strict xfail; now a regular passing test)

## Test counts
- real-trade-service: **376 passed, 1 xfailed** (was 375 + 2 xfailed)
- position-stocks-service: **1220 passed** (unchanged)
