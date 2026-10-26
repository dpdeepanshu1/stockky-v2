# E180 - position-stocks live short: a SELL that errored while Dhan's order book was unreadable freed its symbol for a second SELL

Includes E179.

## What I did
Read `position-stocks-service/orders/short_live.py` `_attempt` (lines 112-299), `_recover_ambiguous_sell` and `_handle_failure` line by line:
live guards, symbol claim, capital reserve and release (size-cap shrink, shortfall top-up), security id, shared order budget, the SELL Super Order,
the E114 plain-order fallback, the position write, and the alerts.

## Finding (fixed)
The same gap E177 closed for the long entry and E178 for the manual BUY. When the SELL Super Order call raised an ambiguous error (timeout, dropped
connection, 5xx) and `_recover_ambiguous_sell` could not read Dhan's order book either, it logged "treating as failed" and returned None. `_attempt`
then ran `_handle_failure` (a 5-minute symbol rest) and `_undo()` (capital AND symbol claim released). A live short can exist at the broker (the
Super Order carries its own legs) with no local position, and 5 minutes later the next cycle could short the same symbol again. The fallback was
correctly skipped (an ambiguous error never reaches a plain SELL); this gap was the cleanup after it.

## Change
- `_recover_ambiguous_sell(..., unknown=None)`: optional list; a True is appended only when the Dhan lookup itself failed. "Looked, nothing there"
  appends nothing. The return value is unchanged.
- `_attempt`: on that unknown state the capital is still returned, the symbol claim is KEPT, the symbol rests 480 minutes
  (`_UNKNOWN_SELL_COOLDOWN_MIN`, reason `SELL_STATUS_UNKNOWN:...`), the skip row is `SHORT_SELL_STATUS_UNKNOWN`, a critical log and a notification tell the
  operator to check Dhan and cover by hand if the short is there. No order counter is incremented. All other failure paths are unchanged.
- `orders/entry.py` (E178 manual-BUY guard): a manual BUY is now also refused while a symbol rests for `SELL_STATUS_UNKNOWN` (a live short may exist),
  not only `BUY_STATUS_UNKNOWN`. Ordinary failed-placement rests still do not block it.
- No new environment variable.

## Read and judged fine
Every early return before the order releases the capital and the claim (`_undo`); the size-cap shrink gives back the unused reservation and the
shortfall top-up is checked; the shared order budget is checked right before the call; the plain-order fallback never runs after an ambiguous error;
an unknown plain SELL keeps capital and lock and goes to `watch_orphan`; a failed position write after a placed SELL keeps both and alerts.

## Not changed / your call
- Same as E177: no write-ahead PENDING position row; the rest lives in process memory (a restart clears it, the database claim stays).
- On the unknown state the capital is returned (same rule as E177). If you would rather keep it reserved until the SELL resolves (as the plain-short
  unknown path does with `watch_orphan`), say so; that needs the watch list to accept a Super Order id.

## Tests (RUN)
`scripts/test_short_live.py` (run by `tests/test_short_live.py` in a clean interpreter): 6 new checks, section 7b. 73 of 73 pass here (was 67). The
"claim is NOT released" check FAILS on the E179 code and passes on this one. `tests/test_short_live.py` static rails checked by hand against the new source.
Run: `bash scripts/docker_test.sh position-stocks-service`
