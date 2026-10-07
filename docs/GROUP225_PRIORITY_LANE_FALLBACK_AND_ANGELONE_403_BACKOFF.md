# Group 225 - open positions keep a price under load, and the AngelOne feed backs off after a 403

Cumulative on group 224. Items 1 and 3 of the 2026-10-07 open-market log review (item 2 is the load that causes 1).
Rebuild: `docker compose build real-trade-service market-data-service && docker compose up -d`.
Files changed:
- `real-trade-service/market_feed/feed.py` (last-good fallback + back-pressure for the priority lane)
- `market-data-service/angelone_client.py` (rate-limit classifier reads the raw body)
- `market-data-service/angelone_ws_feed.py` (stop the cycle when the shared cooldown starts; 403/429 safety net)
- tests: `real-trade-service/tests/test_group225_priority_lane_backpressure.py` (new, 13),
  `market-data-service/tests/test_group225_plain_text_403.py` (new, 10), plus two test-hygiene fixes (below).

## What the log showed
- 09:43: the six REAL positions (ADANIGREEN, GODREJCP, SCI, IOC, CIEINDIA, COHANCE) hit ReadTimeout on `/quotes/bulk`, `/live-quote`
  and `/quote` every 8 s exit cycle although market-data answered 200 (late). The same process was also sending ~579 per-symbol
  watchlist lookups, so market-data's workers were busy when the position calls arrived. FAST EXIT then ran on "No current price".
- `AngelOne quote batch (x-y) failed` repeated every ~3 s: the feed kept sending into the rate limit, which kept it tripped for everyone.

## Change 1 - real-trade-service priority lane (`market_feed/feed.py`)
- Last-good fallback: when bulk-first, the per-symbol cascade and the scaled bulk retry all fail for a held symbol, the lane returns
  the last tick it priced for it if that tick is at most `FEED_PRIORITY_STALE_FALLBACK_S` old (default 90, 0 = off). The Tick keeps its
  REAL `as_of`; `source` becomes `stale_last_good(<original>)`; it is never shared through the 3 s share cache and never remembered
  as a new "good" tick. A WARNING lists the symbols served this way.
- Back-pressure: a priority-lane failure switches non-priority per-symbol leftovers off for `FEED_BACKPRESSURE_S` (default 20, 0 = off),
  and one non-priority batch sends at most `FEED_LEFTOVER_MAX` (default 120, 0 = no cap) per-symbol lookups. Symbols left out wait
  for the next cycle (bulk already priced what it could).

## Change 2 - market-data-service AngelOne backoff
- `_is_rate_limit_response(status, body, text=None)` also checks the raw response text. A rate-limit 403 whose body is not JSON gave
  `body=None`, so the old check said "not a rate limit": no endpoint cooldown, no group211 global cooldown, `raise_for_status()` raised,
  and the feed logged "batch failed" and carried on. All four call sites (quote, candles, gainers/losers, batch) now pass `r.text`.
- The poll cycle stops walking the remaining batches as soon as the shared cooldown has started mid-cycle (one INFO line).
- Safety net: if a batch still raises an HTTP 403/429 whatever its text, the feed trips the shared cooldown itself (`_trip_on_http_denied`).

## Not changed / limits
- Fallback ticks are up to 90 s old. Nothing in the exit path checks tick age, so a stop/target is judged on that price instead
  of being skipped; lower `FEED_PRIORITY_STALE_FALLBACK_S` if you prefer skipping. A symbol never priced in this process has no fallback.
- That the real body is plain text is inferred from behaviour (the batch raised, so the classifier said "no"). The first
  `AngelOne ... returned HTTP 403 - body: ...` WARNING after deploy shows the true text; the safety net covers any wording.
- The load itself (items 2, 6, 7: 655-symbol watchlist poll, movers sweep, 74 s feed poll) is not reduced beyond the leftover cap.
- A 403 that is NOT a rate limit (bad session / IP) now also pauses AngelOne for 30-60 s instead of retrying every 3 s.

## Test hygiene (pre-existing, found while running the suites)
- `test_group171_held_quote_calls.py` failed 4 tests whenever it ran after `test_feed_priority_bulk.py`: that file reloads
  `market_feed.feed`, replacing the `Tick` class the group171 file had imported. It now builds ticks through `f.Tick`.
- `test_feed_fanout_controls.py` sets `FEED_LEFTOVER_MAX=0` so it keeps pinning the fan-out cap, not the new per-batch cap.
- One existing teardown error remains in `test_group172_volume_shock_history_reasons.py` (identical on the group 224 upload).
