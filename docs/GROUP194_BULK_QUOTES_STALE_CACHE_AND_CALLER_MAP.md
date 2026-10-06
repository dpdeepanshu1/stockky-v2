# Group 194 - /quotes/bulk stops serving stale cached rows (market-data-service)

Cumulative on group 193. Item 1 of the remaining list (market-data timeouts / AngelOne rate limits). Rebuild
market-data-service.

## Evidence (boot log 2026-10-06 06:38 UTC)
- real-trade: `get_quotes: bulk-first priced 542/725 symbol(s); 183 left for per-symbol lookups (... older than limit 183 ...)`
  followed by ~180 `GET /quote/<SYM>` from real-trade within a few seconds, then `get_quote(NTPC|PAYTM|ELEVATE|STEAMHOUSE|SGRL|
  ROSSTECH|KENNAMET): source-2 (market-data-service /quote) failed: ReadTimeout`, `dynamic_universe: /check trigger failed (ReadTimeout)`
  and `feed universe refresh: fetch failed ... ReadTimeout` on market-data.
- Cause: `POST /quotes/bulk` returned any cached `quote:<SYM>` row with its ORIGINAL `fetched_at`; the quote cache keeps rows far
  longer than real-trade's 20 s freshness limit (`FEED_BULK_MAX_AGE_S`), so those rows were rejected as "older than limit" and
  every one was re-priced through `GET /quote` (yfinance-backed, one worker thread each). That burst, on top of api-gateway's own
  `/quote` waves and the AngelOne rate-limit cooldowns, starved market-data-service.

## Fix
- `market-data-service/main.py`: cached rows older than `BULK_CACHE_MAX_AGE_SEC` (default 15; 0 = old behaviour) are no longer returned as
  fresh; they go through the live-feed hits and the AngelOne REST batch (50 tokens per call) like any miss. If every source fails the old
  row is still returned (original `fetched_at`, response gets `stale_served: N`), so nothing that used to answer now goes silent and the
  caller's own age check still applies. Rows with no parsable timestamp behave as before.
- Fail-open: any error in the new merge step returns the core answer unchanged.

## 172.18.0.7 is api-gateway
Docker assigns IPs in start order, so the number is not in the repo, but the log pins it: every `GET /angelone/movers` and
`GET /internal/yahoo-ws-status` on market-data comes from 172.18.0.7, and the only caller of those two routes in the code is
api-gateway (`main.py` ~1429, ~4470, ~10508). Its `AngelOne movers: +96 symbols` line follows each one. For the other addresses:
172.18.0.2 = real-trade-service (calls `/live-quote`, `/quotes/bulk`, api-gateway `/stockky-hot`), .4 = analysis-intelligence-service
(`/fundamentals/*`), .6 = position-stocks-service (polls real-trade `/status/REAL`), .1 = the host/nginx. Confirm on the VM:
`docker inspect -f '{{.Name}} {{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' $(docker ps -q)`

## Not changed
- api-gateway's per-symbol `GET /quote/<SYM>` waves (172.18.0.7, ~40 at a time at boot) - several call sites (`main.py` 2581, 4752, 7275, 10162,
  `ipo_scanner.py` 1266), none obviously the one; left alone rather than guess.
- The boot-time AngelOne 403s (`quote(batch)`, `getCandleData`) are already handled by the cooldowns; the partial `/angelone/movers` sweep (2484/2584)
  is cached 120 s by design. `angelone-ws-feed: a poll cycle over 489 symbols took 16.0s` is unchanged.
- STEAMHOUSE, SGRL, KENNAMET: not in any bhavcopy (404 on `/last-close`) - the "symbols that never price" item.

## Tests
`market-data-service/tests/test_group193_bulk_stale_cache.py` (9). Sandbox: market-data 908 passed (899 on the uploaded zip). Not live-tested.
Tuning: raise `BULK_CACHE_MAX_AGE_SEC` only if AngelOne REST rate-limits return; real-trade's limit is `FEED_BULK_MAX_AGE_S` = 20.
