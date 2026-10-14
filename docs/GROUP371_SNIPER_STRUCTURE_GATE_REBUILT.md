# Group 371 - Buy Sniper structure gate rebuilt and wired

The group 357 doc and tests described a daily-structure gate in `buy_sniper.py`, but the code was missing from this tree, and `sniper_observation.py`
(group 361/362) was never called from `main.py`. Both are now in.

## buy_sniper.py
- `structure_verdict(row, price)`: pure, never raises. Rules and defaults match real-trade `entry_engine/structure_gate.py`: chase > 1.5 ATR, stretch > 3.0 ATR,
  room < 0.5 ATR, structure R:R < 1.2, `stop_too_wide`, bearish daily trend or a fresh bearish BOS (<= 5 bars). Chase / stretch / room / R:R are
  re-expressed at the card price. No / stale (> 7 d) / insufficient summary, no ATR, or a price > 15% away from the structure close = no opinion = never blocks.
- `build_suggestion` runs the gate AFTER the sniper's own R:R check. A blocked card returns None; a surfaced card carries an additive `structure` record
  (display only: `stop_loss`, `target_price`, `reward_risk_ratio` are unchanged).
- `suggestions_from_scan_payload` adds `structure_mode` and `structure_rejected` (`symbol`, `price`, `reasons`); when every setup was held back the message says so.
- `symbols_needing_structure(payload, limit)` picks the best-conviction candidates that have no summary yet. `_payload_rows` / `_structure_summary` are the helpers
  `sniper_observation.py` already imports.
- `SNIPER_STRUCTURE_MODE`: `enforce` (default) | `off`. Any other value (including a stale `shadow`) is enforce.

## main.py
- `_sniper_attach_structure(payload)` (fail-open) fetches `GET {TECHNICAL_URL}/structure/{sym}` for up to `SNIPER_STRUCTURE_FETCH_LIMIT` (12) candidates, 4 at a
  time, inside `SNIPER_STRUCTURE_FETCH_TIMEOUT_S` (10), and puts the flat summary on the row. Counter `stockky_sniper_structure_fetch_total{outcome}`.
- `_sniper_observe(payload, out)`: hands the final answer to `sniper_observation.record` on a worker thread (never delays or changes the response).
- `GET /scan/find-buys/edge` (+ `/api/` alias): gate edge report. `POST /scan/find-buys/edge/evaluate`: one evaluator pass.
  QStash tick body `{"action": "sniper-observe-eval"}` runs the same pass (schedule it once a day after the close).
- `kv_cache.py`: durable prefix `stockky:sniper_obs:` (listed as gateway-only in `tests/test_kv_cache_drift.py`).

## Env (documented in .env.example, docs/ENV_REFERENCE.md regenerated)
`SNIPER_STRUCTURE_MODE`, `SNIPER_STRUCTURE_FETCH_LIMIT`, `SNIPER_STRUCTURE_FETCH_TIMEOUT_S`, `SNIPER_OBS_ENABLED`, `SNIPER_OBS_MAX_PER_DAY`, `SNIPER_OBS_RETENTION_DAYS`,
`SNIPER_OBS_BATCH`, `SNIPER_OBS_HISTORY_TIMEOUT_S`, `SNIPER_OBS_GIVE_UP_DAYS`.

## Tests
`test_buy_sniper.py` (card now carries `structure`, response has the two new keys), `test_group357_sniper_structure_gate.py` and `test_group361_sniper_observation.py`
(the shadow-mode cases now assert a stale `shadow` is enforce).

## Not done
`structure_refresh.py` is still not hooked into `main.py` (its 11 wiring tests in `tests/test_group360_structure_refresh.py` fail until it is); position-stocks
`ScalpStructureObservation` hook; frontend badge labels; backtest on real data.
