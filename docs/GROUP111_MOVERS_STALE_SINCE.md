# group111 (2026-10-04) - Movers stale label: show when the last-known list was saved

Cumulative on group110. Rebuild: `docker compose build api-gateway frontend && docker compose up -d` (frontend: your usual `npm run build` / deploy).

## Why
group110 marked last-known rows stale but could not say how old they were, because the stored list had no timestamp. That was the one caveat left in group110.

## Change
**api-gateway/main.py**
- New key `MARKET_MOVERS_LAST_KNOWN_AT` (`stockky:market_movers_last_known_at`). Whenever a good yfinance fetch refreshes the last-known list, the save time is written beside it (`YYYY-MM-DD HH:MM`, gateway local time = IST, 7-day TTL). The write is wrapped so a failure never affects the fetch. The key starts with the last-known key, so `kv_cache`'s durable-prefix rule already covers it (no kv_cache change).
- `_mark_movers_stale()` adds `stale_since` to each stale row when the stamp is a non-blank string. Missing, blank, non-string or unreadable stamps are ignored; it never raises.
- `_movers_response()` adds `stale_since` next to `stale: true` when a stale row carries it.

**frontend**: `MarketResponse.stale_since?: string` (api.ts); the panel note reads "(saved 2026-09-29 15:30 IST)" when known, otherwise the group110 wording.

## Behaviour to know
- A list saved before this deploy has no stamp, so the old wording shows until the next good fetch writes one.
- The stamp reflects the last good fetch, not necessarily the row's market day. The label is the save time, nothing more.
- Fresh yfinance and AngelOne rows are unchanged and carry neither `stale` nor `stale_since`.

## Tests (`tests/test_main_scan_runner.py`)
New: stale rows and routes carry the save time; missing or malformed stamps (None, blank, whitespace, number, list) are left out; a failing stamp write does not break the fetch. 1 existing assertion updated: the fresh-fetch test now expects the extra stamp write (`2026-09-30 10:00` under the fixed clock).

## Run here
No pytest/fastapi in the sandbox, so the suite was NOT run. `main.py` and the test file compile; the helpers were checked in isolation with stubbed storage. The frontend was not type-checked or built (no node_modules). On the VM run `bash run_tests.sh` in api-gateway (expect 8133 passed: 8126 from group110, plus 7 new cases) and build the frontend.
