# Session 141 — api-gateway coverage passes 9-12 (combined): refill_additional, symbol_aliases, buy_sniper, instant_scanner (2026-09-30)

- New (no production code changed):
  * `tests/test_refill_additional.py` (192 tests) — job mirror + stale detection, rate-limited retry GET, row builder, runner (sync + real thread pool), stop handling.
  * `tests/test_symbol_aliases.py` (341) — static tables + invariants, `is_*` filters, learned rename / delisted / failure streaks over a fake KV, NSE announcement parsing over a fake httpx, `resolve_with_fallback` precedence.
  * `tests/test_buy_sniper.py` (289) — helpers, sector adjust, candidate gate, target/stop/R:R math, filter + payload wrapper, env overrides.
  * `tests/test_instant_scanner.py` (349) — feature bag, technical / fundamental bands and clamps, decision bands, full card, async `process_single_stock` with a fake client.
- Coverage (statements + branches): all four modules 100% / 100%. Full gateway suite `bash run_tests.sh --single`: 2819 passed (1648 + 1171); gateway TOTAL 22% -> 28%. Per-file mode (`bash run_tests.sh`) also green.
- Hermetic: fresh module copy per test where the module reads env at import; `kv_cache`, `httpx`, `rate_limiter`, `data_feed`, `price_resolver` are fakes in `sys.modules`; fake clocks, no sleeps, no network.
- Drift guards: names main.py / other gateway modules import exist; real `rate_limiter` / `kv_cache` / `price_resolver` signatures match the calls; `SYMBOL_RENAMES` agrees with market-data-service `SMART_SYMBOL_MAP`; buy_sniper card carries every required field of the frontend `BuySuggestion` interface.
- Mutation spot-check (operator swaps, sampled; bytecode caching off): buy_sniper 30 (26 caught), symbol_aliases 25 (19), instant_scanner 40 (32). Every survivor was a docstring line or provably equivalent (e.g. rsi `<45` vs `<=45` is shadowed by the earlier `45<=rsi` branch; `prev_close` second guard is re-stamped by `apply_price_aliases`). Two real gaps found (EMA band exact edges) and closed with tests.
- Observations, left unchanged (none pinned by a test):
  * symbol_aliases: `resolve_with_fallback` does NOT chase multi-hop renames, `resolve_ns_ticker` does. `MINDTREE` -> `LTIM.NS` (superseded) vs `LTM.NS`.
  * symbol_aliases: `resolve_with_fallback` calls `learned[base].get("to")` unguarded; a malformed learned entry (non-dict) raises AttributeError. `_apply_all_renames` guards the same case with `isinstance`.
  * symbol_aliases: market-data-service short-circuits `AAKASH` and `ANNAPURNA` as delisted; the gateway `KNOWN_DELISTED` lacks them.
  * symbol_aliases: `_apply_all_renames` loop-exhaustion branch is unreachable with a real dict (each hop consumes a distinct key); covered with a dict that under-reports `len()`.
  * instant_scanner: `_extract_price` fallback crashes on `feed=None` (`feed.get`); all real callers pass a dict.
  * instant_scanner: `compute_instant_scores` second `prev_close <= 0` guard is dead code (`resolve_stock_features` already does it).
  * instant_scanner: a price-only row reports `provisional_defaults=True` but still gets a decision from default indicators (PREPARE TO BUY is reachable with no feed data at all).
  * instant_scanner: `from_data_feed` can be a dict/str rather than a bool (it is `bool(feed) and (... or feed.get("metrics") ...)`).
