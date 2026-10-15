# Group 396 - real-trade stop basis in the per-trade records

Real-trade counterpart of group 395 (which did position-stocks). Evidence only: no entry, stop, size or exit decision changes.

## Why
Group 395 tags where each position-stocks stop came from and exports it. Real-trade had nothing equivalent, so its ATR stops, flat fallback stops and structural stops could not be told apart in a ledger.

## Change
- `models.py` / `db.py`: `trade_decisions.stop_basis VARCHAR(16)`, nullable, added on boot by the existing `_ensure_cost_model_columns` migration (Oracle `VARCHAR2(16)`, Postgres/SQLite `VARCHAR(16)`). Older rows stay NULL. The tag lives on the entry decision (not the position) because the per-trade records already link a trade to its entry order's decision, so no extra column or threading through the order and position paths was needed.
- `entry_engine/entry.py`: `decision.stop_basis` is set when the stop is computed: `atr` (daily ATR x multiplier), `flat` (no usable ATR, so the flat % fallback), and `structure` when the group 351 structural stop replaces it (only when that stop passed its sanity check and `STRUCTURE_STOP_MODE=on`). A structural stop that is rejected as insane keeps the ATR tag.
- `trade_records.py` (feeds `GET /positions/{mode}/records` and `/report`): each record gains `stop_pct` (initial stop distance as % of the entry price; from `initial_stop_distance`, fixed at the opening fill and never trailed, else from the decision's proposed price and stop) and `stop_basis`. The expectancy report gains `by_stop_basis` (atr / flat / structure / unknown). No new route, no new env variable.

## Use
`GET /positions/REAL/report?days=14` now shows expectancy, win rate, payoff and average hold per stop basis. `GET /positions/REAL/records` lists each trade with `stop_pct` and `stop_basis`.

## What it cannot do
- Group 396 alone could not answer "did stopped trades first run in favour?", because real-trade recorded no peak / trough after entry. Group 397 adds that (see `docs/GROUP397_REAL_TRADE_EXCURSION.md`).
- `scripts/opening_replay.py` still reads the position-stocks `/trades/history` export, not a real-trade export.
- Trades opened before this build, and positions imported from the broker, show `stop_basis` as `unknown`.
- Small samples: groups under the report's `min_trades_note` are noise. No significance test.

## Deploy note
New nullable column on `trade_decisions`, added on the next real-trade boot (logged as "real-trade-db: added trade_decisions.stop_basis"). Nothing else to do.

## Tests
- `tests/test_group396_records_stop_fields.py` (8, pure): stop % from the fixed distance, fallback to the decision, missing attribute, old rows give None, blank or non-string tag, inverted levels, `by_stop_basis` grouping with an `unknown` bucket, empty report key.
- `tests/test_group396_entry_stop_basis.py` (7 test functions, 12 cases): `_atr_stop_basis` tag table, `evaluate_mode` wiring (flat, atr, structure, insane structural stop keeps atr), model column, and the migration on a table that predates it (idempotent).
- Verification status: NOT run under pytest (no pytest / sqlalchemy in the sandbox). The 8 pure record tests ran as plain Python and pass; `_atr_stop_basis` was checked by extraction; the entry wiring and migration tests are unrun.
