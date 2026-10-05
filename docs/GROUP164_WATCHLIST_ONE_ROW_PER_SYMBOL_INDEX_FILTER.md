# Group 164 — watchlist: one decision per symbol per cycle, index names kept out

Item 5 of the second open-market log list (real-trade-service), first part: duplicate rows per symbol, index-level
catalyst prices, and the PACEDIGITK QUEUED/SKIPPED flapping. The remaining item-5 points (penny rows that stay
active, the ~450 INFO lines per cycle) are not in this group.

## What was wrong
- **Several active rows for one stock.** `refresh_watchlist` de-duplicates per symbol + catalyst type, so a stock can
  carry a `results` row, a `bulk_block` row and a `volume_shock` row at once, each with its own catalyst price.
  `evaluate_watchlist_entries` judged every row on its own: in one cycle the Tier-1 row could be in band and be
  QUEUED while the Tier-3 row of the same stock was SKIPPED by the day-change guard (or the reverse next cycle) —
  the PACEDIGITK flapping — and each row cost a pass and a log line.
- **Index names as stocks.** A hot-pick/news item for NIFTY, BANKNIFTY, SENSEX ... carries the index LEVEL
  (e.g. 24,500) as its price. That became the row's `catalyst_price`, so the band check compared a share price with
  an index level.

## The fix
New `watchlist_engine/symbol_filter.py`, used by ingest and the trigger pass:
- **Index names are not inserted** (`refresh_watchlist`, one INFO line per refresh with the count). Matching is by
  normalised name (`NIFTY 50`, `NIFTY50.NS`, `^NSEI`, `SENSEX.BO`, `INDIA-VIX`, `BANKNIFTY`, `FINNIFTY`, `CNXIT` ...);
  ETFs on an index (NIFTYBEES) stay with the existing ETF guard. Rows already in the table are retired by the trigger
  pass (`status=expired`, reason `index: ...`) before any price lookup.
- **One row per symbol is evaluated per cycle.** Best source tier first, then the freshest catalyst, then the newest
  row; `.NS`/`.BO` spellings count as one stock. The other rows are left untouched (still `active`) and take over by
  themselves when the primary becomes missed / expired / entered, so no catalyst is lost.
- The tally gains `index_expired` and `duplicates` only when they are non-zero (existing tally keys unchanged).

## Settings (blank/invalid = on)
`WATCHLIST_INDEX_FILTER=0` and `WATCHLIST_ONE_ROW_PER_SYMBOL=0` turn the two rules off; `WATCHLIST_INDEX_SYMBOLS=A,B`
adds more index names.

## Behaviour change to be aware of
A stock is now judged against its BEST catalyst only. If that row is adverse (more than `WATCHLIST_MAX_DROP_PCT` below
its catalyst) the stock is not queued, even when a weaker Tier-3 row of the same stock would have been in band.
That is intended (it was the flapping), but it can mean fewer entries. `WATCHLIST_ONE_ROW_PER_SYMBOL=0` restores the old way.

## Tests
New `tests/test_group164_watchlist_one_row_per_symbol_index_filter.py` (49 tests); `symbol_filter.py` and
`watchlist.py` stay at 100%. Run:

    python3 -m pytest services/real-trade-service/tests/test_group164_watchlist_one_row_per_symbol_index_filter.py services/real-trade-service/tests/test_group155_watchlist_adverse_guard.py services/real-trade-service/tests/test_watchlist_trigger.py -q

Rebuild real-trade-service.
