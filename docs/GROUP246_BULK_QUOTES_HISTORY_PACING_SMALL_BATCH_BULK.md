# Group 246 - the 3-minute candidate/entry cycle no longer starves held positions of market-data (Priority 1 of the 2026-10-08 log review)

From the 2026-10-08 open log (boot about 09:46 IST):
- The 6 REAL positions hit `bulk chunk of 6 failed: ReadTimeout` three times and were priced only through the 12-call
  per-symbol fallback. Each time market-data was busy with the candidate cycle's fan-out.
- From call patterns in the log (code not the source of these numbers, they are counts of log lines):
  - volume-shock pre-check: ~80 separate `GET /quote/<SYM>` calls, although `POST /quotes/bulk` had just answered for the
    same symbols;
  - standard candidate track: 7 `GET /history` per candidate at once, 15 candidates at once (about 100 in flight);
  - entry candidates (<= 20 symbols): `/live-quote` + `/quote` per symbol (~40 calls) because a batch of 25 or fewer symbols
    skipped the bulk path.

## real-trade-service

### candidate_engine/candidates.py
- `_prefetch_quotes_bulk` now RETURNS the usable quote rows of the bulk answers (`{SYMBOL: quote}`; rows without a symbol or
  a price above zero are left out). It still never raises and still warms market-data's cache.
- `_volume_shock_analysis(client, symbol, quote=None)` and `_multi_tf_analysis(client, symbol, quote=None)` use a quote handed
  in by the caller and skip `GET /quote/<SYM>`. A symbol the bulk answer did not price still goes through `_fetch_quote`, as
  before. Both cycles hand over the prefetched quote; the volume-shock cycle logs one INFO line
  (`volume_shock priced N of M symbol(s) from the bulk answer; K fall back to GET /quote`).
  `CANDIDATE_USE_BULK_QUOTES=0` restores the per-symbol calls.
- `/history` pacing: at most `CANDIDATE_HISTORY_MAX_INFLIGHT` (default 6) candidate `/history` calls are in flight at once
  from this service; others wait their turn. The 42 s request timeout starts after the wait, so a queued call is not
  timed out early. 0 = off (old behaviour). One gate per event loop, bounded.

### market_feed/feed.py
- Non-priority batches of `FEED_SMALL_BATCH_BULK_MIN_SYMBOLS` (default 5) or more symbols are priced with `POST /quotes/bulk`
  first, like the large ones, so a cycle's <= 20 entry candidates cost 1 call instead of ~40. Leftovers (not answered, older
  than `FEED_BULK_MAX_AGE_S`) still go through the per-symbol path. Batches of 4 or fewer keep the per-symbol path.
- The large-batch rules are unchanged: the priority-lane distress back-off still applies only above `FEED_BULK_MIN_SYMBOLS`
  (25), so a small batch is never starved. Set `FEED_SMALL_BATCH_BULK_MIN_SYMBOLS` above `FEED_BULK_MIN_SYMBOLS` to turn
  this off.

## Not changed / limits
- No change to what is judged or rejected, thresholds, order placement, or the exit/priority path.
- The candidate engine's yfinance-bound history calls (60m, weekly, monthly) are paced, not removed. Building the weekly and
  monthly series from cached daily candles (Priority 4 in the review) is a separate change.
- Whether the cycles should run at all while BUYs are blocked (Priority 2) is a trading-behaviour decision and is not touched.
- The gate counts only this service's candidate calls; api-gateway / position-stocks `/history` callers are not paced here.
- These are fixes from reading call patterns in a log. They are not confirmed live: after the next open, check that
  `bulk chunk of 6 failed` no longer appears and that the new `priced N of M ... from the bulk answer` line shows N close to M.

## Tests
New `tests/test_group246_bulk_first_small_batches_and_history_pacing.py` (17): bulk prefetch returns usable rows / empty on
HTTP error, exception, bad JSON; lookup and switch; analyses skip `GET /quote` when given a quote; history in-flight cap, off
switch, per-loop bounded gate, no-loop case; both tracks hand over the bulk quote and fall back without one; small batches
bulk-first at the limit, per-symbol below it, switchable off. Two older tests that pinned the old small-batch path
(`test_group225...::test_small_entry_batches_are_never_starved...`, `test_feed_priority_bulk::test_small_batch_keeps...`)
were updated to the new behaviour (their intent, small batches never starved, is still asserted). Real pytest, full
real-trade-service suite: 3652 passed, 1 skipped; the one `test_group172` teardown error and the same 15 + 31 uncovered
lines in candidates.py / feed.py are also on the unmodified upload in this sandbox, no new uncovered line from this change.
