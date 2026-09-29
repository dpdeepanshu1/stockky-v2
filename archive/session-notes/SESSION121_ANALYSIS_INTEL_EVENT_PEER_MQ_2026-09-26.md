# Session 121 — analysis-intelligence-service: event_depth + peer_multi_quarter (2026-09-26)

## Files added (120 new tests, 120/120 passed)

### `tests/test_event_depth.py` — 62 tests

Covers `event/event_depth.py` (317 lines) end-to-end. No network needed.

- `classify_text`: results, bulk_block, insider, board, empty, multiple tags, case-insensitive.
- `summarize_event_block`: next_earnings_date, strong beat, miss, insider buy/sell/ambiguous,
  bulk deals, upcoming events, recent fallback, no events, symbol prefix, bad pct_surprise.
- `_age_days`: same day, 10 days, None, invalid, strips T time component.
- `_decay`: zero→1.0, None→1.0, negative→1.0, half-life decay, floor at 0.15, ceiling at 1.0.
- `compute_event_score`: 31 scenarios — earnings strong/mild beat, earnings miss/mild miss,
  imminent risk flag (event_risk=True), pre-results momentum, bonus/split, buyback, rights
  issue (dilutive), M&A, delisting risk + event_risk, dividend, analyst upgrade/downgrade/buy
  grade, insider large buy, insider small buy, insider sell, bulk buy/sell, block deal,
  fii inflow/outflow, fii bad value skipped, regulatory action + event_risk, board meeting,
  score clamped 0-100, earnings_days_out populated, alternative insider key.
- `enrich_events`: adds event_summary/score/breakdown/risk, backward-compat
  recent_event_score (0-1), has_positive_catalyst True/False, earnings_days_out propagated,
  event_score_raw_delta, None input handled, original fields preserved.

**Bugs caught:** `earnings_days_out` uses `datetime.utcnow()` internally — result
varies by ±1 day due to UTC vs IST offset; test widened to `range(4, 9)`.

### `tests/test_peer_multi_quarter.py` — 58 tests

Covers `fundamental/peer_multi_quarter.py` (386 lines) end-to-end. httpx.get monkeypatched.

- `_safe`: float, None→default, NaN, string, bad string, custom default.
- `_norm_symbol`: adds .NS, preserves .NS/.BO, uppercases.
- `detect_sector`: all 8 sector mappings (IT, BANK, AUTO, PHARMA, FMCG, METAL, ENERGY,
  capital goods via "electrical"), unknown→DEFAULT, empty→DEFAULT, sectorDisp key. Bug
  found: "Hospitality" → matched "it" substring → "IT"; fixed to use "Agriculture".
- `_fund_cache_get/_fund_cache_set`: miss, set/get, expired returns None, thread-safe
  (20 concurrent writers, no errors).
- `fetch_fundamentals`: cached hit (no HTTP), 200 fetches and caches, non-200→{}, exception→{}.
- `fetch_fundamentals_batch`: all from cache, empty list→{}, parallel fetch, failed→{}.
- `compute_peer_relative`: expected keys, cheaper PE raises, expensive PE lowers,
  empty peers→neutral 50, self excluded, sector detection, score clamped 0-100.
- `compute_multi_quarter_consistency`: empty→50, two positive quarters, mixed inconsistent,
  fundamentals fallback, zero yoy not counted, high growth bonus (>95), alternative keys
  (revenueGrowth/earningsGrowth), score clamped.
- `enrich_fundamentals_with_peer_and_consistency`: adds peer+consistency, peer error→50,
  multi-quarter error→50, original fields preserved.

## Running totals for analysis-intelligence-service

| File | Session | Tests |
|---|---|---|
| `rate_limit_report.py` | 120 | 19 |
| `news/news_quality.py` | 120 | 40 |
| `technical/shared_adaptive.py` | 120 | 22 |
| `fundamental/peers.py` | 120 | 28 |
| `fundamental/indianapi_fallback.py` | 120 | 33 |
| `event/event_depth.py` | 121 | 62 |
| `fundamental/peer_multi_quarter.py` | 121 | 58 |
| **Total** | | **262** |

## Next by priority in analysis-intelligence-service

1. `sentiment/main.py` (410 lines) — smallest remaining main
2. `news/main.py` (658 lines)
3. `fundamental/main.py` (660 lines)
4. `technical/main.py` (766 lines)
5. `event/main.py` (1240 lines) — largest
6. `main.py` (134 lines) — tiny orchestrator, last
