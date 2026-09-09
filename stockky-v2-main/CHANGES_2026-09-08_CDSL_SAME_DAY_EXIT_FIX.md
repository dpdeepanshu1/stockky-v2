# 2026-09-08 — SELL rejected "Validate Qty from CDSL" on same-day exits

## Symptom
Real exits were being rejected by Dhan across unrelated symbols and exit
reasons:

    ⚠️ SELL rejected by Dhan — SUZLON ×11 (stop_hit)
    Dhan API error: Validate Qty from CDSL
    ⚠️ SELL rejected by Dhan — IONEXCHANG ×4 (target_hit_partial)
    Dhan API error: Validate Qty from CDSL
    ⚠️ SELL rejected by Dhan — PARADEEP ×7 (emergency_gap_down)
    Dhan API error: Validate Qty from CDSL

The position stayed open (exit_engine correctly leaves it untouched on a
failed SELL and retries next cycle), but every retry hit the same
rejection, so the position never actually got closed.

## Root cause
`exit_engine/exit.py`'s `_send_real_sell()` (the single function used by
automatic exits, manual sells, and EOD square-off — see
`manual_engine.py` and `execution/auto_pilot.py`, both of which call it)
always sent `product_type="CNC"` (the `dhan_client.place_order()`
default) for exit SELLs.

CNC is Dhan's "sell existing holdings" product — it validates the sell
quantity against what CDSL (the depository) actually shows credited to
the demat account. Per Dhan's own documentation, a share bought today is
only added to the demat account *one working day later*; until then it
is not a CDSL holding at all, so there is nothing for CDSL to validate
the quantity against. (This is also why Dhan normally requires a fresh
CDSL eDIS/TPIN one-time-password step to sell existing holdings — an
interactive step this fully automated service has no way to perform, and
one that in any case only ever covers shares CDSL already knows about.)

`stop_hit`, `target_hit_partial`, and `emergency_gap_down` are exactly
the exit reasons most likely to fire the same day as entry — a short-term
setup that moves fast enough to hit its stop, a partial target, or an
emergency gap check within hours of being bought. Every one of those
same-day exits was being sent as a CNC "sell my holdings" order for
shares that, from CDSL's point of view, didn't exist yet.

## Fix
`_send_real_sell()` now checks whether the position was opened on the
current IST trading day (`position.opened_at`, converted via
`tz_utils.as_aware()` / `tz_utils.ist_today_str()` — the same pattern
already used elsewhere in this service for date-based gating):

- **Opened today** → sell as `product_type="INTRADAY"`. This settles net
  against the day's own buy and never touches CDSL holdings validation,
  so it works regardless of T+1 settlement timing.
- **Opened on an earlier day** → sell as `product_type="CNC"` (unchanged
  behavior) — those shares have already settled and are real holdings.

The chosen product type is now also logged in the `TradeOrderEvent`
detail (`"... sent to Dhan (INTRADAY)"` / `"(CNC)"`) so a future rejection
is immediately diagnosable from the order history without guessing.

## Files changed
- `services/real-trade-service/exit_engine/exit.py`
  - `_send_real_sell()`: computes `same_day_position` and picks
    `sell_product_type` ("INTRADAY" vs "CNC") accordingly; passes it to
    `dhan_client.place_order()`; records it in the PLACED order event.
  - Added `ist_today_str` to the existing `tz_utils` import.

No changes needed in `manual_engine.py` or `execution/auto_pilot.py` —
both already route every real SELL through `_send_real_sell()`, so
manual exits and EOD square-off get the fix for free.

## Verification
Confirmed `tz_utils.ist_today_str(tz_utils.as_aware(dt))` correctly
returns the same IST calendar date for a position opened "just now" and
a different (earlier) date for one opened 2 days ago, using the same
naive/aware datetime shape `position.opened_at` actually comes back as
from the DB (see `tz_utils.py`'s own module docstring on why `as_aware()`
is required first). `exit_engine/exit.py` compiles cleanly.
