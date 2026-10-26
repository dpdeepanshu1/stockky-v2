# Service sheet - market-data-service (E170)

Measured read-only in the E170 sandbox with `python3 scripts/audit_service_sheet.py market-data-service` plus the repo scanners; nothing was run against a broker or a database. The ten checks are the same for every sheet (see `docs/SERVICE_SHEETS_INDEX.md`).

| # | Check | Result | Verdict |
| --- | --- | --- | --- |
| 1 | Inventory | 28 files, 16,948 lines, 696 functions; routes 23 GET + 3 mutating; 83 test files | note |
| 2 | Function size | 15 over 100 lines, 20 with branch count over 25. Longest: `main.py:3800 _get_quotes_bulk_core` (522 lines, cc 143); `main.py:3168 _get_quote_inner` (330 lines, cc 101); `main.py:5157 _get_fundamentals_compute` (293 lines, cc 85); `angelone_ws_feed.py:669 start_feed_background` (271 lines, cc 53) | open (F-04) |
| 3 | Silent exceptions | 381 broad `except Exception`; 86 do nothing (`pass`) | note |
| 4 | Zero as missing | 2 `int/float(x.get(k) or N)` sites | review |
| 5 | Frontend route match | Called by the gateway and other services, not the browser; E163 set the shipped provider order dhan,angelone,yfinance (live `.env` change still pending on the VM). | carried |
| 6 | Security | Three mutating routes, no login inside the service (internal port 127.0.0.1:8001): `POST /quotes/bulk` (a read sent as POST), `POST /internal/feed-coverage`, `POST /surprise/premarket` (starts a premarket run). None places an order. | note |
| 7 | Env settings | 204 names read; 32 have no mention outside the generated `docs/ENV_REFERENCE.md` | note |
| 8 | Money-path / critical functions | No order code. Price truth for every other service comes from here: `main.py` `_get_quotes_bulk_core` (522 lines, cc 143) and `_get_quote_inner` (330, cc 101) decide the provider order and the stale label. Two `or 0` price reads (`dhan_data/live_poller.py:72`, `live_store.py:186`) turn a missing price into 0.0 at the store edge; callers must treat 0 as no price. | note |
| 9 | Tests | 83 files, 1,526 test functions (not run in the sandbox) | VM run |
| 10 | Hygiene | stray files: 0; files over 1 MB: 0; same-name copies of shared/: 0 | pass |


Tests per 1,000 lines: 90.0. Silent handlers by folder: main.py 41, dhan_data 9, kv_cache.py 9, angelone_client.py 6, bhavcopy.py 6.

## Findings
| ID | Sev | Finding | Action |
| --- | --- | --- | --- |
| F-32 | S3 | 86 silent `except Exception: pass` handlers in 16,948 lines (5.1 per 1,000 lines), the densest in the repo with the gateway (5.0). In a quote path a swallowed error shows up as a stale or empty price with no log line. | Review `audit_functions.py --silent` rows for `main.py` first (41 of them); log one warning per site that guards a provider call. |

## Not done in this sheet
- Line-by-line read of `_get_quotes_bulk_core` and `_get_quote_inner`.
- Live compare of five prices per screen (VM, market hours).
- `pytest` for this area on Python 3.14 (no pytest / sqlalchemy in the sandbox): `./stockky_full_test.sh`.
