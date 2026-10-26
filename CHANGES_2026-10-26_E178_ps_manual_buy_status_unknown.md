# E178 - position-stocks: the manual BUY had the same "freed its symbol while the BUY may be live" gap as the auto BUY (E177)

Includes E177.

## What I did
Read `position-stocks-service/orders/entry.py` `attempt_manual_entry` (from line 1200) line by line: the gate order, the symbol claim,
the capital reservation and release on every raise, the exact-quantity and auto-size paths, the broker call, and the position write.

## Finding (fixed)
When the manual BUY call errored with a timeout or dropped connection and Dhan's order book could not be read either, the manual path
released the capital AND the shared symbol claim and raised "Dhan rejected the manual BUY". The BUY could still be live at the broker
with no local position. Worse than the auto path: `shared_symbol_lock.try_claim()` treats a claim already held by this service as "no
conflict", and a manual BUY has no re-entry cooldown, so the admin could click again and send a second BUY immediately.

## Change
- `attempt_manual_entry` passes the E177 `unknown` list to `_recover_ambiguous_buy`. On the unknown state: capital is returned, the symbol claim
  is KEPT, the symbol rests for 480 minutes (`_UNKNOWN_BUY_COOLDOWN_MIN`, reason `BUY_STATUS_UNKNOWN:...`), a critical log and a notification tell
  the operator to check Dhan, the skip row is still `MANUAL_ORDER_FAILED:...`, and the admin gets a 409 saying the order may be LIVE.
- A manual BUY is refused up front while a symbol is resting for that reason (checked before the claim, so nothing to release). Only the
  `BUY_STATUS_UNKNOWN` rest blocks; an ordinary failed-placement rest does not, so retrying after a plain rejection works as before.
- No new environment variable. All other manual failure paths are unchanged.

## Read and judged fine
Every raise after the claim releases it (and the capital once reserved); the exact-quantity reserve, the auto-size reserve and the 1-share top-up
return the right amounts; the shared order budget is checked right before the call; daily counters are committed only after a successful or
recovered BUY; the existing-position check blocks averaging in.

## Not changed / your call
- Same as E177: no write-ahead PENDING position row before the broker call; the unknown-BUY rest lives in process memory (a restart clears
  both the rest and, because it is in memory, the new manual block - the shared symbol claim in the database stays).
- The cooldown key is the upper-cased symbol on the manual path. The auto path uses the candidate's symbol as the screener gives it.

## Tests
`tests/test_entry.py` (class `TestManualEntry`): 4 new tests - lookup fails (claim kept, rested for hours, alert, no position, capital back);
a second manual BUY is refused and never reaches the broker; nothing at Dhan still releases the claim as before; an ordinary failed-placement
rest does not block a manual BUY. NOT RUN in my sandbox (no SQLAlchemy, no network); all files compile.
Run: `bash scripts/docker_test.sh position-stocks-service`
