# group88 (2026-10-04) - item 4: peer fundamentals fetched as INFY and INFY.NS

Cumulative on group87. Run `bash run_tests.sh` in analysis-intelligence-service on the VM, then `docker compose build analysis-intelligence-service && docker compose up -d`.

## Cause
`fundamental/peer_multi_quarter.py` cached peer fundamentals under the exact string the caller passed ("INFY" or "INFY.NS") and put the same string in the market-data URL. Two spellings = two cache entries = two `/fundamentals/...` requests for the same company.

## Fix (that file only)
- `_fund_cache_get` / `_fund_cache_set` use the canonical key (`INFY` -> `INFY.NS`; `.BO` stays `.BO`).
- `fetch_fundamentals` requests the canonical symbol.
- Per-symbol in-flight lock: simultaneous callers for one symbol make one request.
- `fetch_fundamentals_batch`: a list with both spellings is fetched once; both spellings are returned in the result dict.
- Env var unchanged: `PEER_FUNDAMENTALS_CACHE_TTL_SECONDS` (default 60).

## Not changed
- `fundamental/main.py::analyze()` and `decision-prediction-service` `_fetch_fundamentals` still use the bare symbol. market-data normalises its own cache key (`fundamentals:INFY.NS`), so this is an extra HTTP round-trip, not an extra Yahoo call. Left alone because existing tests assert on those URLs and calls.
- Item 10 (wrong peer sets, e.g. HEROMOTORS -> FMCG peers) is a separate fix.

## Check after deploy
In the market-data log, one `/fundamentals/<SYMBOL>.NS` per peer per analysis, with no matching bare-symbol request from the peer step.
