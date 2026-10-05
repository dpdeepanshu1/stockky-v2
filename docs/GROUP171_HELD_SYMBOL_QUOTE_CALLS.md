# Group 171 - fewer market-data calls for held symbols (bulk-first priority lane)

Cumulative on group 170. Item 3 of the open list ("`/live-quote` and `/quote` are both called for the same 5 held
symbols every ~10 s, and 224 symbols still go per-symbol after the bulk call"). Rebuild real-trade-service.
Only `real-trade-service/market_feed/feed.py` changed (plus tests).

## What was wrong
- The exit cycle prices every open position through `get_quotes(..., priority=True)`. Each held symbol cost a
  `GET /live-quote` and, when that was not fresh enough (limit `LIVE_QUOTE_MAX_AGE_S`, 5 s), a `GET /quote`:
  about 10 requests per cycle for 5 positions. Four callers do this independently (exit evaluation, two
  auto_pilot loops, the position-action route), so two of them firing close together repeated the same work.
- In the large non-priority batch, symbols that `POST /quotes/bulk` could not price fresh (224 in the log) went
  through the same two-call cascade: up to 448 more requests, although bulk had just read the same live feeds.
  The log only said how many were left, not why.

## The fix
- **Bulk-first priority lane:** one `POST /quotes/bulk` for all held symbols. A row is used only if it is at most
  `FEED_PRIORITY_BULK_MAX_AGE_S` old (default 10 s; the batch limit stays 20 s), with a `FEED_PRIORITY_BULK_TIMEOUT_S`
  (default 4 s) timeout. Only symbols bulk did not price go on to the old `/live-quote` + `/quote` cascade, and
  the last-resort scaled bulk call after that is unchanged. Normal case: 1 request instead of ~10.
- **Cool-down:** if the bulk-first call fails outright (every chunk errored or answered non-200, nothing priced),
  bulk-first is skipped for `FEED_PRIORITY_BULK_COOLDOWN_S` (default 30 s) so a struggling market-data is not
  asked twice per cycle. One WARNING when that happens. A partial answer does not start it.
- **Shared ticks:** a held symbol priced by a priority call is reused for `FEED_PRIORITY_SHARE_S` (default 3 s)
  by the other priority callers, whatever the spelling (`X`, `x.ns`, `X.NS`). The Tick keeps its real `as_of`,
  so staleness checks downstream still see its true age. Symbols nobody could price are not stored.
- **Leftovers skip `/live-quote`:** in a large non-priority batch, symbols bulk answered-but-rejected or missed go
  straight to `/quote` (`get_quote(..., skip_live_quote=True)`). Skipped only when bulk itself worked; if every
  bulk chunk failed, the full cascade runs as before.
- **Log:** `get_quotes: bulk-first priced N/M symbol(s); K left for per-symbol lookups (not in the answer a, older
  than limit b, no price c, no timestamp d, X of Y chunk(s) failed)`. After the next open this says why 224
  symbols were left.

## Switches (all env, read at start)
`FEED_PRIORITY_BULK_FIRST=0` old order for open positions. `FEED_PRIORITY_SHARE_S=0` no sharing.
`FEED_LEFTOVER_SKIP_LIVE=0` leftovers use the full cascade. Also `FEED_PRIORITY_BULK_MAX_AGE_S`,
`FEED_PRIORITY_BULK_TIMEOUT_S`, `FEED_PRIORITY_BULK_COOLDOWN_S`.

## Things to check
- A held symbol's exit price can now come from a bulk row up to 10 s old (or a shared tick up to 3 s old) where
  `/live-quote` required 5 s. `/quote`, the old second step, had no age check at all. If you want the 5 s rule,
  set `FEED_PRIORITY_BULK_MAX_AGE_S=5` and `FEED_PRIORITY_SHARE_S=0`.
- This does not explain why bulk leaves 224 symbols unpriced; the new log line will. Paste it after the next open.
- Not touched: `real-trade-service/main.py:1597` and `portfolio.py:529` still use the non-priority lane.

## Tests
New `tests/test_group171_held_quote_calls.py` (31 tests, all upstream faked). `tests/test_feed_priority_bulk.py`
fixture now turns bulk-first and sharing off, so its existing tests keep pinning the per-symbol cascade.
Not run with real pytest/httpx (sandbox has neither): the 31 new tests passed under a stand-in runner with an httpx
stub; `test_feed_priority_bulk.py` (real HTTP server) was not run.
