# E42 - watchlist "moved N % since added" alert, server-side baseline (E28 second half, 2026-10-23)

**Alert only.** Nothing places, blocks or sizes a trade from it.

`services/notification-scheduler-service/scheduler/watchlist_moves.py` (runs inside the scheduler, next to the E29 digest):
- Every `WATCHLIST_MOVE_CHECK_MIN` (15) minutes, weekdays 09:20-15:35 IST, it reads the gateway `/watchlist` and `/quote/{sym}`.
- First sight of a symbol = its **server-side baseline** (source `first_seen_server`, so within one check of adding it). A removed symbol forgets its baseline.
- A move of `WATCHLIST_MOVE_ALERT_PCT` (5) % or more either way sends ONE message (Telegram / Discord / Slack as configured) listing symbol, % since added, baseline -> now. After an up-alert that symbol stays quiet until the move falls back inside half the threshold; a swing the other way alerts at once.
- State `<SCHEDULER_STATE_DIR>/watchlist_moves_state.json` (the `/data` volume). A symbol is marked alerted only after the notification service answered 200, otherwise the alert is retried on the next check.
- `GET /scheduler/watchlist-moves/status`; `POST /scheduler/watchlist-moves/run?dry_run=true` lists what would be sent (default), `dry_run=false` sends.
- Switches (env, read at call time): `WATCHLIST_MOVE_ALERTS_ENABLED` (true), `WATCHLIST_MOVE_ALERT_PCT` (5), `WATCHLIST_MOVE_CHECK_MIN` (15), `WATCHLIST_MOVE_MAX_SYMBOLS` (80).

Limits: the browser card's "% since added" keeps its own per-device baseline, so it can differ from the alert's by the minutes between add and first check. The switches are env-only (same reason as the PATTERN_ALERTS ones: the scheduler has no calibration-page control routes). Nothing is sent while the market is closed.
Tests: `services/notification-scheduler-service/tests/test_watchlist_moves.py` (7, pass in the sandbox without pytest). Not run: the live scheduler, real quotes, a real notification.
