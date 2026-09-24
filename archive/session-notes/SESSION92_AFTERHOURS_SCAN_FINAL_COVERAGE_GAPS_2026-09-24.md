# Session 92 (2026-09-24): afterhours_scan.py — final 3 coverage gaps closed

## What was already in this zip coming in

The pasted terminal transcript (`services/real-trade-service`, full
`pytest -q --cov=. --cov-report=term-missing`) confirmed session91's rounds
1-3 (`test_afterhours_scan_pure_helpers.py`, `test_afterhours_scan_fetchers.py`,
`test_afterhours_scan_orchestration.py`) actually pass on a live VM: **1694
passed, 1 xfailed, 92% total coverage, no failures.** Nothing was broken —
this session is closing the last named gaps in
`watchlist_engine/afterhours_scan.py`, which the transcript's own coverage
table showed at 98% (Missing: `390, 494, 866-872`), plus verifying
`tests/test_afterhours_scan_orchestration.py`'s own reported miss (`373`).

## This session's actual work — 3 real gaps, 3 targeted tests, no production code changed

1. **`tests/test_afterhours_scan_orchestration.py:373` is not a real gap.**
   It's the file's own `if __name__ == "__main__": sys.exit(pytest.main(...))`
   bootstrap line, which only executes when the test file is run directly as
   a script — never under a normal `pytest` invocation. The same pattern
   exists in `test_afterhours_scan_fetchers.py`,
   `test_afterhours_scan_pure_helpers.py`, `test_main_routes_trading.py`, and
   `test_main_routes_core.py` (visible as part of *their* reported misses in
   the pasted coverage table too, e.g. `test_main_routes_trading.py`'s
   `1107-1108`) — accepted, unfixed by design across the whole repo. Left
   alone.

2. **`afterhours_scan.py:390`** — `_extract_symbol`'s final `return None` in
   the degraded (no `known_symbols`) fallback path. No existing test drove a
   headline where every ALL-CAPS token is either a stopword or under the
   6-char length floor, so this line — the actual "found genuinely nothing"
   outcome — was never exercised (only the "found something via the length
   heuristic" sibling line was, in
   `test_afterhours_extract_symbol.py::test_unknown_long_token_still_falls_through_to_heuristic`).
   Added `test_degraded_path_with_no_viable_token_returns_none` using
   `"Market reports strong growth today"` (MARKET/REPORTS/STRONG/GROWTH all
   in `_STOPWORDS`/`_ENGLISH_STOPS`, TODAY at 5 chars fails the length
   floor).

3. **`afterhours_scan.py:494`** — turned out to be a more interesting miss
   than it looked. It's `_parse_item_datetime`'s plain-`"YYYY-MM-DD"`
   strptime **success** return, not a `None` branch. The existing
   `test_plain_yyyy_mm_dd_parses("2026-09-16")` never actually reaches this
   line: a bare `"2026-09-16"` is itself valid ISO-8601 (date-only), so
   Python's `datetime.fromisoformat()` accepts it directly and returns from
   the ISO branch two `try` blocks earlier — the plain-date strptime
   fallback this function's own docstring describes for
   insider_transactions/bulk_deals date fields is dead code for any input
   that's *already* clean ISO. The real-world case it exists for is a date
   string with trailing non-ISO text after the date (a bulk-deal remark,
   etc.) that `fromisoformat()` rejects but `strptime(value[:10], ...)`
   still parses. Added
   `test_plain_date_with_trailing_text_reaches_strptime_branch` using
   `"2026-09-16 (bulk deal)"`, confirmed by hand-tracing both stdlib calls
   directly (`fromisoformat` raises `ValueError` on the full string,
   `strptime` on `value[:10]` succeeds).

4. **`afterhours_scan.py:866-872`** — `run_afterhours_scan`'s per-symbol
   upsert loop's `except Exception as e: db.rollback(); ...; continue`. This
   is the same incident class as session65's `import_broker_holdings` fix
   and session82c's `evaluate_mode` isolation fix (one bad row must not take
   down the whole batch) — but for this function specifically, nothing
   exercised the isolation actually working. Added
   `test_one_symbols_upsert_failure_is_isolated_and_others_still_write`:
   monkeypatches the real in-memory-SQLite `db` fixture's own `.commit` to
   raise `RuntimeError` on its first call only, with an RSS hit (RELIANCE,
   inserted first) and a bulk-only hit (TCS, inserted second) in the same
   pass — RELIANCE's commit fails and is rolled back, TCS's still succeeds.
   `written == 1`, RELIANCE absent, TCS present.

## Why 866-872 specifically (not `finalize_nextday_watchlist`)

Worth flagging since the line numbers sit close to that function: the
coverage table's `866-872` is inside `run_afterhours_scan` (712-908), not
`finalize_nextday_watchlist` (909+) — the two orchestrators are adjacent in
the file and easy to mix up by line number alone.

## Verification status — same caveat as sessions 76/77/82c/86/91

**This sandbox still has no network access**, so these 3 new tests were
**not run** — written and hand-traced against the actual current source
(including manually executing the stdlib `fromisoformat`/`strptime` calls in
isolation to confirm gap #3's exception-type reasoning, and re-reading
`run_afterhours_scan`'s `best` dict construction order to confirm RELIANCE
really is processed before TCS in gap #4's test). Run for real before
trusting the result:

```bash
cd services/real-trade-service
python -m pytest tests/test_afterhours_extract_symbol.py tests/test_afterhours_scan_pure_helpers.py tests/test_afterhours_scan_orchestration.py -v
python3 -m pytest -q --cov=. --cov-report=term-missing
```

Expected: `watchlist_engine/afterhours_scan.py` at 100% (all of 390, 494,
866-872 closed); `373` in the orchestration test file remains listed as
missing and should stay that way (see point 1 above — not a bug).

No production code was changed this session — tests only.
