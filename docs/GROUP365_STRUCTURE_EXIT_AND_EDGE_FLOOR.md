# Group 365 - structure exit (real-trade), edge floor, board golden, PS_ overrides

Re-applies the work of the interrupted session on top of the group 363/364 zip. Everything below is LIVE by default.

## real-trade-service
- `exit_engine/structure_exit.py` (new, pure): watches the daily structure while a position is OPEN.
  - bearish signal = fresh bearish CHOCH / not-failed bearish BOS (<= 3 daily bars old) that formed on a bar AFTER the entry day.
  - `STRUCTURE_EXIT_MODE=tighten` (default): in profit + signal -> stop raised to max(entry, LTP - 0.75 x ATR). Ratchet only.
  - `STRUCTURE_TRAIL_ENABLED=true` (default): in profit, no signal -> stop follows the newest structural higher-low (`ms_stop`) once it is >= entry and >= 1 ATR below the price.
  - `exit` mode additionally sells an in-profit position on a signal; `log` records only; `off` disables.
  - A losing position is never touched; no / stale / split-affected structure = no opinion.
- `exit_engine/exit.py`: new step 3b before the breakeven step. It reads the structure CACHE only (`structure_gate.peek_summary`); a miss starts a background fetch (`structure_gate.warm`), so a slow gateway can never delay a stop check. Events `STRUCTURE_TIGHTEN` / `STRUCTURE_TRAIL`, exit reason `structure_exit`; any error is swallowed and the other checks run as before.
- `GET /structure/status/{mode}` now also returns `exit` (mode, counters, last verdicts).
- `STRUCTURE_EDGE_MIN_N` (default 100, was 20 hard-coded) is the finished-row floor for the edge / calibration reports (`structure_observation.report`).

## position-stocks-service
- `STRUCTURE_EDGE_MIN_N` (default 100) for its edge report too.
- `PS_<name>` env override for the shared names GATE_MODE, REQUIRE_DATA, BEARISH_BLOCK, MAX_CHASE_ATR, MAX_STRETCH_ATR, MIN_ROOM_ATR, so the scalper can stay looser than real-trade when both read one `.env`. Without a `PS_` value nothing changes.
- Legacy Market-Board golden baseline: `scripts/gen_ms_golden.py` -> `tests/golden/market_structure_v0.json` (41 seeded series). `test_group365_legacy_board_golden.py` freezes the legacy maths (the fallback when `BOARD_USE_MS_ENGINE=0` or the engine errors) and compares the engine adapter with it.
- No structure exit here on purpose: scalp positions exit on their bracket / trailing / EOD logic and a daily signal "after the entry day" only exists for carried-overnight positions, which already hold a broker stop.

## frontend
- `structureView.exitView()` + an "Open positions - structure exit" block in `StructureGateCard` (real-trade cards only). Edge card fallback floor 20 -> 100.

## Not done / needs you
- Training logs the pre-policy decision (decision-prediction), no decision-service reject observations, PREPARE recompute, `/analyze` `groups` vs `families`: untouched (not real-trade / position-stocks).
- Gates remain `enforce` by default as before; no backtest has been run on real history.
- Schedule `structure-refresh` (~16:00 IST) and `sniper-observe-eval` (~16:30 IST) yourself.
