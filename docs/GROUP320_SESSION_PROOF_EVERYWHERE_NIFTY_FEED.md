# Group 320 (2026-10-11) - closed-market check in every service, Nifty feed, first main.py split

Cumulative on group 319.

## 1. The price-based closed-market verdict now reaches every service
Group 319 only gated the two trading services and the market-data feeds. Now also:
- notification-scheduler-service: `scheduler/run_once.py` `is_holiday()` (a verdict-closed weekday skips the scan; `MARKET_DATA_URL` added to its compose block).
- api-gateway: `nse_holidays.is_nse_holiday()` / `holiday_name()`. Every gateway caller (universe TTL, scan gating, session phase, data_feed
  open checks, next-trading-day maths) goes through it, so one change covers them all. Only TODAY (IST) is ever judged by prices.
- analysis-intelligence-service: `technical/main.py` `is_market_open()` (cache TTL).
- decision-prediction-service: `decision/main.py` `_is_market_open()` (cache TTL).
- `session_proof_client.py` is now six identical copies; `scripts/check_shared_copies.py` enforces it (IDENTICAL family).
- Same fail-open rules: no verdict, a network error, or a verdict for another day = not closed. Off with `SESSION_PROOF_CLIENT_ENABLED=false`.

## 2. Nifty is fed into the proof
- `session_proof.probe_nifty()` reads the Nifty 50 price at most every `SESSION_NIFTY_PROBE_S` (60 s) while it can matter
  (inside 09:15-10:00, or while the day is judged closed so a later move still reverses it). market-data `main.py` registers
  the fetcher (`_session_nifty_price`, its own `/quote` path for `^NSEI`); the AngelOne feed loop calls the probe each cycle.
- Safety change: Nifty ALONE may close the day only after `SESSION_NIFTY_MIN_OBS` (3) unchanged in-window readings. One stale
  cached index quote can no longer close a day (previously a single Nifty observation was enough, which was harmless only while nothing fed it).
- `/market/session` now also reports `nifty_readings`.

## 3. Muhurat trading, Sunday 2026-11-08 - decided, not modelled
NSE confirmed a one-hour special session that day; the timing is still to be notified by circular. Stockky has no intraday logic
for it (09:15-15:30 windows, 15:20 square-off, opening gate), so Sunday = closed, nothing runs. This is now pinned by
`tests/test_muhurat_sunday_closed.py` in both trading services. Revisit when NSE publishes the timing if you ever want to trade it.

## 4. api-gateway main.py split, first step
`pick_ranking.py` (value-adjusted top-pick ranking: `VALUE_*`, `_value_adjusted_score`, `_select_top_picks`, `_board_rank_key`)
moved out verbatim; main.py re-exports the same objects. Chosen because it has no I/O, no state and no test patches through `main`.
Deliberately NOT moved: anything tests patch via `main` (datetime, httpx, Redis helpers, `_send_scan_notification`,
`_estimate_holding_period`) - a plain move would silently stop those patches working. The remaining split needs the tests
changed to patch the new modules first; do it one block at a time.

## Still not done
- 2027 NSE holidays: NSE publishes in December; add to all 7 copies then (`scripts/check_holiday_lists_sync.py` warns from 15 Nov).
- Dhan depth20 / order-WebSocket frame layouts: need real frames from your account.
- Rest of the main.py split (see 4).
