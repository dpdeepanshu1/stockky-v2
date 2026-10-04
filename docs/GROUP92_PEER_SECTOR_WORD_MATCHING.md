# group92 (2026-10-04) - item 10: wrong peer sets (Auto -> FMCG peers, Utilities -> IT peers, retail -> generic set)

Cumulative on group91. In `analysis-intelligence-service` run `bash run_tests.sh`, then `docker compose build analysis-intelligence-service && docker compose up -d`.

## Cause
`fundamental/peer_multi_quarter.py::detect_sector` chose the peer set by SUBSTRING tests on the first non-empty of sector / industry / sectorDisp:
- `"IT" in text` was true inside "CAPITAL", "UTILITIES", "HOSPITALITY" ... so a utility / renewable company (CLEANMAX) got IT peers.
- Any text containing "CONSUMER" went to FMCG, and Yahoo files autos, retail and apparel under "Consumer Cyclical", so an auto maker (Hero) got FMCG peers.
- `sector` was read before the more specific `industry`, so the useful "Auto Manufacturers" was never looked at.
- Retail had no peer set, so it ended in the generic `DEFAULT` list (RELIANCE, TCS, HDFCBANK, INFY, ICICIBANK).

## Fix (that file only)
- Matching is on whole WORDS (exact words or word prefixes), never substrings. `TECH` still covers TECHNOLOGY but not BIOTECHNOLOGY; `AUTO` does not match AUTOMATION.
- Fields are tried most specific first: `industry`, then `sector`, then `sectorDisp`; the first one that names a known sector wins, so "Consumer Cyclical" + "Auto Manufacturers" is AUTO. An industry with no known word (for example "Capital Markets") falls through to the sector.
- "Consumer Cyclical / Discretionary / Durables" is no longer FMCG. "Consumer Defensive / Staples" and a bare "Consumer" still are.
- New words: FOOD, BEVERAGE, TOBACCO, HOUSEHOLD, PACKAGED, DEFENSIVE, STAPLES, BIOTECH, ALUMINUM and UTILITIES.
- Two new peer sets in `DEFAULT_PEERS`: `POWER` (NTPC, POWERGRID, TATAPOWER, ADANIPOWER, JSWENERGY, NHPC; used for Utilities and "power") and `RETAIL` (DMART, TRENT, ABFRL, SHOPERSTOP, VMART, BATAINDIA). `ENERGY` is now oil, gas and "energy" only.
- Non-string sector values (None, numbers, lists) count as "no data" instead of being stringified.
- Unrecognised sectors still return `DEFAULT` with the same generic list.

## Not changed / not verified
- I have not seen the payloads for HEROMOTORS, CLEANMAX or SSRETAIL. The Yahoo sector/industry strings above are the standard Yahoo taxonomy, so the three causes are the likely explanation, not a confirmed one. If SSRETAIL's payload has no sector or industry at all, it still gets `DEFAULT`; check `sector` / `industry` in `/fundamental/analyze/SSRETAIL` after deploy.
- "HEROMOTORS" is not an NSE symbol (Hero MotoCorp is `HEROMOTOCO`); that spelling problem belongs to item 11 (name resolution).
- I have not checked that the new peer symbols are all fetchable from market-data; one that is not is skipped and counted in `peers_skipped`, as before.
- "Financial Services" still maps to BANK (so insurers and asset managers get bank peers). That is the old behaviour and is not part of this item.
- `fundamental/peers.py` (the curated sector map used by `analyze()`) is a separate map and was not touched.

## Tests
`tests/test_peer_multi_quarter.py::TestDetectSectorWordMatching` (39 cases: real Yahoo sector/industry pairs, no "IT" inside other words, consumer cyclical vs defensive, industry-before-sector, sectorDisp, non-string values, punctuation, automation, new peer sets). Against the old `detect_sector` 20 of them fail; all pass now. Real pytest 9 with the pinned requirements in a clean venv: the three peer test files 231 passed (this includes the 5 `test_peer_ranking.py` tests the group88 stand-in runner could not run), full analysis-intelligence-service suite 2148 passed.
