# E173 - line-by-line read of `holdings_sync_reconcile`, three ghost-close guards

Includes E172.

Read: `portfolio/portfolio.py` `holdings_sync_reconcile` (343 lines). It force-closes a REAL position when Dhan's holdings plus
positions do not list the symbol, refunds the cash, and books no P&L. The logic (guard minutes, pending-sell skip, partial cap,
net-sell detection, refund maths) is sound. Three inputs could make it close positions that are still held:

1. **Empty broker answer.** `get_holdings` and `get_positions` both return `[]` for "no data" (also when the payload is not a
   list). If both are empty while positions are open, every position older than the guard was closed in one pass. Now the empty
   answer must be seen again `HOLDINGS_SYNC_EMPTY_CONFIRM_SECONDS` (default 120, 0 = old behaviour) later; a non-empty answer is
   trusted at once and clears the mark. A really flat account still gets cleaned up, two minutes later.
2. **Quantity text like "10.0".** `int("10.0")` raises, the quantity read as 0, and the symbol counted as not held. Now
   `int(float(x))`.
3. **Symbol "ABC-EQ".** Compared with the bare "ABC" it never matched, so every such position looked like a ghost. Both sides are
   now compared without a trailing `-EQ` (the same rule as the oversell sync in E171).

New env var `HOLDINGS_SYNC_EMPTY_CONFIRM_SECONDS` (docs/ENV_REFERENCE.md regenerated, 1,493 variables).
Tests: `tests/test_e172_holdings_sync_guards.py` (4, all fail on the E172 code). `tests/conftest.py` turns the confirm wait off
for older tests, like the other time-dependent guards.
Run here: whole real-trade suite 5,330 passed, 2 skipped (oracle_compat file left out: no `oracledb` here); env reference,
merge-marker and route-auth checks pass.

## Noted, not changed
- Broker quantity per symbol is the larger of holdings and positions, not the sum. With one position per symbol (no pyramiding)
  this is right; a symbol held both settled and bought again today could under-read. Left as is.
