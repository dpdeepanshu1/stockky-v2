# E177 - position-stocks: a BUY that errored while Dhan's order book was unreadable freed its symbol for a second BUY

Includes E176.

## What I did
Read `position-stocks-service/orders/entry.py` `attempt_entry` (lines 448-1148) line by line: gate order, the claim / capital
reservation / release on every early return, sizing reductions, the broker call, and the position write.

## Finding (fixed)
After the BUY call raises an ambiguous error (timeout, dropped connection, gateway 5xx), `_recover_ambiguous_buy` asks Dhan whether a young
matching BUY exists. If that lookup ALSO fails (the likely case in one network outage), it logged "treating as failed", and the caller
released the capital and the shared symbol claim and rested the symbol 5 minutes (ENTRY_ORDER_FAILED_COOLDOWN_MINUTES). The BUY could be live
(a super order carries its own legs at the broker), with no local position, and after 5 minutes the next cycle could buy the symbol again.
real-trade-service already treats this case as UNKNOWN (A1b); position-stocks did not.

## Change
- `_recover_ambiguous_buy(..., unknown=None)`: optional list; a True is appended only when the Dhan lookup itself failed. "Looked, nothing there"
  and clean rejections append nothing. The return value is unchanged, so existing callers and tests are unaffected.
- `attempt_entry`: on that unknown state the symbol stays claimed (shared lock NOT released) and rests for 480 minutes
  (`_UNKNOWN_BUY_COOLDOWN_MIN`, no new env variable); a critical log and a notification tell the operator to check Dhan.
  Capital is still returned and the skip row is still `ORDER_FAILED:...`. All other failure paths are unchanged.

## Read and judged fine
Every early return after the claim releases it (and the capital once reserved); sizing reductions release the difference; the shared order
budget is checked right before the call; gate/counter commit happens only after a successful or recovered BUY.

## Not changed / your call
- No write-ahead record: the ScalpPosition row is written after the broker call (and after a separate commit of the daily counters). A crash or DB
  failure in that window leaves a live BUY with no local row. Super-order legs protect it at the broker; reconcile would need to adopt it.
  Closing that window means writing a PENDING row before the call, which is a larger change. Say if you want it.
- The gates between `reserve_capital` and the broker call are not wrapped; if one ever raised, the reservation and claim would leak. I did not
  check every gate for fail-open behaviour (see the unread list in the chat reply).
- The unknown-BUY rest lives in process memory (same as real-trade), so a restart clears it.

## Tests
`tests/test_entry.py`: 2 new flow tests (lookup fails -> claim kept, rested for hours, alert sent; nothing at Dhan -> claim released as before).
`tests/test_group313_money_path_fixes.py`: 1 new unit test for the flag. NOT RUN in my sandbox (no SQLAlchemy, no network); all files compile.
Run: `bash scripts/docker_test.sh position-stocks-service`
