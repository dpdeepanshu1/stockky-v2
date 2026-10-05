# Group 163 - exchange day-stats plausibility check (position-stocks-service)

Follow-up to group 162. The exchange day open/high/low/previous close are read from fixed byte offsets (91-122)
of the mode-2/3 frame, and those offsets have not been checked against a live frame. A wrong offset would put
garbage into the entry range gate, the adaptive levels, the screener multiplier and the entry guard's day-gain
check (`MAX_DAY_GAIN_PCT`).

## Change (feed/ws_client.py)
- `_day_stats_plausible(ds, ltp)`: a parsed tuple is accepted only if every present field is within 0.5x-2x of the
  last price, high >= low when both exist, and the last price sits inside [low, high] with 0.5% slack for
  tick/frame timing. No last price -> not rejected.
- `_accept_day_stats` stores a plausible tuple; an implausible one is dropped (a previously accepted value is kept),
  counted, and logged once per process: "exchange day stats for X look wrong ... using the tick buffer".
  Every consumer already falls back to the tick buffer when `get_day_stats` / `get_day_range` return None.
- `ws_status()` gains `day_stats_symbols`, `day_stats_accepted`, `day_stats_rejected`.

## How to verify live (first minutes of the session)
`GET /positionstocks/status` -> `ws.day_stats_accepted` should climb and `day_stats_rejected` stay near 0.
If rejected is high and accepted is ~0, the offsets are wrong: the service keeps working on the tick buffer, and the
warning line names the first bad sample. `get_day_stats("SBIN")` should return today's real open/high/low/prev close.

## Not changed
No trading rule, threshold or default changed. Entry window (09:30-14:30) still unchanged: needs your chosen times.
Tests: +9 in test_scalp_review_20261005.py, +1 in test_ws_client_loop.py. Rebuild position-stocks-service.
