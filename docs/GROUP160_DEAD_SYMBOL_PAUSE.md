# group160 (2026-10-05) - pause symbols that have no price instead of retrying them every cycle

Cumulative on group159. Rebuild: `docker compose build real-trade-service && docker compose up -d`.

Source: item 5 of the remaining list. Files changed: `real-trade-service/market_feed/feed.py` (+ one new test file).

## What the log showed
QUALIANCE, BMISL, BAGMANE, EMBASSY.NS, ANNAPURNA and AAKASH were retried every cycle. Each retry cost a `/live-quote` call and a
`/quote` call (and a miss in the bulk call before that) against the rate-limit budget that real symbols need.

## Cause
real-trade-service had no memory of a symbol that had no price. The Hot Picks and scan paths in api-gateway filter known-delisted
symbols (group99/118), but the watchlist and the candidate paths in this service just asked again each cycle.

## Fix (feed.py)
- A "miss" is a definite answer of no price from `/quote`: HTTP 404, or HTTP 200 with no usable price. Timeouts, connection errors
  and 5xx are NOT misses (upstream trouble says nothing about the symbol), and `/live-quote` misses are ignored (a symbol can be
  missing from the live table and still have a normal quote).
- After `FEED_DEAD_AFTER_MISSES` (default 3) misses in a row the symbol is paused: `get_quotes` leaves it out of non-priority batches
  (no bulk, no per-symbol call) for `FEED_DEAD_BACKOFF_S` (default 1800 s). Each further miss after the pause doubles it, up to
  `FEED_DEAD_BACKOFF_MAX_S` (default 21600 s = 6 h).
- One real price (from `/live-quote`, `/quote` or bulk) clears the count.
- The priority lane (open positions) NEVER skips: a held symbol that looks dead is still asked about every exit cycle.
- Keys are the canonical symbol from group159, so `X`, `X.NS` and `x.ns` share one count.
- One INFO line when a pause starts (and for the first few doublings): `get_quotes: SYM has had no price N time(s) in a row — paused
  for M min`. Skips themselves log at DEBUG only.
- `FEED_DEAD_SKIP=0` turns all of it off. The state is per process and resets on restart; `clear_dead_symbols()` empties it.

## Tests
New `tests/test_group160_dead_symbol_pause.py` (config defaults / bad env / off switch, not paused before the third miss and both
spellings share a count, a price clears the count, pause ends after the backoff, backoff doubles to the cap, helpers never raise,
404 / 200-no-price count, 5xx and timeout do not, priced quote clears, `get_quotes` leaves paused symbols out and makes no call when
all are paused, priority never skips, symbol retried after the pause, three cycles then no more requests).
No pytest in my sandbox, so the file is compiled but NOT run here; the helper functions were extracted from `feed.py` and checked
directly (pass). The `get_quotes` skip and the get_quote hooks are covered only by the new pytest file. The existing feed tests that
return 404 / no price use a single call per symbol, so they do not reach the 3-miss threshold. On the VM:
`python3 -m pytest tests/test_group160_dead_symbol_pause.py tests/test_feed_priority_bulk.py tests/test_feed_fanout_controls.py tests/test_feed_remaining_coverage.py tests/test_feed_atr_persistence_and_source1.py tests/test_feed_display_prices.py -q`

## Judgement calls to check
- 3 misses, 30 min and 6 h are my picks. A symbol that is briefly missing at the data provider (not delisted) is paused for at
  least 30 min after three bad cycles; it comes back by itself.
- A watchlist row whose symbol is paused simply gets no tick that cycle ("no price - skip, try again next cycle"), so it stays active
  until it expires. Retiring such rows is not part of this change.
- After a service restart the first three cycles still ask once each, because the state is not stored.
- If the log shows a real, liquid symbol being paused, check what market-data-service returned for `/quote/<sym>`; a wrong 404 there is
  the thing to fix.

## Not changed
Items 6-13 of the list and the unreachable site (VM side).
