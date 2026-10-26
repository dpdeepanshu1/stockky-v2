# E176 - a manual REAL BUY whose reply is lost could be recorded as REJECTED while live at Dhan

Includes E175.

## What I did
Read `manual_engine.py` `evaluate_manual_order` line by line (BUY and SELL paths).

## Finding (fixed)
The auto entry BUY (entry.py, A1b) sends a correlation tag with the order and, if the call raises after Dhan may have accepted it
(timeout, dropped connection), looks the order up by that tag before calling it REJECTED; if the order book cannot be read it
keeps the symbol locked and warns. The manual REAL BUY had none of this: any exception marked the order REJECTED and released the
symbol lock, though the order might be live. The operator then saw "rejected", could click again, and a second BUY would go out.
A live order with a REJECTED local row also has no `dhan_order_id`, so reconcile could not match its fill.

## Change (`manual_engine.py`, REAL BUY only)
- `place_order(..., tag=f"stk{order.id}")`, the same scheme as entry_engine.
- After an error that is not a clear broker rejection and happened after the call started: look the order up by tag.
  - found: booked as sent (`SENT_TO_BROKER`, `recovered: true`), no second order, logged as MANUAL_BUY_SENT.
  - order book unreadable: REJECTED locally, symbol lock KEPT, symbol flagged "unverified BUY today" (the flag entry.py already uses, so
    the auto path also leaves it alone), response has `uncertain: true` and says to check Dhan.
  - order book readable and order absent, or a clear broker rejection (RMS / EXCH / DH- / IP / circuit / CDSL): unchanged.
- A later manual BUY of a flagged symbol is refused with the same message the auto path uses.

## Read and judged fine
SELL path: pending-sell guard, qty capped to qty_open, LIMIT/MARKET passed through to `_send_real_sell` (which has its own lost-reply
handling, A1c). Preview and confirm share one computation. No change.

## Not changed / your call
The unverified-BUY flag lives in process memory (same as auto), so a restart clears it. Check Dhan after any "may be LIVE" message.

## Tests (`tests/test_manual_engine.py`)
5 new tests (class TestBuyConfirmRealLostReply) + 1 existing test now stubs `find_order_by_tag`. NOT RUN in my sandbox (no SQLAlchemy,
no network); both files compile. Run: `bash scripts/docker_test.sh real-trade-service`
