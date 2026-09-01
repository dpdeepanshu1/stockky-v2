# Fix: delisted/merged symbols entering the scan universe

## Root cause
`_build_scan_universe()` (services/api-gateway/main.py) merges symbols from
many sources: NSE's full securities list, index constituents, momentum
movers, bulk/block deals, 52-week extremes, news mentions, recent IPOs,
corporate events, your watchlist, and recently-searched symbols.

Only two of those sources — `_get_all_nse_securities()` and
`_get_nifty_indices()` — routed every symbol through `_clean_equity_symbol()`,
which is the function that checks `is_known_delisted()` (symbol_aliases.py)
and drops delisted/merged-away tickers.

The three sources that map most directly onto what you described — recent
news events, bulk-deal/institutional-buying flow, and volume/52-week
extremes — built their symbol lists with a bare uppercase+suffix normalizer
and **never called the delisted check at all**:
- `_get_bulk_deal_symbols()` (NSE bulk/block deals — institutional buying)
- `_get_52w_extreme_symbols()` (52-week highs/gainers)
- `_get_event_symbols()` (corporate events feed)
- `_get_momentum_movers()` steps 2–3 (gateway's own gainers/losers/most-active,
  and the yfinance-seed `.add()` calls)

So a delisted or merged-away symbol surfacing through any of those feeds
sailed straight into the tracked universe, got a slot, got scored every
cycle, and (per the Sync Context history) is also why the Repair button
can spin on a symbol forever: repair only purges a symbol immediately if
it's on the static `KNOWN_DELISTED` list — anything not yet on that list
just keeps getting a live-quote attempt that fails.

## Fix (services/api-gateway/main.py)
1. **Central gate** — `_build_scan_universe()`'s final merge/dedupe loop now
   runs every symbol, from every source, through `_clean_equity_symbol()`
   before it can enter `clean`. This is a single choke point, so any future
   new source is covered automatically without needing its own patch.
2. **Stale-cache gate** — the cached-universe fast path now re-applies
   `_filter_equities()` (not just the price cap) before returning, so a
   symbol added to `KNOWN_DELISTED` *after* the cache was written still gets
   dropped instead of surviving for the rest of the cache TTL.
3. **Defense in depth** — patched the four leaky sources' own `_add()`/`.add()`
   call sites to also clean through `_clean_equity_symbol()`, so ineligible
   symbols are dropped at the source instead of only at the final gate (saves
   the wasted scoring work, not just the wasted universe slot).

## What this does NOT fully solve
`is_known_delisted()` is still a **static, manually-curated list**
(symbol_aliases.py / market-data-service's mirror). This fix guarantees that
*any symbol already on that list* can never re-enter the universe again,
from any source. It does **not** give you live, automatic detection of a
stock that gets delisted today — that still surfaces the same way it did
before (a live-quote 404 in the logs), and still needs a one-line addition
to the list. Automating that fully would mean querying NSE's own live
delisted-securities list during universe build, which is a separate, bigger
piece of work — flag if you want that built next.

## Verified
`python3 -m py_compile` clean on services/api-gateway/main.py.
