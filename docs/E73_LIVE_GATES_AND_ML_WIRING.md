# E73 - everything live, and D22-D24 wired into the training run

Two parts. Part A answers decision 1 of the E72 hand-over ("everything in live mode"). Part B finishes the ML wiring that was cut off in the previous session (`ml_wiring.py` and `fo_download.py` were never in the E72 zip, so they are rebuilt here).

## A. Live mode (structure gates and breakout volume)
| Switch | Before | Now |
|---|---|---|
| Real Trade `STRUCTURE_GATE_MODE` / `STRUCTURE_GATE_DEMO_MODE` | `log` | **`enforce`** |
| Real Trade `STRUCTURE_BREAKOUT_VOLUME_MODE` | `log` | **`enforce`** (thin-volume breakout = gate reason, entry WAITs) |
| Position Stocks `PS_STRUCTURE_GATE_MODE` | `enforce` | `enforce` (unchanged, decision 1) |
| Position Stocks `PS_STRUCTURE_BREAKOUT_VOLUME_MODE` | `log` (no enforce existed) | **`enforce`** - new: a thin-volume breakout is a refusal reason (`STRUCTURE_*` skip). `log` only via the PS_ name; Real Trade's shared name cannot loosen the scalper |

- Changed: `real-trade-service/config.py`, `control/catalog.py`; `position-stocks-service/config.py`, `orders/structure_gate.py`, `control/catalog.py`; `.env.example`; `docs/ENV_REFERENCE.md` regenerated.
- The `thin_volume_breakouts` counter on both `/structure/status` endpoints now counts the breakout in `enforce` too (before, only an observation counted); the Position Stocks refusal-reason breakdown has a new `volume` bucket.
- **B15 guard still applies on Real Trade.** While fewer than `STRUCTURE_ENFORCE_MIN_OOS` (15) observation rows with a measured outcome exist, `enforce` runs as `log` (the status endpoint shows `gate_mode_effective`). That guard is evidence protection, not a default, so it was left in place. `STRUCTURE_ENFORCE_MIN_OOS=0` removes it.
- **Not switched, on purpose:** `REGIME_RULES_MODE` has no enforce mode (it is a log-only study; making it live means new rule behaviour and thresholds, not a flag), and `STRUCTURE_EXIT_MODE` stays `log` (`tighten` / `exit` would start selling or tightening live positions). Both are one setting / one decision away if you want them.
- Tests: PS `test_e72_log_mode_and_breakout_volume.py` updated for the new default (log never refuses, enforce does, counter + reason bucket); Real Trade defaults / catalog-vs-config suites unchanged and green.

## B. D22 / D23 / D24 in `train.py --legacy-ohlcv` (offline, opt-in)
Nothing here is imported by a live decision, gate or the `/train` endpoint. Without a `ml_extras` block and with `target_type` left alone, `train.py` runs the old path.

- `training/ml_wiring.py` (new): `resolve_extras` (strict - a wrong key / type / combination raises), archive dataset builder, point-in-time universe (D23) applied **after** the labels (labels need later bars), F&O features (D24), triple-barrier columns + `tb_end_date` + the MA-cross flag (D22), per-fold **purge by date**, probability calibration (Brier / reliability) and the model-picks-vs-MA-cross comparison.
- `training/train.py`: hooks only (feature list, dataset, targets, purge, classifier for `TripleBarrier_Class`, report key `ml_wiring`). A triple-barrier run trades on the label's net return and zeroes the simulator's own bps costs (they are already in `tb_net_return`). Two small robustness fixes: `Session` no longer raises `NameError` when sqlalchemy is missing, and the per-symbol target step is an explicit loop (pandas >= 2.2 warns and pandas 3 drops the `symbol` column from `groupby.apply`).
- `training/fo_features.py`: `parse_fo_bhavcopy_oi` (new UDiFF and old FUTSTK layouts, summed over expiries) and `load_oi_history`.
- `training/fo_download.py` (new, run by hand): `python fo_download.py --from 2026-09-01 --to 2026-10-22 --out fo_data [--only ban|oi]`. Skips files that exist; a 404 on a weekday is a holiday. **The two exchange URL templates were not exercised from the sandbox** (no network); if one has moved the run prints `failed: <url>` and writes nothing for that day - fix `BAN_URL` / `BHAV_URL` only.
- Tests: `training/tests/test_e73_ml_wiring.py` (30): config validation, universe point-in-time + survivors, NaN-not-0 for uncovered ban / OI dates, label end dates, purge, evaluation, downloader (write / skip / holiday), and a full `run_training_pipeline` smoke for both triple-barrier targets plus the unchanged old path (uses a stand-in xgboost only when the real one is missing).

### Example config (`cfg.json`)
```json
{
  "target_type": "TripleBarrier_Class",
  "forecast_horizon_days": 5,
  "validation_strategy": {"method": "WalkForward", "train_window_size": 3000, "validation_window_size": 500, "step_size": 500, "embargo_days": 5},
  "preprocessing": {"scaler_type": "RobustScaler"},
  "model": {"max_depth": 4, "learning_rate": 0.05, "n_estimators": 150, "subsample": 0.8, "colsample_bytree": 0.8, "reg_alpha": 0.5, "reg_lambda": 1.0, "tree_method": "hist"},
  "trading": {"long_threshold": 0.0, "short_threshold": 0.0, "transaction_cost_bps": 5.0, "slippage_bps": 2.0, "allow_short": false},
  "random_seed": 42,
  "data": {"symbols": [], "period": "2y"},
  "ml_extras": {
    "source": "archive", "archive_dir": "archive_parquet", "archive_symbols": [],
    "universe": {"enabled": true, "min_history": 60, "adv_window": 20, "min_adv": 1e7, "top_n": 0},
    "fo": {"enabled": true, "ban_dir": "fo_data", "oi_dir": "fo_data"},
    "barrier_params": {"up_atr": 2.0, "dn_atr": 1.0, "cost_pct": 0.3, "slip_pct": 0.1},
    "purge": true, "long_threshold": null
  }
}
```
Note: the walk-forward windows count rows of the pooled (all-symbol) frame, as before - size them for your universe.

### Honest limits
Archive prices are not split / bonus adjusted; a ticker rename looks like one symbol ending and another starting; ban / OI flags are NaN (never 0) on dates the files do not cover; a `yfinance` source with `universe.enabled` is refused (it only knows survivors). `tb_net_return` assumes a same-bar stop/target tie goes to the stop.

## On the VM
```
cd services/decision-prediction-service/training && pytest tests/test_e73_ml_wiring.py tests/test_d_triple_barrier_universe_fo.py
cd services/real-trade-service && pytest tests/test_dvol_breakout_volume.py tests/test_b15_enforce_guard.py tests/test_group351_structure_gate.py tests/test_group415_structure_defaults.py tests/test_group477_catalog_matches_config.py
cd services/position-stocks-service && pytest tests/test_e72_log_mode_and_breakout_volume.py tests/test_group354_structure_gate.py tests/test_group355_structure_status.py tests/test_group356_structure_observation.py tests/test_group477_catalog_matches_config.py
python scripts/check_control_catalog.py && python scripts/gen_env_reference.py --check
python services/decision-prediction-service/training/fo_download.py --from 2026-09-01 --to 2026-10-22 --out fo_data
```
Not run here: pytest itself (not installable; a stand-in runner was used), the suites that need sqlalchemy / httpx / websockets, the real xgboost, the exchange downloads, frontend `tsc` / `vite build`.
