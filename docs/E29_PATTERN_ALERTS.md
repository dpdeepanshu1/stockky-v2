# E29 - watchlist chart-signal digest (E36, 2026-10-23)

**Alert only.** Nothing places, blocks or sizes a trade from this. The signals have not passed the B-tools, and every digest says so.

| What | Detail |
| --- | --- |
| Signals | confirmed chart pattern (breakout / breakdown), golden / death cross (SMA50/200 or SMA20/50, in the last 2 daily bars), Bollinger squeeze on, squeeze released up / down |
| Universe | the gateway `/watchlist` (first `PATTERN_ALERTS_MAX_SYMBOLS`, default 80). Open positions are not included yet |
| Source | the `events` list of `GET /stock/{sym}/chart-context?tf=1d` (new, uncapped; older payloads fall back to `badges`) |
| When | weekdays at / after `PATTERN_ALERTS_TIME` (16:05 IST), once a day. A failed run retries every 10 minutes |
| Message | one digest through `/notification/notify` (Telegram / Discord / Slack / CallMeBot as configured), at most `PATTERN_ALERTS_MAX_LINES` lines |
| No repeats | quiet period per signal: breakout 10 days, cross 5, squeeze 5, release 3. State file `<SCHEDULER_STATE_DIR>/pattern_alerts_state.json` (the compose `/data` volume). A signal is marked sent only after the notification service answered 200 |
| Holidays | no holiday list: on a closed weekday the bars are unchanged, so the quiet periods suppress a repeat |

Try it: `POST <scheduler>/pattern-alerts/run?dry_run=true` shows the digest without sending; `dry_run=false` sends it. `GET <scheduler>/pattern-alerts/status` shows the last run.

Switches: `PATTERN_ALERTS_ENABLED` (true), `PATTERN_ALERTS_TIME`, `PATTERN_ALERTS_MAX_SYMBOLS`, `PATTERN_ALERTS_MAX_LINES` (see `.env.example`). These are read at call time through a helper, so `docs/ENV_REFERENCE.md` does not list them.

**Not done:** alerts for open positions, an on/off toggle in the UI, and the "% since added" watchlist notification (E28). The notification service reports 200 even when it queued the message in its outbox because no channel is configured, so "sent" means "handed to the notification service".
