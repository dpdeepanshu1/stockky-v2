# Group 248 - a timed-out /quotes/bulk chunk is retried as smaller bulk calls, not 100+ per-symbol calls (2026-10-08 10:16 IST log)

## What the log showed
With groups 246/247 live, the 709-symbol watchlist poll priced 558 symbols in bulk and 1 of 8 chunks failed with a
ReadTimeout. Its 100 symbols plus 51 others were "left for per-symbol lookups", capped at 120 (`FEED_LEFTOVER_MAX`). Those 120
`GET /quote` calls were mostly shed by AngelOne's lane budget ("higher-priority lanes need the bucket"), sent to the
saturated yfinance path, and about 15 of them ended in `get_quote(...): source-2 failed: ReadTimeout`.

## real-trade-service (market_feed/feed.py)
- `_bulk_ticks` records the symbols of every failed chunk in `stats["failed_symbols"]` and takes a `chunk_size` argument.
- `_get_quotes_unique`: after the bulk-first pass, if at least one chunk answered and some chunks failed, the lost symbols are
  asked again as bulk calls of `FEED_BULK_RETRY_CHUNK_SIZE` (default 25) with a `FEED_BULK_RETRY_TIMEOUT_S` (default 20s)
  timeout, same client and concurrency limit. Whatever is still unpriced goes to the per-symbol path as before.
- Not retried when nothing answered in the first pass (market-data looks down; a second bulk round would only add delay).
- The log line now reads "N symbol(s) of failed bulk chunk(s) asked again in smaller bulk calls: priced M".
- `FEED_BULK_RETRY_FAILED=0` restores the old behaviour.

## Limits
- A retry adds up to one extra timeout (20s) to a poll when market-data is slow. That is less than the per-symbol path it
  replaces, but it is not free.
- The ~51 symbols that bulk answered but did not price fresh (stale / no price) still use the per-symbol path; that is
  group 246's behaviour and is unchanged.
- Not confirmed live. After the next open, `get_quote(...) source-2 failed: ReadTimeout` lines should drop sharply when a
  bulk chunk fails.

## Tests
New `tests/test_group248_failed_bulk_chunk_retry.py` (6): failed-chunk symbols and `chunk_size` in `_bulk_ticks`; failed chunk
recovered by the smaller bulk call (no per-symbol calls); a retry that fails again leaves the symbols to the per-symbol path;
no retry when nothing answered; switch-off; no retry when nothing failed. Real pytest: full real-trade-service suite 3666
passed, 1 skipped; the one `test_group172` teardown error is also on the unmodified upload.
