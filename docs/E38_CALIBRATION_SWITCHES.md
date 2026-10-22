# E38 - every switch on the Real Money Calibration page (2026-10-23)

Rule from now on: a new change ships with a test, a calibration switch where it makes sense, and live wiring (not log-only).

**Added to the page** (all read at call time, proved by `scripts/check_control_catalog.py`; every `shipped` value equals the config default; no behaviour changed):

| Service | Added |
| --- | --- |
| real-trade-service (+21) | Circuit / results gate, VIX gate, pre-open gap gate, calendar rules, negative-news block, carry context gate, global cues, regime gate, US sector signal, liquidity gate, volume-shock gate, best-of-cycle filter, shortlist, board nudge, cost model, **cost gate ratio**, pullback re-arm, exit DP guard, **risk per trade**, **max entries per day (A12)**, **account-wide daily loss cap (A11)** |
| position-stocks-service (+24) | Market gate (+ prev-close variant), circuit, VIX, news, calendar, pre-open, quality gate, cost gate, loss brake, window auto-disable, scan ranking v2, pullback re-arm, adaptive bar ATR, sizing rounding, trailing stop, no-follow-through exit, stop watchdog, stop verify, **account-wide loss cap**, **risk per trade**, **daily order budget**, **max open positions**, **journal notes (E30, new switch)** |

**Guard:** `tests/test_e37_catalog_coverage.py` (both services) fails when a `*_ENABLED` / `*_MODE` setting in `config.py` is neither on the page nor in a short EXEMPT list with a written reason (login, broker link, display-only). A new switch can no longer ship without its calibration entry.

**Not on the page, on purpose:** admin password change, Dhan TOTP, daily report, product type (CNC / MIS), EDIS morning check, shortlist panel, adaptive history cache, Dhan order websocket.

**Not done:** the notification-scheduler's `PATTERN_ALERTS_*` switches (the page only drives the two trading services), and `ENTRY_NO_AFTER_IST` / calendar cut-off times (the page has no time-of-day field type yet).
