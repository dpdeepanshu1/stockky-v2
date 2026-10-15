# Group 394 - opening protection (real-trade)

Real-trade counterpart of group 392. Same two guards, adapted to how this service sizes and records trades.

## Change
- `entry_engine/opening_protect.py` (new). Window: trading day (weekday, not an NSE holiday), 09:15 to `OPENING_PROTECT_UNTIL_IST` (09:45), for the modes in `OPENING_PROTECT_MODES` (default `REAL,DEMO`; set `REAL` to keep DEMO as an unprotected control).
  - `size_mult(mode)`: `OPENING_SIZE_MULT` (0.5, clamped 0.1-1.0) inside the window, else 1.0.
  - `pause_reason(db, mode)`: when `OPENING_PAUSE_STOPOUTS` (2) or more positions have closed as a **losing stop-out** since today's 09:15 bell, no new automatic entry is evaluated until the window ends. 0 = no pause.
  - A stop-out is a position fully CLOSED (event `CLOSED` in `trade_position_events`) with reason `stop_hit` (the bot's own stop), `broker_stop` (the resting Dhan stop, group 384) or `emergency_gap_down`, and negative realized P&L. Counted from the database, so a restart does not reset it.
- `entry_engine/entry.py` `evaluate_mode`:
  - Pause: checked right after the opening guard and before the candidate loop. It returns `{..., "opening_protect": reason}` with the candidates **left queued** (not consumed, not rejected, no decision rows), exactly like the opening guard, so they are evaluated normally once the window ends. The log line is throttled to one per 5 minutes per mode.
  - Size: applied to the risk-sized quantity right after the "even 1 share exceeds the risk cap" check (nearest share, a 1-share quantity is unchanged, never below 1). Because it cuts the quantity rather than the risk %, a 1-share trade that fits the full risk cap is not turned into a WAIT. The risk engine, cost gate and order sizing see the cut quantity.
- `config.py`: `OPENING_PROTECT_ENABLED`, `OPENING_PROTECT_UNTIL_IST`, `OPENING_SIZE_MULT`, `OPENING_PAUSE_STOPOUTS`, `OPENING_PROTECT_MODES`. Position-stocks and real-trade read the same names, so one value in `.env` sets both services (compose passes `.env` to both via `env_file`). `OPENING_PROTECT_MODES` is read by real-trade only.
- `tests/conftest.py` switches it off for older tests (time-dependent), like the opening guard and gate.
- Manual orders (`manual_engine`) are not touched. Fails open: a DB error, bad clock or bad config means full size and no pause. `OPENING_PROTECT_ENABLED=false` turns both off.

## How it differs from position-stocks (group 392)
- The pause counts **losing** stop-outs only. Position-stocks counts every STOP_HIT / STOP_BREACH_EXIT status; here a trailing or breakeven stop that closed in profit is not a failed entry, so it does not pause real money. The replay tool (`scripts/opening_replay.py`) uses the position-stocks definition (any stop status), so its stop-out counts can be slightly higher than what this service would have counted.
- The pause leaves candidates queued instead of logging a SKIPPED row per candidate.
- The size cut sits after the risk sizing (risk % x equity / per-share risk) instead of after a capital reservation, because real-trade sizes by risk, not by a reserved pool.

## Interaction with the other opening rules
- The opening guard (group 220, from 09:15) and opening gate (group 268, until 10:00) run first and are unchanged. The size cut is applied only to a candidate that has already passed every gate; the pause only stops evaluation.
- The broker stop (groups 384/385) is a different mechanism and is unchanged: it still protects an open position. A `broker_stop` fill is one of the events that counts toward the pause.

## Limits
- 0.5 and 2 are judgement values, not backtested. Use `scripts/opening_replay.py` on your own ledger exports to see what they would have done; it needs 30+ opening trades before it gives a verdict, and it replays the position-stocks ledger export (the `/trades/history` route), not a real-trade export.
- Half size on the real account also halves a winning opening trade. Nothing here shows the opening trades lose on average; the evidence is the earlier 1-of-10 and 0-of-5 samples, which are small.
- Only stops with a negative P&L and one of the three reasons above count. A position closed for another reason (target, time stop, structure exit, manual) never counts.
- Overnight carried positions that gap down and are closed at 09:15 by `emergency_gap_down` or the broker stop with a loss DO count, because they are stop-outs since the bell even though the entry was yesterday.

## Tests
`tests/test_group394_opening_protect.py` (37): window bounds, weekend / holiday, mode coverage, clamps, quantity rule, stop-out counting (all three reasons; ignores profit, partial, other mode, before the bell, yesterday), pause threshold, and `evaluate_mode` wiring (pause leaves candidates queued and they enter after 09:45; half quantity inside the window, full after it; mode and switch off). Module coverage 100%. real-trade suite 4748 passed, 1 skipped.
