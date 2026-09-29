# Session 125 — analysis-intelligence-service: root main.py + sentiment/main.py coverage (2026-09-29)

Picked per the session-124 "Next" instructions. Coverage table before: TOTAL 98% (75 missed); `main.py` 87% (8 missed)
and `sentiment/main.py` 89% (29 missed) were the two lowest. Target: both to 100%.

## Files changed (tests only — no production code touched)

### `tests/test_service_main.py`  (root `main.py`)

Root cause of the gap: `build` executes a *copy* of `main.py` under `__pycache__` (deliberately omitted by
`.coveragerc`), and the one real-file smoke test only takes the all-mounts-OK path — so the failure branches (missing
folder, no `app`, mount-failure handler, health "error"/"degraded", BASE sys.path insert) never counted for the real
file. New `_exec_real_source()` compiles the REAL `main.py` with its real filename (so coverage attributes lines to
it) but executes it with `__file__` pointing into a temp tree of fake sub-apps. Added `TestRealSourceBranches`
(8 tests). Added `import types`.

### `tests/test_sentiment_main.py`  (`sentiment/main.py`)

| Class | Covers (lines) |
|---|---|
| `TestFetchIndividualTickerZeroRetries` | `max_retries=0` -> trailing `return None`, Yahoo never called (150) |
| `TestBatchFallbackWhenIndividualAlsoFails` | download raises + individual fails (190); empty/None batch + individual fails (199); partial individual success |
| `TestBatchPerSymbolFallbacks` | symbol absent from batch frame (206-210); single-row frame (233-236); processing error via missing `Close` column (237-241); each with individual success AND failure; one bad symbol doesn't affect a good one |
| `TestComputeMarketScoreAdjustmentFailures` | Yahoo failure on `6d` / `1mo` skips only that adjustment (282-283, 296-297); `Ticker()` constructor failure swallowed |
| `TestDoubleCheckedCacheInsideLock` | cache filled while waiting for the lock is served without refetch (329-333); data-without-timestamp counts as expired |
| `TestMainEntrypoint` | `if __name__ == "__main__"` block via `runpy` + stub `uvicorn`: default port 8009, `PORT` override, `reload=True` (409-411) |

## Verification status

- First attempt (earlier sandbox, deps installed): `pytest tests/test_sentiment_main.py tests/test_service_main.py` -> 104 passed;
  `./run_tests.sh` -> `main.py` 100%, `sentiment/main.py` 100%, TOTAL 99%; `./run_tests.sh --single` -> 1741 passed.
- Packaging session (this zip): that sandbox was reset and had NO pytest/fastapi/yfinance and no network. The identical test code
  was re-applied and only `py_compile`-checked here — NOT re-run. Please run `./run_tests.sh` and `./run_tests.sh --single`
  on your VM to confirm.

## Next

`fundamental/indianapi_fallback.py` 93% (44-45, 146, 159-160, 199-201), then `event/event_depth.py` 97%
(118-119, 167-168, 174-175), `fundamental/peer_multi_quarter.py` 98% (173-175), `fundamental/peers.py` 98% (74-75),
`news/news_quality.py` 98% (158-159). The `8-9`/`11-12` lines and `657-661`-style tails are import-fallback / `__main__` blocks
— the `runpy` + stub-`uvicorn` technique from `sentiment/main.py` closes the `__main__` ones.
