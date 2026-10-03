# Market-hours news check (2026-10-04, user request)

Problem: the after-hours news scan cadence (15:45 -> 21:45 -> 03:45 -> 08:00/08:30/08:45 -> 09:00) then
slept through 09:00-15:45, so news that broke during the session was never seen.

Change: a second, very light loop (`auto_pilot._intraday_news_loop`) + `watchlist_engine/intraday_news.py`.
- Every `INTRADAY_NEWS_INTERVAL_SECONDS` (default 900 = 15 min, floor 300) on trading days 09:00-15:45 IST
  (weekends + NSE holidays skipped), on a worker thread with a skip-if-busy lock.
- Same RSS feeds / symbol extraction / keyword scorer as the after-hours scan. No quote APIs, no api-gateway call.
  Only NEW headlines (in-memory seen-set) published within `INTRADAY_NEWS_MAX_AGE_MINUTES` (180) are scored;
  undated headlines are skipped; generic tickers (IT, ENERGY, GOLD, ...) ignored; skipped if symbol master unavailable.
- Never places/injects/removes a candidate. Stores a capped per-symbol nudge (`intraday_news:v1` resilience snapshot,
  max 80 symbols, expires after the max age). Gate 6 ranking in entry_engine adds it to candidates already queued:
  + up to `INTRADAY_NEWS_BONUS_CAP` (8) for fresh positive news, - up to `INTRADAY_NEWS_PENALTY_CAP` (10) for
  fresh negative news (probe, downgrade, fraud...). Shown on the PLACED event as `[news +x.x]`.
- Telegram alert: any hit on a queued candidate, plus positives scoring >= `INTRADAY_NEWS_ALERT_MIN_SCORE` (45).
- Uses the existing `gate.afterhours_news_scan_enabled` toggle (no new toggle / migration). `/status` shows
  `intraday_news_window_ist` and `intraday_news_interval_seconds` under scheduled_automation.afterhours_news_scan.

Env knobs (all blank-safe): INTRADAY_NEWS_START_IST, INTRADAY_NEWS_END_IST, INTRADAY_NEWS_INTERVAL_SECONDS,
INTRADAY_NEWS_MAX_AGE_MINUTES, INTRADAY_NEWS_MIN_SCORE, INTRADAY_NEWS_BONUS_CAP, INTRADAY_NEWS_PENALTY_CAP,
INTRADAY_NEWS_ALERT_MIN_SCORE.

Tests: tests/test_intraday_news.py (new) + TestIntradayNewsNudge in tests/test_entry_evaluate_mode_remaining_coverage_2.py.
