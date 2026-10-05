# group159 (2026-10-05) - one spelling per stock (KOTAKBANK / KOTAKBANK.NS, M&M / M%26M)

Cumulative on group158. Rebuild: `docker compose build real-trade-service && docker compose up -d`.

Source: item 4 of the remaining list. Files changed: `real-trade-service/market_feed/feed.py`,
`real-trade-service/watchlist_engine/watchlist.py` (+ one new test file).

## What the log showed
`KOTAKBANK` and `KOTAKBANK.NS` were fetched separately (two lookups for one stock), and `ARE&M` and `M%26M` were handled
inconsistently.

## Cause
- Tier 1/2 sources can send a suffixed symbol and Tier 3 a clean one. `refresh_watchlist` stored whatever it got (only upper-cased),
  so one stock became two watchlist rows, and `get_quotes` then made a `/live-quote` and a `/quote` call for each spelling.
- `_clean_sym` used `.replace(".NS", "")`, so it cut `.NS` out of the middle of a string and did not decode `%26`. A percent-encoded
  `M%26M` therefore never matched `M&M` in the ATR cache, the display cache or the bulk-result mapping.
- Per-symbol URLs put the symbol in the path unencoded.

## Fix
- `feed.py` `_clean_sym`: percent-decode, trim, upper-case, strip ONE trailing `.NS`/`.BO`. New `_path_sym` encodes a symbol once for a
  URL path (`M&M` -> `M%26M`, an already-encoded value is not double-encoded); `/live-quote`, `/quote` and `/history` URLs use it.
- `feed.py` `get_quotes`: collapses the requested symbols to distinct canonical ones, runs the existing lanes (priority, bulk-first,
  per-symbol, unchanged) on those, and returns the tick under every spelling that was asked for, so callers that look up
  `ticks.get(row.symbol)` keep working. Symbols that clean to an empty string are dropped (no lookup at all if nothing is left).
  The old body is now `_get_quotes_unique`.
- `watchlist.py` `refresh_watchlist`: the symbol is stored in its clean form, and the duplicate check and the group158 cooldown check
  match the clean, `.NS` and `.BO` spellings, because rows written before this change may still carry a suffix.

## Tests
New `tests/test_group159_symbol_canonical.py`: canonical form for 12 spellings, only a trailing suffix stripped, path encoding,
`get_quotes` fetches once and returns every spelling, priority flag passed through, blank symbols dropped, unknown symbol absent,
watchlist stores clean/decoded symbols, an active row in any spelling blocks a duplicate, a different catalyst type is still added,
the cooldown sees a legacy suffixed row.
No pytest/sqlalchemy in my sandbox, so the file is compiled but NOT run here. `_clean_sym`, `_path_sym` and the `get_quotes` wrapper
were extracted from the source and checked directly (pass). The existing feed tests use clean upper-case symbols except
`test_priority_lane_maps_bulk_result_back_to_requested_spelling` (`marine.NS`), which still gets its key back. On the VM:
`python3 -m pytest tests/test_group159_symbol_canonical.py tests/test_feed_priority_bulk.py tests/test_feed_fanout_controls.py tests/test_feed_remaining_coverage.py tests/test_feed_display_prices.py tests/test_watchlist_decay_and_watchlist.py tests/test_group158_watchlist_deep_drop_expiry.py -q`

## Judgement calls to check
- Per-symbol requests now go out with the clean symbol (no `.NS`), the same as the bulk path always did. If market-data-service ever
  needs the suffix for a particular symbol, it would show up as a symbol that prices in a bulk call but not here.
- Existing watchlist rows keep their stored spelling until they expire; only new rows are clean.
- I did not change other services' symbol handling; the same collapse is only done inside real-trade-service.

## Not changed
Items 5-13 of the list and the unreachable site (VM side).
