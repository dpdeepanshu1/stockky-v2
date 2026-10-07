# Group 229 - surprise scan bulk prefetch

Problem (log review item 8): `SurpriseStockEngine.scan()` asked market-data-service for one `GET /quote/{sym}`
per liquid-universe symbol (about 1,000, 25 at a time) whenever the gateway's shared bulk cache was cold.
During the open session each call was shed from the AngelOne lane and fell to Yahoo.

Change (`services/api-gateway/surprise_scanner.py`):
- `_prefetch_bulk(client, market_data_url, symbols)` prices symbols with chunked `POST /quotes/bulk` into `self._bulk_ticks`.
- `scan()` resets `_bulk_ticks`, prefetches the key list, then the sector-sympathy peers.
- `_fetch_quote` order: gateway bulk cache -> per-scan prefetch -> per-symbol `/quote/{sym}`.
- `_row_to_tick` and `_bulk_row_fresh` are shared helpers; prefetched ticks are marked `_from_cache=False`.

Env: SURPRISE_BULK_PREFETCH (default on, 0 = off), SURPRISE_BULK_CHUNK (100), SURPRISE_BULK_TIMEOUT (15 s),
SURPRISE_BULK_CONCURRENCY (2), SURPRISE_BULK_MAX_AGE_SEC (30).

What to look for after deploy: log line `surprise scan: bulk prefetch priced N of M symbol(s)`; the
`AngelOne-first did not price X (lane budget shed this call)` count from 172.18.0.7 should fall sharply.
