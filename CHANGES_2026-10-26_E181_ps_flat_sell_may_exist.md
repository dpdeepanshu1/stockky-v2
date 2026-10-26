# E181 - position-stocks: a flat SELL that timed out could be sent a second time (duplicate exit SELL)

Includes E180.

## What I did
Read `position-stocks-service/orders/eod_squareoff.py` `_fire_flat_sell` (the exit SELL used by the EOD sweep, manual close and stagnation exit),
`_existing_flat_sell`, `_filled_bracket_exit_leg`, `orders/exit_retry.py`, and both callers' error handling, line by line.

## Finding (fixed)
The duplicate-SELL guard (group205) had two holes after a placement error that does not prove Dhan never received the order (timeout, dropped
connection, gateway 5xx), where the SELL may be live at Dhan:
1. Inside one call, the retry checks Dhan's order book first, but `_existing_flat_sell` FAILS OPEN: if the book could not be read (likely in the
   same outage) the retry sent a second SELL anyway.
2. Across calls it did not look at all. When all attempts of a call ended in a timeout, the position stayed OPEN (no order id recorded) and the
   next cycle / manual click ran a first attempt with no check, so an accepted-but-unrecorded SELL was followed by a second one. A duplicate exit
   SELL on a long can open an unintended short (the guard's own docstring says so).

## Change
- `_existing_flat_sell(db, pos, strict=False)`: with `strict`, an unreadable order book raises the new `FlatSellUnverified` instead of returning None.
  Rows created before the position opened are skipped (a same-size SELL from an earlier position today is not this position's SELL). A row with no
  readable createTime is kept, as before.
- `_fire_flat_sell`: remembers (process memory, `_AMBIGUOUS_EXIT_POS`) a position whose placement erred ambiguously. It looks at the order book before
  every retry (as before) AND before the first attempt of a later call for such a position; strict after an ambiguous error. An unreadable book then
  blocks the SELL: the failure is recorded (backoff + alert), the position stays OPEN, and the next call tries again. An accepted SELL found is adopted
  as before. The memory is cleared when a SELL is adopted or placed.
- A clean rejection (not ambiguous) behaves exactly as before, including fail-open on an unreadable book.
- No new environment variable. Both callers already treat any exception as "failed, position stays OPEN" and alert.

## Read and judged fine
Bracket-fill check before the SELL (fails open by design, a filled leg raises PositionAlreadyFlat); overnight-stop settle / cancel / re-settle around the
SELL sizing; a short never being sent as a CNC order; the plain-short stop released before the cover; permanent rejections failing fast and counted.

## Not changed / your call
- The memory is per process: after a restart the first attempt of a later call has no check (old behaviour) until another ambiguous error happens.
  A persistent flag needs a new column and migration; say if you want it.
- Blocking the SELL while the book is unreadable delays an exit during an outage (it was previously sent blind). At the 14:55 to 15:00 EOD sweep that is
  a deliberate choice of "no duplicate" over "send anyway": the next sweep or the manual close retries, and the alert tells you. The only alternative is
  fail-open, which is the old gap.

## Tests
`tests/test_eod_squareoff.py` (class `TestDuplicateSellGuards`): 6 new tests (timeout + unreadable book blocks; clean rejection still retries; a later call adopts
the accepted SELL on its first attempt; nothing at Dhan then sends and forgets; a later call with an unreadable book is blocked; an older same-size SELL is
not adopted). NOT RUN by pytest here (no SQLAlchemy / pytest in my sandbox); they compile. I ran the same 8 scenarios against the real `eod_squareoff.py`
with stubbed imports: 8 of 8 pass. Run: `bash scripts/docker_test.sh position-stocks-service`
