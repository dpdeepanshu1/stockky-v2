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

---

## Follow-up (same day) — learned-delisted wiring + IPO calendar non-equity filter

Triggered by a real IPO-tracker prefeed log (1908 lines, 273 yfinance
"possibly delisted" errors, 138 distinct symbols) and a Database Feed
Health screenshot showing symbol PRIORITY stuck at 0/5 fields with Repair
returning 200 "success" and 0 improved.

### Finding 1 — PRIORITY is not a bug
PRIORITY = Priority Jewels IPO (mainboard, ₹200/share, subscription window
28-Aug to 1-Sep-2026). It has not listed yet as of this log, so there is no
price anywhere in the world for it — Repair correctly can't find one. This
resolves itself once NSE lists it (normally T+3 trading days after close).
No code change for this one; flagged to admin so Repair "succeeding but
improving nothing" isn't mistaken for a bug in every case.

### Finding 2 — the self-learning delisted system existed but wasn't wired in
symbol_aliases.py already has `is_learned_delisted()` / `record_resolution_failure()`
(a durable, KV-backed failure-streak tracker, MAX_FAILURE_STREAK=5) and
rate_limiter.py's yfinance monkeypatch already calls it on every yfinance
failure across every service. But `resolve_ns_ticker()` / `resolve_base_symbol()`
— the functions `_clean_equity_symbol()` (and therefore the whole
`_build_scan_universe()` gate from the earlier fix) and the repair-purge
short-circuit both call — only checked the tiny static `KNOWN_DELISTED`
dict, never `is_learned_delisted()`. So a symbol that crossed 5 consecutive
yfinance failures stopped burning yfinance calls (is_skippable() already
handled that) but kept its slot in the scan universe and kept "succeeding"
uselessly on every Repair click, forever — exactly the PRIORITY-shaped
symptom, just for genuinely dead tickers instead of a not-yet-listed one.

Fix — 2 files:
- `symbol_aliases.py`: `resolve_ns_ticker()` and `resolve_base_symbol()` now
  also check `is_learned_delisted()`.
- `main.py`: `repair_single_stock()`'s purge short-circuit now also purges
  on `is_learned_delisted()` (distinct message, lower confidence than a
  manually-confirmed KNOWN_DELISTED entry, since it's inferred from 5
  failures rather than a verified corporate action).

### Finding 3 — 7 of the 138 dead symbols were never equities
`1150VIES30`, `925ECL28`, `10MWL29`, etc. are NCD/bond-series tickers
(coupon+issuer+maturity-year coding), not stocks. NSE's `public-past-issues`
endpoint (ipo_scanner.py's calendar source) returns every public issue —
debt included — with no instrument-type field to filter on. Real NSE equity
tickers never start with a digit, so `_normalize_nse_row()` in
ipo_scanner.py now drops any symbol starting with a digit before it can
enter the IPO tracker's price-fetch pipeline.

The remaining 131/138 are genuine SME/small-cap equity tickers with no
data from any source — Finding 2's fix is what actually resolves those
over time (5 consecutive failures → learned-delisted → dropped from the
universe and purged on next Repair).

Verified: `python3 -m py_compile` clean on all 3 files (symbol_aliases.py,
main.py, ipo_scanner.py).
