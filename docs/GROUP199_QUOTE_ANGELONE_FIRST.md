# Group 199 - GET /quote asks AngelOne REST first for symbols outside the live feed (market-data-service)

Cumulative on group 198. Item 1 of the remaining list (symbols that never price). Rebuild market-data-service.

## Evidence (group 198 report, 2026-10-06 08:36 UTC, market open)
- All five names have an NSE `-EQ` row in the AngelOne scrip master, so the SME-board hypothesis in group 198 is **wrong**: they are
  tradable EQ stocks and all of them price.
- First `/quote` answers: ELEVATE `angelone_ws` in 0.0 s; STEAMHOUSE `yahoo` 5.5 s; SGRL, KENNAMET and ROSSTECH `yahoo` 20.5 s each.
  The second call is instant only because the first filled the cache.
- ELEVATE is in the 489-symbol live feed (it is in the `yahoo_ws_feed` subscription list in the boot log); the other four are not.
  A name outside the feed went straight to Yahoo (`_yahoo_ohlcv_quote`, 1 month of daily bars), the slowest source.
- real-trade-service reads `/quote` with a short timeout and logs `get_quote(...): source-2 (market-data-service /quote) failed:
  ReadTimeout` (ARIS, TRIDENT, ABSLAMC in this boot). `/quotes/bulk`, which uses AngelOne REST, recovered them every time
  ("per-symbol path failed for 3/11 ... /quotes/bulk recovered 3"). So these names are priceable; `/quote` was just too slow.
- Not shown by the report: whether real-trade-service actually paused these five (section 3b printed nothing). The names the
  boot log shows as paused are AAKASH, BMISL, QUALIANCE (no price) and ACEVECTOR, ARMEE, MONEYVIEW (no daily history); they are a
  different case and are not touched here (`NAMES="AAKASH BMISL QUALIANCE" bash scripts/diagnose_never_priced.sh` checks them).

## Fix
- `market-data-service/main.py`: new `_angelone_rest_quote_first(sym)`. In `_get_quote_inner`, after the live-feed hits, the cache and
  the group161 "no price" check, and before Yahoo, the same AngelOne REST quote that `/quotes/bulk` uses is requested for one token.
  A hit returns `source: "angelone_rest"` with price, previous close (AngelOne `close`), day change %, day high/low and volume, caches
  it for 12 s (the `/quotes/bulk` TTL) and keeps the last-good copy. `atr` stays `None` (AngelOne has none; merges keep the prior value).
- Every miss falls through to the **unchanged** Yahoo path: AngelOne not configured, `angelone_quote` cooldown, no scrip-master
  token, empty/invalid answer, an error, a call longer than `QUOTE_ANGELONE_FIRST_TIMEOUT_S` (default 6 s, allowed 0.5-30), a busy
  rate bucket, an index / `NIFTY*` symbol, or being called from inside a running event loop. The helper never raises.
- `market-data-service/angelone_client.py`: `get_quote(exchange, token, max_wait=20.0)`. With the default nothing changes. `/quote`
  passes 2 s, and below 20 s the call is **fail-closed** (`try_acquire`): if the `angelone_quote` bucket (5/s, burst 8, shared with the
  feed poll and `/quotes/bulk`) has no token within 2 s it returns `{}` without sending anything, so a burst sheds load to Yahoo
  instead of adding to the AngelOne 403 problem.
- Off switch: `QUOTE_ANGELONE_FIRST=0`. Not added to docker-compose (no other market-data tunable is listed there either); put it in `.env`.

## Not changed
- The AngelOne rate limit itself (403 "exceeding access rate" on `getCandleData`), the 19.1 s feed poll over 489 symbols, and api-gateway's
  per-symbol `/quote` waves are item 2 and still need your decision. This change adds at most one AngelOne call per non-feed symbol per 12 s,
  through the same bucket, and gives up when the bucket is busy.
- real-trade-service's pause logic and the universe filter: no change. Nothing here drops or adds names.

## Tests
`market-data-service/tests/test_group199_quote_angelone_first.py` (27): quote shape, optional fields, off switch, bad answers, not configured,
cooldown, no token/base, indices, exception, slow call cut off, running loop, timeout bounds, route (AngelOne answers and Yahoo is never asked;
a miss falls to Yahoo; off switch; fresh cache wins; a price clears the negative cache), client fail-closed and default blocking acquire.
Sandbox: market-data 935 passed (908 on the uploaded zip). Not live-tested.

## Check on the VM after the rebuild
`bash scripts/diagnose_never_priced.sh 2>&1 | tee never_priced_report.txt` - the first `/quote` for STEAMHOUSE / SGRL / KENNAMET / ROSSTECH
should now say `source='angelone_rest'` and take well under a second, and real-trade-service should stop logging ReadTimeout for non-feed names.
If `source` is still `yahoo`, look for `angelone_quote` cooldown lines in market-data (the fallback is working as designed).
