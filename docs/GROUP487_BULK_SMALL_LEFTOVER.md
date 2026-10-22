# group487 - /quotes/bulk: small leftovers no longer go to yfinance

## Problem (2026-10-21 log)
Dhan priced almost the whole /quotes/bulk request, but the few symbols it could not price still went to `yf.download`.
That call took the 18 s hard timeout, the whole answer waited for it, and the timeout was then reported to the rate-limit
dashboard as a 502 event although nothing was rate limited.

## Change (services/market-data-service/main.py)
1. New gate in `_get_quotes_bulk_core`, after the Dhan / AngelOne / pre-open / cooldown stages and before `yf.download`.
   It applies when Dhan priced at least one symbol, at least `QUOTE_BULK_SMALL_LEFTOVER_MIN_PRICED_PCT` (default 90) % of the
   request is already priced, and at most `QUOTE_BULK_SMALL_LEFTOVER_MAX` (default 8) symbols are left.
   Each leftover then gets a cached price no older than `QUOTE_BULK_LEFTOVER_MAX_AGE_S` (default 180 s), tagged
   `stale_leftover(<source>)` with its real `fetched_at`; with none it is left out of the answer. No `yf.download`.
   Indices (`^...`) and held symbols keep the normal path.
2. A yfinance hard timeout or open circuit in /quotes/bulk is logged as a warning and no longer reported as a 502
   rate-limit event. A real 429 ("Too Many Requests") and other errors are reported as before.

## Switches
| Variable | Default | Meaning |
|---|---|---|
| `QUOTE_BULK_SMALL_LEFTOVER_SKIP` | 1 | `0` restores the old behaviour (leftovers go to yfinance) |
| `QUOTE_BULK_SMALL_LEFTOVER_MAX` | 8 | most leftover symbols the gate handles (0-200) |
| `QUOTE_BULK_SMALL_LEFTOVER_MIN_PRICED_PCT` | 90 | share of the request already priced (50-100) |
| `QUOTE_BULK_LEFTOVER_MAX_AGE_S` | 180 | oldest cached row that may be served (1-3600) |
All are blank-safe.

## Notes
- A symbol left out of a bulk answer can still be read one by one through `GET /quote` by the caller; that path is unchanged.
- Not part of this group: the scrip-master snapshot on a mounted volume and the staggered first download (next group).

## Tests
`services/market-data-service/tests/test_group487_bulk_small_leftover.py` (13). Whole market-data suite: 1726 passed.
