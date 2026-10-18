# kronos-forecast-service

Small FastAPI worker that runs [Kronos](https://github.com/shiyu-coder/Kronos) (Kronos-small, MIT, vendored in `kronos_model/`) and returns **statistics of N sampled
5-day price paths** for one symbol. It feeds `real-trade-service` entry **Gate 4f** (`entry_engine/kronos_gate.py`). The gate ships **OFF**.

- `POST /forecast` `{symbol, ref_price, stop_pct, target_pct, horizon=5, n_paths=10}` -> `p_up`, `q10/q50/q90_ret_pct`, `p_target_first`, `p_stop_first`, `ev_pct`, ...
  Candles come from market-data `GET /history/{symbol}`; only completed daily bars are used (today's forming bar is dropped). One forecast at a time.
- `GET /health`. The model loads on the first forecast (`KRONOS_PRELOAD=true` loads it at start). First start downloads the weights into the `kronos-hf-cache` volume.
- Same seed for the same symbol + last bar, so a repeated request returns the same paths.
- Tests (no torch, no network): `python3 tests/run_plain.py` or `python -m pytest tests`.

Turning it on: see `docs/GROUP425_KRONOS_FORECAST_GATE.md`. Run `scripts/kronos_backtest.py` first; the thresholds are hypotheses.
