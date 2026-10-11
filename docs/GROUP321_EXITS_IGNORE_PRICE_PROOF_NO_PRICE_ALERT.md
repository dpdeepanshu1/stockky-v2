# Group 321 (2026-10-11) - stops never wait on the price-based "closed" verdict, verdict logging, Dhan + own Nifty thread, no-price alert

Cumulative on group 320.

## 1. The price-based "closed" verdict now stops only NEW entries (safety fix)
Group 319/320 made `tz_utils.is_market_open_ist()` return False when market-data says "no stock/Nifty moved 09:15-10:00".
real-trade-service's fast exit loop and its EOD square-off start with that check, so a WRONG verdict (a stale feed that
repeats the same prices) would have switched off protective stops and the 15:00 square-off for open positions (real-trade
stops are software-only and positions are CNC, up to 10 days). Now:
- `is_market_open_ist(now=None, honor_session_proof=True)` (real-trade-service and position-stocks-service `tz_utils.py`).
  `honor_session_proof=False` = calendar + clock only.
- real-trade `execution/auto_pilot.py`: the exit-only tick and the EOD square-off use `honor_session_proof=False`.
  Entries (full tick, enter-at-open, EOD signal scan) still honour the verdict.
- position-stocks `main.py`: the fast reconcile loop and the trading loop use `honor_session_proof=False` (exit reconcile and
  EOD square-off keep running); `_run_cycle` (trigger AUTO) checks the verdict itself before screening and skips with
  `MARKET_CLOSED_PRICE_PROOF`, so no new entry is placed on a verdict-closed day.
- 60+ existing test fakes `lambda: True/False` for `is_market_open_ist` became `lambda *a, **k:` (the call now passes a keyword).

## 2. Verdict changes are logged (market-data `session_proof.py`)
`session-proof` logger: every change of status is logged once; `-> closed` and `closed -> open` are WARNING, `open`/`unknown` INFO.

## 3. Dhan and Nifty feed the proof
- `dhan_data/live_poller.py` calls `session_proof.observe()` for every Dhan row (Dhan is the first price source; only the
  AngelOne / Yahoo websockets fed the proof before).
- New `session_proof.start_probe_thread()` (started from market-data `main.py`, skipped under pytest): the Nifty probe runs on
  its own daemon thread, no longer only inside the AngelOne feed loop. `SESSION_NIFTY_THREAD=false` turns it off.

## 4. No-price alert for held positions (real-trade-service `exit_engine/exit.py`)
A held position whose quote cannot be read gets a HOLD note and NO stop check. After `EXIT_NO_PRICE_ALERT_CYCLES` (6, about a
minute at the 8 s interval) cycles in a row: WARNING + one Telegram alert per symbol per `EXIT_NO_PRICE_ALERT_COOLDOWN_S` (900).
`EXIT_NO_PRICE_ALERT_CYCLES=0` = off. Streak resets on the first price.

## Tests
`real-trade-service/tests/test_group321_exits_ignore_price_proof.py`, `position-stocks-service/tests/test_group321_cycle_ignores_price_proof.py`,
`market-data-service/tests/test_group321_session_proof_logging_dhan_probe.py`.

## Still not done (see the session reply for the ranked list)
- Broker-side stop-loss for real-trade CNC positions (software stop, 8 s polling, market sell).
- DEFAULT_RISK_PER_TRADE_PCT (5) above DEFAULT_MAX_DAILY_LOSS_PCT (3): harmless while MAX_TRADE_VALUE (3000) binds.
- Strategy settings that need your own 14-day reports: no entries before 10:30, carry-overnight bar vs the 0.71% delivery cost,
  BOARD_GATE_MODE=enforce, ENTRY_LIMIT_AT_TOUCH.
- Depth20 / order-WebSocket frame layouts (check Monday 09:30 IST); 2027 NSE holidays (December); api-gateway main.py split;
  ~400 silent `except Exception: pass`; decision-prediction coverage.
