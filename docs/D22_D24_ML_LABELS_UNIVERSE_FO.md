# D22-D24 - cost-aware labels, point-in-time universe, F&O ban + OI (2026-10-22), offline training only

Files in `services/decision-prediction-service/training/`. **Nothing here changes a live decision, gate or the default training config** (`target_type` stays `Log_Return`).

| Item | File | What it does |
|---|---|---|
| D22 labels | `triple_barrier.py` | For a signal at the close of bar i: entry = open of bar i+1, target = entry + 2 ATR, stop = entry - 1 ATR, window = bars i+1..i+horizon (entry bar included). First barrier wins; a bar touching both counts as the STOP; otherwise timeout at the close of bar i+horizon. `net_return` subtracts 0.30 % costs + 0.10 % slippage (same as the B tools). Adds `tb_exit`, `tb_label`, `tb_net_return`, `tb_y`, `tb_bars`, `tb_end_idx`; rows whose window does not fit are NaN. `purge_overlapping()` drops training rows whose window reaches the validation block. |
| D22 wiring | `targets.py` | New `target_type` values `TripleBarrier_Net` (net return, regression) and `TripleBarrier_Class` (1 if net > 0), optional `barrier_params` (`up_atr`, `dn_atr`, `atr_period`, `cost_pct`, `slip_pct`, `horizon`). Needs open / high / low columns, which `build_multi_symbol_dataset` already keeps. |
| D22 calibration | `calibration.py` | `brier`, `reliability_table` (+ECE), `IsotonicCalibrator` (fit on a hold-out fold, not on training rows), `ma_cross_signals` (the B16 dumb baseline), `compare_to_baseline` (model picks vs MA-cross entries, 90 % bootstrap CI, verdicts `insufficient_sample` (< 15 either side, the B15 floor) / `beats_dumb` / `worse_than_dumb` / `no_better`). |
| D23 universe | `pit_universe.py` | `load_archive` reads `SYMBOL_YYYY_MM.parquet` / `.csv` / `.csv.gz` from `bhavcopy_archive.py`; `add_membership` sets `in_universe` from bars strictly before each date (min history, trailing average traded value, optional top-N); delisted symbols stay in with their bars; `survivorship_report` counts symbols that stopped early and their share of in-universe rows. |
| D24 F&O | `fo_features.py` | `parse_secban` / `load_ban_history` (`fo_secban_YYYYMMDD.csv`), `add_ban_flags` (`in_fo_ban`, `ban_prev`, `ban_days_20`, this row excluded from its own count), `add_oi_features` (`oi_chg_1d`, `oi_chg_5d`, `oi_buildup` +2 long build-up / +1 short covering / -1 long unwinding / -2 short build-up, one-hot flags). Non-F&O stocks keep NaN OI features, never 0. |

Tests: `training/tests/test_d_triple_barrier_universe_fo.py` (19; numpy + pandas only; includes truncation tests that show no lookahead in labels, the MA signal and universe membership). Run: `python3 training/tests/test_d_triple_barrier_universe_fo.py`.

## Not done / caveats
* **Not wired into `train.py`.** `TargetGenerator` accepts the new types, but `train.py` still uses `FEATURE_COLUMNS` without the OI / ban columns and the config default is unchanged. Wiring the universe, the OI / ban columns and the purge into a training run is the next step and needs OI and ban files to exist (nothing in the repo downloads them yet).
* **No calibrated model yet.** The calibrator and comparison exist; applying them needs a trained model's hold-out probabilities.
* **Prices in the archive are not split / bonus adjusted**, and a ticker rename looks like one symbol ending and another starting.
* **ATR multiples (2 / 1), horizon 5 and the liquidity floor (Rs 1 crore average traded value, 60 bars) are starting values**, not tuned on your data.
* **Unpaired baseline comparison**: model picks and MA-cross entries cover different days.
* Parquet reading needs pyarrow (not installed in this sandbox); the `.csv` / `.csv.gz` paths were tested, the parquet branch was not.
