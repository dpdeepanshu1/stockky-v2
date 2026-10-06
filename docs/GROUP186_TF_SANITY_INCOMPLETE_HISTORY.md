# Group 186 - candidate timeframe sanity, "incomplete history", bhavcopy word lookups

Cumulative on group 185. Items 1, 2 and 3 of the 2026-10-06 boot-log list.
Rebuild real-trade-service and market-data-service.

## Item 1 - HFCL weekly return of +211%
The boot log showed `Returns: {'1w': 211.28, '1m': 4.72, '3m': 17.22, ...}` for HFCL. The 1-week window sits inside the
1-month window, so it cannot have returned 3x the month unless the first bar of the 5d history was bad; `_pct_return`
trusts the first open blindly and the bogus value counted as a bullish timeframe (it was one of the 3.0 points).
`candidate_engine/candidates.py::_sanitize_tf_returns` (called in `_multi_tf_analysis` right after the returns are
computed): a 1d / 1w / 1m return above its cap (25 / 60 / 120 %) AND more than the cap above the next longer horizon
(1w / 1m / 3m) is set to None, with one WARNING naming the symbol. With no longer horizon to compare against the value
is kept (a real new-listing spike must not vanish). The dropped horizon then counts as missing (item 2).
Env: `CANDIDATE_TF_SANITY=0` turns it off; `CANDIDATE_TF_CAP_1D_PCT`, `_1W_PCT`, `_1M_PCT`.
The cause of the bad bar itself is not known (the log does not show the candles); the guard stops it scoring.

## Item 2 - TCS rejected on missing data
TCS had no return for 1d, 1w, 6m, 1y, 2y (history fetches failed under the AngelOne 403 / yfinance timeouts), so its
score was 1.0 of 4 and the reason read "Weighted bullish score 1.0". A data gap was reported as weak momentum.
Now, when the score is below the threshold but the missing horizons could still lift it to the threshold, the reject
reason is `Incomplete history, cannot judge: no return for 1d, 1w, 6m, 1y, 2y ...` and the result carries
`data_incomplete: True`. When the missing horizons could not reach the threshold even if all were bullish, it stays a
normal weak-momentum rejection. Three or more such symbols in one cycle log one WARNING pointing at the /history
failures. The candidate is still not inserted this cycle (behaviour unchanged); only the reason and the log are honest.
An all-empty history now reads "Incomplete history" too (the older test that expected "Weighted bullish score" there
now feeds flat data).

## Item 3 - FOCUS / TECH reaching the bhavcopy price lookup
`eod_close_from_bhavcopy(FOCUS)` and `(TECH)` each scanned 12 session days and logged. Both are plain words, not NSE
tickers. WHO asked market-data-service for them is not visible in the log (the quote calls are logged only when they
finish), so the source is NOT fixed. What changed, in `market-data-service/bhavcopy.py`: a symbol that is absent from
every recent bhavcopy day that really had rows is remembered for `BHAVCOPY_EOD_MISS_TTL_S` (default 21600 s, 0 = off);
a repeat lookup returns at once and is not logged again. An empty or unparsed day proves nothing and is never
remembered; the memory is capped at 2000 symbols.
To find the caller, run `docker compose logs real-trade-service api-gateway | grep -E "FOCUS|TECH"` after a boot.

## Tests
New `real-trade-service/tests/test_group186_tf_sanity_incomplete_history.py` (16 cases) and
`TestEodMissMemory` (6 cases) in `market-data-service/tests/test_bhavcopy.py`; `test_candidates_analysis.py` and the
`_clean` fixture in `test_bhavcopy.py` adjusted. No pytest, httpx or sqlalchemy in the sandbox: the 16 + 6 new cases pass
under a stand-in runner with stubbed imports; the full suites were not run (3 volume-shock cases in
`test_candidates_analysis.py` fail under the stub harness on the original code as well, so they say nothing here).
