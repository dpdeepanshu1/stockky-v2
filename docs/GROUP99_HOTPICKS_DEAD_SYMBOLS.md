# group99 (2026-10-04) - item 28: confirmed-delisted symbols (AAKASH, ANNAPURNA) in the Hot Picks universe return 404

Cumulative on group98. In `services/api-gateway` run `bash run_tests.sh` (or `python3 -m pytest tests -q`), then `docker compose build api-gateway && docker compose up -d`.

## Cause
`main.py::stockky_hot_stocks` builds its universe from movers, news, events, last-scan seeds, the watchlist, the searched list and recent IPOs. The scan universe passes every symbol through `_clean_equity_symbol` (which applies `KNOWN_DELISTED` and the non-equity rules), but this builder only de-duplicated. AAKASH and ANNAPURNA are in `KNOWN_DELISTED` (`symbol_aliases.py`, kept identical to market-data-service's list) and were still reaching the watchlist/searched sources, so every Hot Picks run fetched news, events and prices for them and got 404s from every upstream.

## Fix (`services/api-gateway/main.py`)
- New helper `_drop_dead_hot_symbols(seen)` removes, in place, any symbol that `symbol_aliases.is_known_delisted` or `is_non_equity_instrument` flags (rights/partly-paid/warrant/NCD suffixes and F&O contract symbols), and returns the dropped keys. It is applied right after the existing de-duplication, so `.NS`/`.BO`/lowercase spellings are covered.
- One INFO line per run lists what was dropped (`stockky-hot universe: dropped N delisted/non-equity symbol(s): ...`).
- It never renames a symbol, and if `symbol_aliases` cannot be imported it keeps everything (previous behaviour). If dropping leaves the universe empty, the existing nifty fallback still applies.

## Also fixed: a group93 test that could never have passed
`tests/test_main_core.py::test_symbol_alias_table_is_consistent_with_extra_new_symbols` required every `SYMBOL_ALIASES` target to be in `EXTRA_NEW_SYMBOLS` (recent renames). Group93 added the HEROMOTORS-style aliases that point at HEROMOTOCO, so it failed the first time the suite was run. `EXTRA_NEW_SYMBOLS` is used nowhere in production code. The test now also allows an explicit set of established targets (`HEROMOTOCO`); every other target must still be a recent rename. No production code changed for this.

## Not changed / not verified
- **Where AAKASH / ANNAPURNA entered:** the sources are the watchlist or searched list (or news/events). I filtered at the Hot Picks merge rather than purging the stored entries, so they can still sit in `stockky:searched_symbols` and the saved watchlist, and other consumers of those lists are unchanged. Group93's note about unclean `searched_symbols` still applies.
- **Other 404 sources:** only the Hot Picks universe was changed. A symbol that is delisted but not yet in `KNOWN_DELISTED` still 404s until it is added (or learned through the existing failure-streak logic).
- **Not seen live:** I have not seen your VM's log, so I cannot confirm the 404s came only from this path.

## Tests
`tests/test_main_hot_stocks.py::TestHotUniverseDropsDeadSymbols` (7): known-delisted symbols never reach evaluation, dotted/lowercase spellings, non-equity instruments, one log line with the names, dead-only universe falls to the nifty fallback, the helper drops in place, the helper keeps everything when `symbol_aliases` cannot be imported. Run here: `test_main_hot_stocks.py` 177 passed; full `api-gateway` suite 8090 passed.
