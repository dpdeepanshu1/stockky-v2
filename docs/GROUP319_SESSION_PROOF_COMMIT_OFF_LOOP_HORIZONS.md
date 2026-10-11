# Group 319 (2026-10-11)

## 1. Price-based "did the exchange trade today" check
Rule: weekday, 09:15-10:00 IST, no stock moves and Nifty unchanged -> today is a non-trading day.
- `market-data-service/session_proof.py` (pure stdlib). Feeds call `observe()`; `status()` returns open / closed / pending / unknown.
  closed needs >= SESSION_PROOF_MIN_SYMBOLS (10) observed (or Nifty) and zero moves; too little data = unknown, never closed.
  A closed verdict is overturned by the first real move later in the day.
- `GET /market/session` on market-data-service. AngelOne feed probes every SESSION_CLOSED_PROBE_S (300) while closed.
- `session_proof_client.py` (identical copy in real-trade-service and position-stocks-service): cached, refreshed by a daemon
  thread, fail-open. `tz_utils.is_market_open_ist()` returns False when closed (real-clock calls only).
- Env: SESSION_PROOF_ENABLED, SESSION_PROOF_START_IST, SESSION_PROOF_END_IST, SESSION_PROOF_MIN_MOVES, SESSION_PROOF_MIN_SYMBOLS,
  SESSION_CLOSED_PROBE_S, SESSION_PROOF_CLIENT_ENABLED/TTL_S/TIMEOUT_S.
- Not wired: notification-scheduler, api-gateway, analysis, decision. Nifty only counts if a caller feeds `observe("NIFTY", price)`.
- Groww NSE 2026 list checked against all 7 copies: identical (16 dates). 2027 still to add in December.
  Muhurat trading Sun 2026-11-08 is not modelled (Sundays are treated as closed).

## 2. real-trade-service: 13 operator routes commit off the event loop (`_commit_off_loop`); guard test added.
Engine ticks already run on worker threads (auto_pilot.py note 4).

## 3. Silent handlers: warnings added in exit.py (exit profile lookup, HOLD-note commit) and entry.py (risk-config read, held-symbol lookup).

## 4. decision-prediction-service/decision/horizons.py
- A real 0 (event score, RS, delivery, quality) no longer falls through `or` to a neutral default.
- News time-decay now runs BEFORE the weighted score (it used to change only the displayed pillar). Live decisions change slightly for stale news.
- tests/test_horizons.py (34 tests).

Not done: api-gateway main.py split; Dhan depth20 / order-WS layouts (need real frames); 2027 holidays.
