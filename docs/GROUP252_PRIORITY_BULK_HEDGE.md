# Group 252 - held-position prices no longer wait out a slow bulk call (2026-10-08 open-market log)

## What the log showed
`get_quotes: /quotes/bulk chunk of 6 failed: ReadTimeout` followed by `priority quotes: bulk-first failed for 6 symbol(s) - per-symbol path
now, bulk-first paused for 30s`, then the six `GET /live-quote` + `GET /quote` calls (GENUSPOWER, MPHASIS, AURIONPRO, UNITDSPR, JSWCEMENT,
ARTNIRMAN), all answered 200 at once. Market-data was busy (the AngelOne feed poll took 16.8 s) but its cached per-symbol endpoints still
answered quickly.

## Why not "retry in smaller bulk calls"
The priority lane already sends ONE chunk for the held symbols (6 here), so a smaller-chunk retry cannot help (group 248 does that for
the big watchlist poll). The real cost was the 4 s bulk timeout, after which the per-symbol path started: every held-position price,
and the 8 s exit cycle, lost 4 s.

## real-trade-service `market_feed/feed.py`
- `_priority_quotes`: the bulk call now runs as a task. If it has not answered after `FEED_PRIORITY_HEDGE_S` (default 1.5 s, 0 = old
  behaviour) the per-symbol cascade starts alongside it (`_priority_hedge`) and each symbol takes the first tick that arrives. It stops as soon
  as every symbol has a tick, or both sources finished; unfinished lookups are cancelled.
- A bulk call still unanswered (or failed with nothing) after a hedge counts as a failed bulk-first: same `FEED_PRIORITY_BULK_COOLDOWN_S`
  (30 s) pause and a new log line: `bulk-first had not answered N symbol(s) after 1.5s - per-symbol lookups started alongside it (X priced
  per-symbol, Y from bulk), bulk-first paused for 30s`.
- Symbols neither source priced skip the repeat per-symbol round and go straight to the existing last-resort bulk and the
  last-good fallback (group 225).
- A bulk answer that arrives inside the hedge delay behaves exactly as before; nothing changes while bulk-first is paused.

## Limits
- While market-data is slow, a hedge adds one per-symbol call per held symbol for about the first 1.5 s of a slow bulk, at most
  twice per 30 s (bulk-first is then paused). Held positions are few (6 in the log), so this is small.
- If one held symbol cannot be priced per-symbol, the lane still waits for the pending bulk (bounded by its own 4 s timeout), as before.
- Not confirmed live. After the next open: the `bulk-first failed ... per-symbol path now` lines should be replaced by the hedge line,
  and the exit-cycle price latency in those moments should drop from about 4 s to about 1.5 s.

## Tests
New `tests/test_group252_priority_bulk_hedge.py` (12): fast bulk never hedges; slow bulk is hedged and not waited for (bulk cancelled, pause set);
bulk just inside the delay; bulk arriving mid-hedge wins and sets no pause; per-symbol prices kept when bulk then fails; uncovered symbol waits
for bulk; no repeat per-symbol round; failure inside the delay as before; hedge off; bulk-first paused; results shared/remembered; blank-safe env.
Real pytest, real-trade-service: 3701 passed; the one `test_group172` teardown error is also on the unmodified upload.
