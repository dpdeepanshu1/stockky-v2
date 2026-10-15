# Group 392 - opening protection (position-stocks)

## Change
- `orders/opening_protect.py` (new). Window: trading day, 09:15 to `OPENING_PROTECT_UNTIL_IST` (09:45).
  - `size_mult()`: `OPENING_SIZE_MULT` (0.5, clamped 0.1-1.0) inside the window, else 1.0.
  - `pause_reason(db)`: when `OPENING_PAUSE_STOPOUTS` (2) or more positions have closed as STOP_HIT / STOP_BREACH_EXIT since today's 09:15 bell, the automatic entry is skipped with `OPENING_PROTECT:PAUSED:...` until the window ends. Counted from the positions table, so a restart does not reset it. 0 = no pause.
- `orders/entry.py` `attempt_entry`: the pause is checked right after the margin / symbol pause; the size cut sits next to the VIX and calendar cuts (nearest share, never under 1; the unused reservation goes back to the pool). Manual BUYs are not touched.
- `tests/conftest.py` switches it off for older tests (time-dependent), like the opening gate.
- `OPENING_PROTECT_ENABLED=0` turns both off. Both fail open (DB error or bad clock = full size, no pause).

## SL-M: no code change
- position-stocks has no separate stop order to switch: the stop is the Super Order's stop-loss leg, sent at entry and left as it was.
- real-trade's broker stop already defaults to `BROKER_STOP_ENABLED=true` and `BROKER_STOP_ORDER_TYPE=STOP_LOSS_MARKET` (config.py). Only if Dhan rejects that does group 384 retry once as STOP_LOSS with a limit below the trigger. Check your live `.env` has not pinned `BROKER_STOP_ENABLED=false` (it was false on 2026-10-12).

## Limits
- 0.5 and 2 are judgement values, not backtested. The pause counts stop-outs of any entry, not only opening entries.
- Real-trade did not have these yet when this was written; group 394 added them (`docs/GROUP394_REAL_TRADE_OPENING_PROTECTION.md`).

## Tests
`tests/test_group392_opening_protect.py` (window, size, pause) plus `TestOpeningProtectWiring` in `tests/test_entry.py`. position-stocks suite 3799 passed.
