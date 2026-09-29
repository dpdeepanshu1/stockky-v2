# Session 122 — analysis-intelligence-service: sentiment/main.py (2026-09-26)

## File added

### `tests/test_sentiment_main.py` — 47 tests, 47/47 passed

Covers `sentiment/main.py` (410 lines) end-to-end. yfinance stubbed before
import; FastAPI TestClient for route tests; time.sleep patched where retries
would block.

| Class | Coverage |
|---|---|
| `_safe_float` | float, None, NaN, Inf, string, bad string, rounding |
| `_safe_int` | int, None, float truncation, bad string |
| `classify_sentiment` | all 5 bands including both boundaries (75/55/45/25/24) |
| `compute_market_score` | empty→50, no change_percent→50, positive>50, negative<50, large cap at 100, returns int, weighted average NIFTY>SENSEX, momentum adjustment path (6-day history), volatility adjustment path (1mo history) |
| `fetch_individual_ticker` | empty history→None, 2-row → IndexData with correct change_percent, 1-row→None, retry on transient exception, exhausted retries→None |
| `fetch_indices_batch` | empty symbols→{}, batch success with MultiIndex DataFrame, fallback to individual on empty batch, fallback on download exception |
| Routes `GET /health` | healthy status |
| Routes `GET /` | running + version |
| Routes `GET /sentiment` | neutral fallback when no data (stale=True), real score from IndexData, cache served on second call (cached=True), force_refresh bypasses cache, stale cache returned when data unavailable, trend/breadth/momentum/volatility populated |

**Key design points verified:**
- Double-checked locking pattern: outer pre-lock check + inner post-lock check
  both tested via the cache hit path.
- `stale=True` returned both when no data ever fetched (first cold call) AND
  when fresh fetch fails but warm cache exists.
- Momentum path only runs when ≥6 rows; volatility path only when ≥15 rows —
  both gated correctly, empty history silently skipped.
- `INDEX_WEIGHTS` (NIFTY 60%, SENSEX 40%) verified: NIFTY +0.3% + SENSEX -0.3%
  → weighted avg positive → score > 50.

## Session totals for analysis-intelligence-service

309 tests passing (up from 262 after session 121).

## Next

`news/main.py` (658 lines) → `fundamental/main.py` (660 lines) →
`technical/main.py` (766 lines) → `event/main.py` (1240 lines) →
`main.py` (134 lines).
