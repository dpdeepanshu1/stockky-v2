# Group 274 - merge of the Dhan Data API branch and the opening-gate branch (2026-10-09)

Two zips were built in parallel from group 269 and both got the number 270:

| Branch | Zip | Content |
|---|---|---|
| Dhan | `...group270-dhan-data-api.zip` | market-data-service `dhan_data/`, sentiment via market-data, optional training helper (docs/GROUP270_DHAN_DATA_API.md) |
| Opening gate | `...group273-prevday-stale-guard-audit.zip` | groups 270-273: previous-day candle checks, shadow stop/target, first-sweep uptime fix, stale previous-day guard |

Base for the merge: the group 273 tree. Every Dhan-only file was layered on top. No source file was edited by both branches.

## Files both branches touched (combined by hand)
- `.env.example` - the opening-gate block (groups 268-273) and the Dhan block (group 270) are both present.
- `docker-compose.yml` - `DHAN_CREDENTIAL_ENC_KEY` on market-data-service (Dhan) and `MARKET_DATA_URL` on position-stocks-service (opening gate).
- `CHANGELOG_INDEX.md` - both "group 270" entries kept, plus this entry.
- `.env.oracle.recommended` and `docs/GROUP268_*.md` were changed by one side only (Dhan resp. opening gate) and taken from that side.

## Interaction checked
The opening gate reads daily candles from market-data `/history/{sym}?period=1mo&interval=1d` and uses the keys `date/high/low/close`.
Dhan candles use the same keys (`date` is `YYYY-MM-DD HH:MM`; the parser only reads the first 10 characters), so with Dhan as a
history source the previous-day check keeps working. The stale guard (`OPENING_GATE_PREVDAY_MAX_AGE_DAYS`) applies to Dhan data too.

## Gaps found and fixed after the merge (checked against the Dhan plan and the Dhan session summary)
- kv_cache drift guard (api-gateway) failed on the undocumented `stockky:dhan_scrip` prefix: now documented + owner-only guard.
- `DHAN_HOURLY_MAX_DAYS` bare `int(os.getenv())` -> blank-safe config helper.
- Five Dhan variables read by the code were missing from `.env.example` / `.env.oracle.recommended`.
- Missing plan tests added: blank-env + "every Dhan variable documented" (market-data) and the "no new direct yfinance callers" guard (api-gateway).
- Details: end of docs/GROUP270_DHAN_DATA_API.md.

## Test results (sandbox, merged tree)
- position-stocks-service: 2986 passed
- real-trade-service: 3850 passed, 1 skipped
- market-data-service: 1403 passed, 16 failed - the same 16 fail in the untouched Dhan zip (its own doc says they also fail on group 269); same failure list before and after the merge
- analysis-intelligence-service: 2443 passed, 1 failed (`test_QUIRK_raw_feed_is_shadowed_by_symbol_route`, documented in the Dhan zip as pre-existing)
- decision-prediction-service `test_group270_md_history.py`: 3 passed
