# Session 31 — 2026-09-12 — training/prediction PIT + data_feed.py merge audit

Continuing the round-30 "still unaudited" list.

## data_feed.py

- `merge_feed_payload` (the merge-never-wipe logic: sparse-zero protection,
  seed-vs-real fundamental protection) — read in full. Correct.
- `DataFeedStore.get_symbol` / `get_symbols_bulk` / `_prepare_symbol_payload`
  / `put_symbol` / `put_symbols_bulk` (canonical+alias+legacy key writes,
  ≤max-price durable-fields-only gate) — read in full. Correct.
- `compute_rsi_from_closes`, `patch_feed_price` — already checked session 30,
  still clean.
- Bulk Yahoo/NSE-bhavcopy download functions (`bulk_yahoo_download_prices`,
  `download_nse_bhavcopy_bulk`, `run_bulk_yahoo_price_feed*`) — not yet read.

## decision-prediction-service training/prediction (point-in-time correctness)

This is the highest-stakes area for a subtle, hard-to-notice bug (look-ahead
bias silently inflating backtest performance vs live results), so this
round focused specifically on PIT-correctness rather than every file:

- `training/targets.py` — `pct_change(h).shift(-h)` for the forward-return
  target: verified algebraically correct (`pct_return[t] == P[t+h]/P[t]-1`,
  not accidentally a backward-looking window). Log-return target and
  directional (BUY/HOLD/SELL) labeling from it: correct.
- `training/walk_forward.py` — `WalkForwardSplitter`: embargo is enforced
  `>= forecast_horizon`, purge gap sits strictly between train_end and
  val_start, both WalkForward and ExpandingWindow modes terminate correctly
  (val_end grows monotonically with fold_id either way). Correct.
- `training/pit_validation.py` — `validate_prediction_snapshot` /
  `validate_outcome_vs_prediction` / `filter_train_rows_pit`: the guards
  against future-stamped feature snapshots and bar dates before the
  prediction timestamp are sound.
- `training/feature_builder.py` — `build_training_row`: history is sliced to
  `index <= as_of` before any feature is computed; `_get_fundamental_as_of`
  and `_compute_news_scores_as_of` both explicitly filter to `date <= as_of`.
  `make_label_from_forward_return` correctly uses only `index > as_of` for
  the label (as it must — it's the target, not a feature). No look-ahead
  found.
- `training/metrics.py` — Sharpe/Sortino/max-drawdown/win-rate/profit-factor/
  directional-accuracy: standard, correct formulas.
- `prediction/pred_features.py` — `compute_technical_features` /
  `compute_feature_frame`: rolling windows and `.shift(1)` (yesterday, not
  tomorrow) all point backward from the current row. No look-ahead found.

**Not yet read**: `prediction/pred_train.py` (1091 lines — the actual model
training loop), `training/evaluate.py` (771 lines), `training/scanner.py`,
`training/insights.py`, `training/universe_ingest.py`,
`training/build_dataset_with_fund_news.py`, `training/bhavcopy_archive.py`,
`training/rl_explore.py`, `training/oracle_compat.py` / `kv_cache.py`,
`prediction/pred_train.py`'s walk-forward driver `pred_walk_forward.py`,
`pred_preprocessing.py`, `pred_trading.py`, `pred_metrics.py`, `losses.py`.

## Frontend

~20,400 lines across the frontend's .ts/.tsx files — not started. Given the
size, this needs its own dedicated round(s) rather than a partial skim.
