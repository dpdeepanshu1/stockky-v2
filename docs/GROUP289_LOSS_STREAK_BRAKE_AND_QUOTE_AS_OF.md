# Group 289 - loss-streak brake in real-trade-service, and `as_of` / `age_s` on every market-data quote

## Why
1. 2026-10-09: nine stagnation exits and a few stop-outs happened back to back. position-stocks-service has paused entries after
   consecutive losses for a long time (`LOSS_BRAKE_*`); real-trade-service only had the 3 % daily-loss cap, which lets a bad
   morning bleed almost that much before anything stops.
2. Plan phase A: every price payload must say where it is from and how old it is, so a consumer can refuse a stale price instead
   of guessing from `fetched_at` (a naive-UTC string with a different meaning in different services).

## What changed

### real-trade-service - loss-streak brake
- `risk_engine/engine.py`: new check **#3b `loss_streak_pause`** (BUY only). After `RISK_LOSS_STREAK_MAX` consecutive realized
  losses today, new BUYs are rejected until `RISK_LOSS_STREAK_PAUSE_MINUTES` after the last losing close.
  Runs after global pause / market closed / daily loss, before the sizing checks. SELLs are never affected.
  - `RISK_LOSS_STREAK_MAX` (default `3`, `0` = off), `RISK_LOSS_STREAK_PAUSE_MINUTES` (default `45`; `0` or negative = never blocks).
  - Blank or unparseable env values give the default (they cannot crash the import).
  - `AccountState.loss_streak` (default 0) and `loss_streak_last_close_at` (default None) mean "no brake", so any construction
    site that does not fill them behaves exactly as before.
- `portfolio/portfolio.py`: `today_loss_streak(db, mode, now=None) -> (streak, last_loss_close_at)`.
  Counts CLOSED positions of the IST day, newest first; a close with P&L >= 0 or unknown ends the streak. Broker-imported holdings
  are skipped entirely (they were not entered by this system today, so neither a loss nor a win on one says anything about
  today's entries). Any error returns `(0, None)`: **fails open**, the brake must never break an entry cycle.
- Wired into `entry_engine/entry.py::_account_state` (live entries) and `main.py` `/risk-engine/check` (dry run, so the preview
  matches the live path).
- **Not wired on purpose:** `manual_engine.py`. A manual "Confirm BUY" is a human decision; the daily-loss cap and all sizing
  checks still apply to it.

### market-data-service - `as_of` / `age_s`
- `_quote_as_of_age(fetched_at)` -> `(as_of, age_s)`: `as_of` is timezone-aware UTC ISO, `age_s` is seconds since then, never
  negative, 0.1 s resolution. A naive string is read as UTC (never local time). Unreadable -> `(None, None)` = unknown, never fresh.
- `_restamp_quote(row)`: rewrites only `as_of` / `age_s`, **at response time**. Quote rows are cached with the age they had when
  stored; without this a minute-old cached price would still say `age_s: 0.2`. `fetched_at` is never touched.
  Rows with no timestamp and no derived fields are returned unchanged.
- Applied in `_pad_quote_response` (so every payload, including `_failed_quote_payload`), in the `/quote/{symbol}` wrapper and in
  the `/quotes/bulk` wrapper (all rows, including the stale-served ones). `QuoteResponse` gained `as_of` and `age_s`.
- Additive only: no existing field changed. Consumers that ignore the new fields behave as before.

## Tests
- `real-trade-service/tests/test_group289_loss_streak.py` (34): engine boundaries and ordering, real-SQLite `today_loss_streak`
  incl. IST midnight and imported holdings, and `entry._account_state` -> `evaluate` end to end.
- `market-data-service/tests/test_group289_quote_as_of.py` (26): parsing forms, cache restamp, bulk error shapes, and the real HTTP
  endpoints (the response model must not drop the new fields).
- Suites after the change: real-trade-service 3916 passed, 1 skipped (`test_db_postgres_live.py` needs `pgserver`); 34 of them are new.
  market-data 1565 passed (1539 before + 26 new).
  market-data still has the same 2 failures it had before this group, both thread-scheduling assertions:
  `test_group270_dhan_core.py::TestBatcher::test_many_concurrent_single_symbol_callers_share_one_upstream_call` (60 callers made 2
  upstream calls, expected 1) and `test_angelone_feed_batch_writes.py::test_poll_cycle_writes_one_batch_per_angelone_batch_not_one_row_per_symbol`
  (chunk write order `[50, 20, 50]`, expected `[50, 50, 20]`).

## Rollout
- Turn the brake off at any time with `RISK_LOSS_STREAK_MAX=0`; no code change needed.
- Watch the `risk_events` table / dashboard for `loss_streak_pause` rejections on the first live day.
- Not yet done: no consumer refuses a price by `age_s` yet. That is the next step of plan phase A (default limit about 10 s for entries).
