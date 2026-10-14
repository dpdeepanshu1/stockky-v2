# Group 366 - structure gate "room to resistance": raw value + breakout counter (diagnostic only)

Verdicts are UNCHANGED. Only the reason text and one counter are new.

## Why
`live_metrics` re-expresses the cached room at the live price: `room = cached_room - (live - snapshot_close) / ATR`.
When a stock breaks above the cached resistance intraday the result goes negative, is clamped to 0 in the text and
shows as "only 0.0 ATR of room to the next resistance" - indistinguishable from a genuinely blocked entry.

## What changed (real-trade-service `entry_engine/structure_gate.py`, position-stocks-service `orders/structure_gate.py`)
- When the RAW room is <= -0.05 ATR the room reason gets a suffix:
  `... (min 0.5) [raw -0.6: live price is 0.6 ATR beyond the cached resistance - possible breakout]`.
  A real "no room" (raw >= -0.05) keeps the exact old text.
- New counter `room_past_resistance` in the `GET /structure/status/{mode}` stats: rejections where the room check fired AND the
  raw room was negative. Compare it with `by_reason.room` to see how many "no room" blocks are really breakouts.
- `reason_key` still buckets the text as `room` (no change to existing counters or tests).

## Not changed
- The gate still rejects exactly the same candidates. The real fix (move to the NEXT resistance above the live price)
  needs the next supply level in the cached summary, which it does not carry - decide after reading the counter.
- `STRUCTURE_MIN_ROOM_ATR` untouched.

## Tests
`real-trade-service/tests/test_group366_room_raw_diagnostic.py` (5), `position-stocks-service/tests/test_group366_room_raw_diagnostic.py` (3).
