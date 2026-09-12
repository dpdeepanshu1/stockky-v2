# Session 30 — 2026-09-12 — Auto-Pilot gate-off alert + data_feed.py spot-check

## Investigated: "Auto-Pilot not evaluating exits — DEMO" notification

User asked whether this alert was correct/normal or a bug. Traced every path
that can flip a mode's `armed` or `auto_pilot_enabled` to False:

- Dhan token-expiry auto-disarm (`main.py::_check_and_expire_gates`) — hard
  gated to `mode == "REAL"`.
- Dhan live-token-rejected / invalid-IP auto-disarm
  (`auth/dhan_credentials.py::enforce_live_token` /
  `disarm_on_invalid_ip`) — every call site (`cycle_runner.py`,
  `entry_engine/entry.py`, `manual_engine.py`, `exit_engine/exit.py`) is
  itself nested inside an `if mode == "REAL":` block, so these never fire
  for DEMO.
- Manual `/disarm/{mode}`, `/emergency-pause`, `/autopilot/{mode}/disable`
  — all explicit user/admin actions.

**Conclusion: the alert is accurate.** DEMO's gate/auto-pilot can only go
off via an explicit action, never a background bug — so if you got this
alert, something (a disarm, an emergency-pause, or an auto-pilot toggle)
really did turn it off while DEMO still had open positions, and stops
genuinely were not being evaluated. This is the alert doing its job.

## Fixed: alert wording was misleading for DEMO

The alert text unconditionally said "Re-authenticate and re-arm" for every
mode. Re-authentication (Dhan token/session) is a REAL-only concept — DEMO's
`/arm` route has no auth checks at all (see main.py's `arm()`: "DEMO: no
gate checks at all"). Telling a DEMO user to "re-authenticate" when there is
nothing to authenticate is confusing and was very likely part of why this
notification looked wrong. `execution/auto_pilot.py::_alert_if_open_positions_while_gate_off`
now says "Re-arm and re-enable Auto-Pilot" for DEMO and keeps
"Re-authenticate and re-arm" only for REAL.

## data_feed.py spot-check

Read `compute_rsi_from_closes` (standard Wilder-style RSI, correct) and
`patch_feed_price` (surgical price-field patch, correct). The 600-line
`merge_feed_payload` and the bulk Yahoo/NSE-bhavcopy fetch paths are not
yet read end-to-end.

**Still genuinely unaudited:** `merge_feed_payload` (data_feed.py) and the
bulk price-feed functions, decision-prediction-service's `training/` and
`prediction/` subtrees, and the frontend.
