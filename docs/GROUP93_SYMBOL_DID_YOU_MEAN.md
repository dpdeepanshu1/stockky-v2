# group93 (2026-10-04) - item 11: a mistyped company name silently resolved to the wrong symbol ("HERO MOTERS" -> HEROMOTORS)

Cumulative on group92. In `api-gateway` run `bash run_tests.sh`, then `docker compose build api-gateway && docker compose up -d`.

## Cause
`GET /stock/{symbol}` in `api-gateway/main.py` had three problems that combined:
- `_add_searched()` ran BEFORE the analysis, so every typed name, right or wrong, was stored in the "searched symbols" list. `_get_all_known_symbols()` includes that list, so a misspelling became a "known" symbol and then won the next fuzzy match. A recorded `HEROMOTORS` (not an NSE symbol; Hero MotoCorp is `HEROMOTOCO`) beat the real symbol: "HEROMOTERS" scores 0.90 against HEROMOTORS and 0.70 against HEROMOTOCO.
- Fuzzy matches were applied silently at a 0.7 cutoff. Any near-miss got analysed as if it were what the user meant (the response only carried `corrected_from`).
- A name typed as words ("hero moters") kept its space; NSE symbols never contain one, so it could not match exactly and went to the fuzzy step.

## Fix (`api-gateway/main.py` only)
- Whitespace is removed from the typed text (`_squash_symbol`); hyphens and `&` are kept (BAJAJ-AUTO, M&M).
- A fuzzy correction is applied silently only when it scores at least 0.90 AND is at least 0.05 ahead of the next candidate. Otherwise the route returns 404 `Symbol 'X' not found. Did you mean: A, B, C?` (best match first, up to 3) and makes no upstream calls. Curated aliases and exact known symbols are unchanged.
- `SYMBOL_ALIASES` now maps `HEROMOTORS`, `HEROMOTERS`, `HEROMOTER`, `HEROMOTOR`, `HEROMOTO` to `HEROMOTOCO`.
- The symbol is recorded as searched (and the universe cache dropped) only after an analysis that has a real close price. A degraded HOLD, an unreachable engine or a 404 records nothing, so typos stop polluting the known-symbol set. A failure while recording is logged and does not fail the analysis.
- `_resolve_symbol` itself keeps its 0.7 fuzzy cutoff and its return type (other code and tests rely on it); the confidence gate is in the route.

## Not changed / not verified
- **Already-polluted data:** if `HEROMOTORS` (or other typos) are already in your Redis `stockky:searched_symbols` list they stay there. For HEROMOTORS the new alias now takes precedence, but other stale entries can still appear as fuzzy candidates until they age out of the 200-entry list. To clear the list: delete the `stockky:searched_symbols` key.
- **Frontend:** I did not change it. It shows the 404 `detail` text as before; the `Did you mean` text is in that string. There is no clickable suggestion list.
- **Other routes:** only `/stock/{symbol}` was changed. Other routes that call `_resolve_symbol` or fuzzy-match on their own were not reviewed.
- **Unknown but real symbols:** a brand-new listing that is not in any universe still goes to the engine as typed (no near match to ask about), as before.
- Not run under real pytest here (the sandbox has none); the pure helpers were checked standalone and the tests were compiled.

## Tests
- `tests/test_main_universe.py`: `_squash_symbol`, whitespace handling in `_resolve_symbol`, the HEROMOTORS aliases, `_fuzzy_correction_is_confident` (close and unique, below the bar, runner-up too close) and `_symbol_suggestions`.
- `tests/test_main_stock_scan_routes.py`: spaced name squashed, alias correction reported, weak fuzzy match returns 404 `Did you mean` with no upstream call, confident correction still silent, searched list not written on a degraded HOLD, a recording failure does not fail the analysis. The test stub `Env._resolve` now squashes spaces like the real function.
