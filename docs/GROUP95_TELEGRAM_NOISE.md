# group95 (2026-10-04) - item 19: Telegram messages on every boot and every after-hours scan

Cumulative on group94. In `real-trade-service` run `bash run_tests.sh`, then `docker compose build real-trade-service && docker compose up -d`.

## Cause (three sources, all in real-trade-service)
- **After-hours scan, every tick:** `watchlist_engine/afterhours_scan.py::run_afterhours_scan` sent a Telegram message at the end of every pass, for both DEMO and REAL, including "No new rows - N symbol(s) already at best score" when nothing had changed. The loop also runs one tick right after each service boot, so every restart produced two such messages (the notifier's dedup only covers 5 minutes).
- **Finalize, twice:** at 08:45 `finalize_nextday_watchlist` sends the detailed "After-hours watchlist FINALIZED" message, and `execution/auto_pilot.py::_afterhours_scan_body` then sent a second, shorter "finalized" message with the same symbols (different text, so dedup did not catch it).
- **Stale constants, every boot:** `adaptive_thresholds.startup_staleness_warning()` sent "Stale trading thresholds detected" on every start (every deploy and restart) for as long as any regime constant was older than 30 days.

## Fix
- **Scan:** a SCHEDULED scan messages only when it wrote or updated rows. A MANUAL scan (the button / `run_afterhours_scan_manual`) always replies. A pass that wrote nothing because saves failed is reported once per (mode, date, failed-count) instead of every tick (in-memory, bounded; a restart can repeat it once). `run_afterhours_scan` gained `manual: bool = False`; `_afterhours_scan_body` passes it through. Logging is unchanged and says when the message is skipped.
- **Finalize:** the second message from `auto_pilot` is removed; the detailed one from `finalize_nextday_watchlist` stays. The shortlist itself is unchanged.
- **Stale constants:** the notice is sent when the set of stale constants changes (or a value or review date changes), otherwise at most once per `STALE_NOTICE_INTERVAL_HOURS` (default 168 = weekly). The warning log lines still appear on every boot. State is a small file, `STALE_NOTICE_STATE_PATH` (default `/tmp/stockky_stale_notice.json`). It survives a container restart but not a container re-create, so a fresh deploy sends one message. A failed send is not recorded, so the next boot retries. Any problem reading the file means "send".

## Not changed / not verified
- **Other Telegram senders:** I looked only at the boot and after-hours paths in real-trade-service. Other messages (auto-pilot cycle summaries, order events, the 15-minute intraday news check, error alerts, position-stocks and gateway sends) were not reviewed or throttled. If you still see a repeating message, send me its first line and I will trace it.
- **Item 25 itself:** the six regime constants are still 30+ days old. This only stops the repeat messages; it does not review or update the constants.
- **First scan after a long quiet period:** if a scan writes rows you will still get that message, as before. A restart no longer re-sends a list for rows that are already stored.
- Not run under real pytest here (the sandbox has none and no sqlalchemy). The throttle and gating helpers were checked standalone and all changed files compile. Run `bash run_tests.sh` in `real-trade-service` on the VM.

## Tests
- `tests/test_adaptive_thresholds.py`: `TestStaleNoticeThrottle` (no state, same set, after the interval, changed set, order, corrupt state / clock going backwards, unwritable path, two boots send once, failed send retries, log lines kept). An autouse fixture gives each test its own state file, so the existing staleness tests do not touch `/tmp`.
- `tests/test_afterhours_scan_orchestration.py`: `TestScanTelegramGating` (wrote rows notifies, repeat scan silent, manual always replies, empty scan silent unless manual) and `TestShouldNotifyScan` (once per mode/date/count, bounded memory).
- `tests/test_auto_pilot_orchestration.py`: the finalize test now expects no second message from the orchestrator; new test that scheduled scans pass `manual=False` and manual runs pass `True`.
