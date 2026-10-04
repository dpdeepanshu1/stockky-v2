# group105 (2026-10-04) - item 12 remainder: `technical_thin` label on scan and Hot Picks rows

Cumulative on group104. In `api-gateway` run `bash run_tests.sh`, then `docker compose build api-gateway && docker compose up -d`.

## Why
group94 added the "Technicals on minimal price history" data-quality flag, but only to `/stock/{symbol}`. Scan results and Hot Picks rows could still show a confident-looking technical read built from a single quote bar, with nothing marking it.

## Change (`api-gateway/main.py` only)
- New `_mark_thin_technical(result)`: sets `result["technical_thin"]` (bool) using the same `_thin_technical_history` test group94 added (minimal/fallback markers in `reasons.technical`, or the decision service's `data_quality.pillars.technical` being False). Never raises.
- Called at the end of the full-analysis path of `_analyze_one_symbol_ultra`, so Run Market Scan, batch scans and the scan stream all get the field.
- Hot Picks: `technical_thin` is added to the two field lists that seed and rebuild a Hot Picks card from the cached decision (`_build_hot_conviction_extra` and the `last_decision` seed), so it survives into the cards. `False` is kept (not treated as missing).

## Label only
No score, decision, confidence or ranking changes. Easy to revert: remove the one call and the helper.

## Not changed / not verified
- **Lite / Data Feed fast-path rows** carry no technical reasons, so they get no `technical_thin` field (absent, not False). Circuit-open and error rows likewise.
- **Frontend:** nothing displays the field yet. It is in the API payload only; a badge on the scan table and Hot Picks cards is a separate UI change.
- **Marker list** is the one from group94 and matches today's reason wording; if the technical service rewords its messages the flag stops firing.
- Whether scoring should discount a one-bar technical read is still a separate decision.

## Tests (`tests/test_main_analyze_symbol.py`, 7 new)
Thin reason -> True; normal reasons -> False; pillar map False -> True; score/decision/confidence identical with and without the thin reason; lite fast path has no field; helper safe on `None` and malformed `reasons`; Hot Picks extra carries the field (including False, and the fuller record wins).

## Run here
api-gateway: 8118 passed (was 8111).
