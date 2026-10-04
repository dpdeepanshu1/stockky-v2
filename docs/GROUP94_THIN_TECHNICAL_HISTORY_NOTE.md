# group94 (2026-10-04) - item 12: no UI note when technical analysis uses the minimal bar

Cumulative on group93. In `api-gateway` run `bash run_tests.sh`, then `docker compose build api-gateway && docker compose up -d`.

## Cause
When price history is thin, the technical service falls back to a single quote/bhavcopy bar, a market-data fallback, or fewer than about 30 daily bars. In those cases it already writes a reason into `reasons.technical` ("Limited history for X; using last quote...", "Fallback quote ...", "EMA trend: insufficient data"), and the score is a neutral 50 or a partial read. `/stock/{symbol}` in `api-gateway/main.py` built its `data_quality` flags from other signals only (fundamental fallback, news, model score, delivery %, training), so the card showed "Data quality: high" next to a technical read made from one bar. The gateway also sets `data_insufficient` to False as soon as any close price exists, which hid the signal further.

## Fix (`api-gateway/main.py` only)
- New helper `_thin_technical_history(result, tech_reasons)`. It is true when `reasons.technical` contains a minimal/fallback marker (limited history, history temporarily thin, price history unavailable, fallback quote, fallback technical, market-data fallback, full technicals retry, "EMA trend: insufficient data", "MACD: insufficient data"), or when the decision service's own pillar map says `data_quality.pillars.technical` is False.
- When true, `/stock/{symbol}` adds the flag `Technicals on minimal price history` as the FIRST flag (the card shows only the first three) and lowers a "high" level to "medium". It never raises a level that is already medium or low.
- The payload's overall `data_insufficient` is deliberately not used as a trigger: it can be set for non-technical reasons.
- No change to scoring, the technical service, the decision service or the frontend. The card already renders `data_quality.flags`, so the note appears without a UI change.

## Not changed / not verified
- **Marker list:** it matches the reason texts as they are written today. If the technical or decision service rewords those messages, the flag stops firing; the tests pin the current wording.
- **Other entry points:** only `/stock/{symbol}` builds this `data_quality` object. Scan results, hot picks and the real-trade decision path do not show the flag. Whether the scoring itself should discount a one-bar technical read is a separate decision and was not changed.
- **Delivery / other thin-data cases** keep their existing flags.
- Not run under real pytest here (the sandbox has none): the helper was checked standalone against 11 cases and the changed files were compiled. Run `bash run_tests.sh` on the VM.

## Tests
`tests/test_main_stock_scan_routes.py`: `TestStockThinTechnicalFlag` (limited-history reason adds the flag first and lowers the level; five more markers; full-history reasons add nothing; pillar map False adds it; it leads other flags and does not raise a low level) and a parametrized `_thin_technical_history` helper test (empty/None, malformed `data_quality`, pillar True/False).
