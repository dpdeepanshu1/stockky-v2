# Group 242 - the Surprise page stream prices its universe with bulk quotes, not one /quote per symbol

From the 2026-10-08 pre-open boot log (about 08:55-08:57 IST, market closed / pre-open):
- Each time the Surprise page was opened (`GET /api/surprise/scan/stream?force_reload=false`, three times in the log)
  market-data-service then logged hundreds of `GET /quote/<SYMBOL>` calls from the gateway in a row, one per symbol,
  each answered from the bhavcopy close. Only ONE `POST /quotes/bulk` (22 symbols, Hot Picks) appears in the whole log.
- Cause: group 229 put the bulk prefetch into `SurpriseStockEngine.scan()`, but the NDJSON stream route in
  `api-gateway/main.py` has its own loop (chunks of 20, `_fetch_quote` per symbol over the whole liquid static
  universe) and never called the prefetch. Pre-open nobody can use those prices, and every page reload repeats it.

## api-gateway (surprise_scanner.py, main.py)
- New `SurpriseStockEngine.prime_bulk_ticks(client, market_data_url, symbols)`: one chunked `POST /quotes/bulk` pass
  (same `SURPRISE_BULK_*` env as group 229) into a fresh dict, then swapped into `self._bulk_ticks`. Ticks left from an
  older prefetch for these symbols are dropped first (they would be served as fresh otherwise); ticks for other
  symbols (a running `scan()`) are left alone. Never raises; returns the number priced.
- `_prefetch_bulk` gets an optional `store=` dict (default unchanged: `self._bulk_ticks`), so the stream does not race
  `scan()`, which resets `_bulk_ticks` when it starts.
- `api_surprise_scan_stream` calls `prime_bulk_ticks` once for the chosen keys before its chunk loop. The existing
  per-symbol `_fetch_quote` calls then find the tick in hand; only symbols bulk could not price still go out one by
  one. A failing prefetch is logged and the stream carries on symbol by symbol (old behaviour). An engine without the
  method is tolerated. Output lines and event names are unchanged.
- `SURPRISE_BULK_PREFETCH=0` switches it off for both `scan()` and the stream.

## Not changed (from the same log)
- SMCG04 (AngelOne cannot resolve it): group 241 already explained it as the ipoalerts IPO fallback symbol; still
  unfiltered on purpose. Check the IPO tracker tab.
- HF_MODEL still rejected by Hugging Face (your env change), Yahoo news 0 (auto-skipped 600 s), gateway NSE bootstrap
  403 (bhavcopy fallback covers it), group 241 feed fallbacks not yet seen in a log.
- The Hot Picks table / watchlist panels still quote ~10-40 symbols one by one on page open; small, not touched.

## Tests
New `tests/test_group242_surprise_stream_bulk_prime.py` (11): store-only prefetch, no per-symbol GET after priming, 1000
symbols -> 10 chunk calls, stale tick dropped, other symbols' ticks kept, failures swallowed, switch-off.
`tests/test_main_surprise_routes.py` +5 (primed once before any quote, requested order, empty universe not primed,
prime failure falls back, engine without the method). The 11 engine tests were run through a stand-in runner (no
pytest/httpx in the sandbox); the 5 route tests need fastapi and were NOT run. Run `bash run_tests.sh` on the VM.
