# Group 397 - real-trade excursion (peak / trough since entry) and stop diagnostics

Completes the real-trade side of group 395 / 396. Evidence only: no entry, stop, size or exit decision changes.

## Why
Group 396 tagged where each real-trade stop came from, but judging a stop needs the highest and lowest price each trade actually reached. Position-stocks has had that since 2026-10-02; real-trade had nothing.

## Change
- `exit_engine/excursion.py` (new): `record(db, position, tick, now)` widens `position.max_price_seen` / `min_price_seen` with the tick price. The entry price counts as a price the trade has been at. It writes (one commit) only when a new extreme is reached, ignores a tick older than `EXCURSION_MAX_TICK_AGE_S` (default 120 s) when its age is known, and never raises (a failure rolls back and is logged at debug).
- `exit_engine/exit.py` `evaluate_mode`: one call per open position per cycle, right after the price is read and BEFORE any exit check, so the price that triggers a stop is itself recorded. Runs for DEMO and REAL.
- `models.py` / `db.py`: `trade_positions.max_price_seen`, `min_price_seen` (nullable float), added on boot by the existing `_ensure_cost_model_columns` migration (Oracle `BINARY_DOUBLE`, Postgres / SQLite `FLOAT`). Older rows stay NULL.
- `trade_records.py`: each record gains `max_gain_pct` and `max_drawdown_pct` (vs the average entry price; None when unknown, never 0).
- `trade_records.stop_diagnostics()`, embedded as `stop_diagnostics` in `GET /positions/{mode}/report`, per stop basis (and `all`), over records that carry their stop AND an excursion:
  - `stop_outs` (exit reason `stop_hit`, `broker_stop` or `emergency_gap_down`), and how many of them had first run at least 0.5R in favour;
  - survivors: other trades whose drawdown came within 0.2R of the stop;
  - `tighter[k]` for k = 0.5 and 0.75: trades whose drawdown reached k x their stop (a tighter stop would have fired), how many of those were not stop-outs, and how many of those were winners (the winners a tighter stop would have killed).
- Env: `EXCURSION_MAX_TICK_AGE_S` (120). Documented in `.env.example` and `docs/ENV_REFERENCE.md`.

## What it cannot do
- Sampled once per exit cycle: a spike between two cycles is missed, so the peak / trough are lower / shallower bounds, not exact.
- No profit-and-loss what-if for a tighter stop (a record has no quantity); counts only. The replay tool (`scripts/opening_replay.py`) has the P&L version and, since group 398, reads real-trade records too.
- A corporate-action price jump (split, bonus) is not filtered; read a trade with an absurd gain or drawdown by hand.
- Positions held overnight accumulate the extremes over the whole holding period, including the gap.
- Only positions opened after this build have an excursion; older and broker-imported rows give None and are left out of `stop_diagnostics`.
- Small samples: counts below the report's `min_trades_note` are noise. No significance test.

## Use
`GET /positions/REAL/report?days=14` -> `stop_diagnostics.groups.<atr|flat|structure|unknown|all>`. `GET /positions/REAL/records` -> `max_gain_pct`, `max_drawdown_pct` per trade.

## Deploy note
Two new nullable columns on `trade_positions`, added on the next real-trade boot (logged as "real-trade-db: added trade_positions.max_price_seen" and `min_price_seen`). Nothing else to do.

## Tests
- `tests/test_group397_excursion.py` (pure, 24 cases + 1 env test): extreme logic, record (new extreme, no change, old tick, unknown age, env limit, naive time, commit failure, bare / garbage position), percentages, record fields, stop diagnostics (indicators, tighter-stop counts, unusable rows, unknown basis, report embedding).
- `tests/test_group397_excursion_db.py` (DB-backed, 7): exit loop keeps both extremes across cycles, the stop-trigger price is recorded, stale tick ignored, a recorder failure never stops an exit, values reach the trade record, model columns, migration on an old table (idempotent).
- Verification status: NOT run under pytest (no pytest / sqlalchemy in the sandbox). The pure file ran as plain Python (24 cases + the env test) and passes; the DB-backed file is unrun.
