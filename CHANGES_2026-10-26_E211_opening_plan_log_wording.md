# E211 - position-stocks: the "[opening-plan] built" log line no longer says "0 of 150 have a volatility"

**Where:** services/position-stocks-service/screening/opening_plan.py, `build`. Noted-not-changed item from E185.
**Problem:** after a restart inside 09:10-10:00 the previous-day cache can still be cold, so the line read "built: 0 of 150 symbols have a previous-day volatility" - which sounds like no plan and no gating. That is not what happens: `prev_day.get()` starts a background fetch for each miss and `bar_atr_pct()` reads the cache (peek) on every lookup in the window, so symbols are served as the cache fills.
**Fix (log text only, no behaviour change):** "built: N of M symbols planned from the previous-day cache; K not cached yet (fetching in the background, read from the cache per lookup as they arrive)"; the clause is omitted when everything was cached.
**Tests:** tests/test_e211_opening_plan_log_wording.py (4). The real `build()` was run standalone and printed both wordings; the pytest file itself was NOT RUN (pytest/SQLAlchemy not installed here), py_compile only.
