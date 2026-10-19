# Group 447: Telegram alert + last-run timestamp for automatic runs, latest result only, run-window close icon, UI polish

Nothing here changes a trading decision, an order or a trading switch. The runner is still read-only toward trading.

## 1. Telegram message when an automatic run happens
- `services/calibration-runner-service/notify.py` (new). When the **timetable** starts a pack and it finishes (OK / LOOK / FAILED / TIMEOUT / CANCELLED), or the timetable fires but the market-hours guard **blocks** it, one plain-text message goes through the repo's own notification service (`POST <NOTIFICATION_URL>/notification/notify`, channel `telegram`). No token is needed in the runner.
- The message has: status icon + pack title, "Automatic run (timetable)", finish time IST and duration, one line per job with its status, the Service ON / OFF verdicts when the run produced them (`service_advice.json`), else up to 4 headline lines, and where to read the full text. Capped at 1500 characters, tokens redacted.
- Failures are urgency `high`. A failed or unreachable Telegram only leaves a note on the run (`notified`: `sent` / `not delivered: ...` / `error: ...`); it never changes the run's status.
- Runs you start with a button are NOT announced unless `RUNNER_NOTIFY_MANUAL=true`. `RUNNER_NOTIFY_ENABLED=false` switches the whole thing off. (Both in docker-compose, defaults true / false.)

## 2. Last automatic run timestamp on the page
- `GET /summary` and `GET /runs` carry `state.last_automatic` and `state.last_automatic_by_pack` (id, title, status, finished time in IST, Telegram result). It is kept in `<runner data>/state/last_automatic.json`, so it survives restarts and the replacing of old run folders (section 3).
- Page: a "last automatic run: 2026-10-19 16:09 IST (5 min ago) · Daily close pack · success · Telegram sent" chip under every sub-tab; the same line plus a per-pack "Last fired" column on Run plan > Optional timetable; "AUTO" badge and Telegram note on the run list.

## 3. Runs and downloads: only the latest result of each test is kept
- New `RUNNER_KEEP_LATEST_ONLY` (compose default **true**). When a job or pack finishes, older runs of the same job / pack are deleted (folder, txt, zip files), so the list is one line per test and disk use stays small.
- A failed / timed-out / LOOK run replaces the older result like any other (it is the newest truth). A **blocked** or **cancelled** run never replaces a real result; the newest one is kept as a note beside it, an older note is dropped. Queued / running runs are never touched. Runs of different jobs never replace each other.
- On the first start with the setting on, old duplicate run folders are cleaned once. `RUNNER_KEEP_LATEST_ONLY=false` brings the old history behaviour back (the 120 runs / 45 days limits still apply).
- Download what you need before running the same test again. A run window that was open on a replaced run says so instead of an error.

## 4. Page improvements (Real Money Calibration)
- Run window: a visible **close icon (X)** top right, plus Close at the bottom, Esc and click outside; the page behind no longer scrolls; wide window, taller output, Copy, Jump to end, job chips with status, finish time and Telegram note.
- Runs and downloads: status filter chips with counts, "automatic only", search, a coloured left edge per status, Open / txt / zip on each line, time in IST with "5 min ago", duration.
- Test cards show "Last result: 5 min ago (2026-10-19 16:09)".

## Tests
`services/calibration-runner-service/tests/test_notify.py`: 11 offline stdlib tests (message text, gating, urgency, redaction, fake HTTP server, announced / blocked / failing-notifier runs, last-automatic record, latest-only replace / blocked-and-cancelled rule / off switch / restart cleanup). `test_runner_engine.py` unchanged, 20 pass. `frontend/src/calibration.test.mjs` passes. The frontend was syntax-checked only (no node_modules, no network in the build container): run `npm run build` once.
