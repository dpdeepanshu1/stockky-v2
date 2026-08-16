"""
Training script for the Prediction Service.

Run this manually (or on a weekly cron) once you have Docker Compose up:

    docker compose run --rm prediction-service python train.py

It builds a labeled dataset directly from Yahoo Finance historical candles
(no need to wait weeks accumulating your own scan history — we can label
the past immediately because we already know what happened next):

  Label = 1 if the close price ~10 trading days after date D is at least
            5% higher than the close on date D (matches the product spec's
            "~5% gain within one month" framing, using 10 trading days ≈ 2
            weeks as a slightly tighter, more actionable window)
  Label = 0 otherwise

Features = the same technical-indicator snapshot the live service computes
           at inference time (see features.py) — this is what keeps
           train/serve consistent.

Saves the trained model to model.pkl, which main.py loads at startup.

ENHANCEMENTS (lightweight financial ML best practices):
- Optional Walk‑Forward validation with purging and embargo
- Per‑fold scaling (RobustScaler) – no data leakage
- Financial metrics (Sharpe, drawdown, etc.) on OOS predictions
- Trading simulation with transaction costs
- Configurable target types (Log_Return, Percentage_Return, Directional)
"""

import os
import logging
import time
import random
import signal
import sys
import json
import gc
import numpy as np
import pandas as pd
import yfinance as yf
from xgboost import XGBClassifier, XGBRegressor
from sklearn.metrics import classification_report, roc_auc_score, brier_score_loss
from sklearn.calibration import CalibratedClassifierCV
import joblib

# --- New imports for enhancements ---
from targets import TargetGenerator
from walk_forward import WalkForwardSplitter
from preprocessing import TimeAwareScaler
from metrics import compute_all_metrics
from trading import TradingSimulator
# -----------------------------------

from features import compute_feature_frame, FEATURE_COLUMNS

# ----------------------------------------------------------------------
# Logging setup
# ----------------------------------------------------------------------
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("prediction-train")

# Silence yfinance's own ERROR logs (they are noisy and we handle them)
logging.getLogger("yfinance").setLevel(logging.WARNING)

# ----------------------------------------------------------------------
# Graceful exit on Ctrl+C
# ----------------------------------------------------------------------
def signal_handler(sig, frame):
    logger.info("\nTraining interrupted by user. Exiting gracefully...")
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

# ----------------------------------------------------------------------
# Configuration for enhancements (can be moved to config file)
# ----------------------------------------------------------------------
ENHANCEMENT_CONFIG = {
    "walk_forward": {
        "enabled": True,                    # Set False to skip walk-forward evaluation
        "target_type": "Log_Return",        # "Log_Return", "Percentage_Return", "Directional"
        "forecast_horizon_days": 10,        # matches existing LOOKAHEAD_DAYS
        "validation_strategy": {
            "method": "WalkForward",
            "train_window_size": 252,       # ~1 year of trading days
            "validation_window_size": 63,   # ~3 months
            "step_size": 63,
            "embargo_days": 10              # at least forecast horizon
        },
        "preprocessing": {
            "scaler_type": "RobustScaler"
        },
        "trading": {
            "long_threshold": 0.0,
            "short_threshold": 0.0,
            "transaction_cost_bps": 5.0,
            "slippage_bps": 2.0,
            "allow_short": False
        },
        "random_seed": 42
    }
}

# ----------------------------------------------------------------------
# Base universe + dynamic extras
# ----------------------------------------------------------------------
# Known symbols that consistently fail on Yahoo Finance (removed)
PROBLEMATIC_SYMBOLS = {"VARUNBEV", "INTERGLOBE", "INDEGENE", "POLICYBZ"}

BASE_TRAINING_UNIVERSE = [
    # === Existing (retained) ===
    "TCS", "INFY", "HDFCBANK", "ICICIBANK", "RELIANCE", "HCLTECH",
    "WIPRO", "COFORGE", "ANGELONE", "ADANIPOWER", "BEL", "HAL",
    "TMPV", "TMCV",  # Tata Motors demerged entities
    "SBIN", "AXISBANK", "KOTAKBANK", "LT",
    "MARUTI", "SUNPHARMA", "TITAN", "ITC",
    "BAJFINANCE", "ASIANPAINT", "NESTLEIND", "ULTRACEMCO",

    # === Top broker picks (August 2026) ===
    "BHARTIARTL", "M&M", "SHRIRAMFIN",
    "INDIGO",       # May or may not work; kept, but filtering will handle failure
    "VARUNBEV", "DMART", "CHOLAFIN", "PHOENIXLTD",
    "FORTIS", "CUMMINSIND", "SYRMA", "ADANIPORTS", "HINDALCO",
    "AUROPHARMA", "NAVINFLUOR",
    # "POLICYBZ",   # Already removed
    "NEULANDLAB", "BIOCON",
    "BAJAJ-AUTO", "PAYTM",
    "MPHASIS", "RICOAUTO",
]


def load_dynamic_training_universe():
    """Combine base universe with external files (updated by other services)."""
    symbols = set(BASE_TRAINING_UNIVERSE)

    extra_files = [
        "../news-intelligence-service/trending_symbols.txt",
        "../event-tracker-service/event_symbols.txt",
        "manual_symbols.txt",
    ]

    for file in extra_files:
        if os.path.exists(file):
            with open(file, "r", encoding="utf-8") as f:
                for line in f:
                    symbol = line.strip().upper()
                    if symbol:
                        symbols.add(symbol)
                        logger.debug("Added %s from %s", symbol, file)

    # Remove problematic symbols that are known to be unavailable
    symbols = symbols - PROBLEMATIC_SYMBOLS

    return sorted(symbols)


TRAINING_UNIVERSE = load_dynamic_training_universe()

logger.info("=" * 80)
logger.info("Training started")
logger.info("Total symbols : %d", len(TRAINING_UNIVERSE))
logger.info("=" * 80)

LOOKAHEAD_DAYS = 10
TARGET_GAIN_PCT = 4.5  # Reduced from 5.0 to get more positive samples


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Fix MultiIndex columns and ensure 1D Series."""
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.droplevel(1)  # keep price level
    for col in df.columns:
        if isinstance(df[col], pd.DataFrame):
            df[col] = df[col].squeeze()
    return df


def fetch_with_retry(symbol: str, max_retries: int = 3) -> pd.DataFrame:
    """
    Download data with exponential backoff.
    Returns empty DataFrame if all retries fail.
    """
    tickers = [f"{symbol}.NS", f"{symbol}.BO", symbol]
    for ticker in tickers:
        for attempt in range(max_retries):
            try:
                df = yf.download(
                    ticker,
                    period="5y",
                    interval="1d",
                    auto_adjust=True,
                    progress=False,
                    threads=False,
                )
                if not df.empty and "Close" in df.columns:
                    logger.info("Fetched %s from yfinance (%s)", symbol, ticker)
                    return df
                else:
                    # Empty data – try next ticker or retry
                    break
            except Exception as e:
                # If it's a rate‑limit error, wait longer
                wait = (2 ** attempt) + random.uniform(0, 1)
                logger.warning(
                    "Download failed for %s (attempt %d/%d): %s. Retrying in %.1fs",
                    ticker, attempt+1, max_retries, str(e)[:50], wait
                )
                time.sleep(wait)
        # If we get here, the ticker didn't work; try next ticker
    logger.warning("All sources failed for %s", symbol)
    return pd.DataFrame()


def build_dataset() -> pd.DataFrame:
    """Build dataset with features, label, close price, date, symbol."""
    rows = []
    failed_symbols = []

    for symbol in TRAINING_UNIVERSE:
        df = fetch_with_retry(symbol)
        if df.empty:
            failed_symbols.append(symbol)
            continue

        if "Close" not in df.columns or len(df) < 250:
            logger.warning("%s: insufficient data (<250 days)", symbol)
            continue

        df = _normalize_df(df)

        try:
            feat_df = compute_feature_frame(df)
        except Exception as e:
            logger.warning("Feature generation failed for %s: %s", symbol, str(e)[:100])
            continue

        closes = feat_df["Close"].values
        for i in range(200, len(feat_df) - LOOKAHEAD_DAYS):
            row = feat_df.iloc[i]
            if row[FEATURE_COLUMNS].isna().any():
                continue
            future_close = closes[i + LOOKAHEAD_DAYS]
            gain = (future_close - closes[i]) / closes[i] * 100
            label = 1 if gain >= TARGET_GAIN_PCT else 0
            record = {col: row[col] for col in FEATURE_COLUMNS}
            record["label"] = label
            record["symbol"] = symbol
            record["date"] = feat_df.index[i]
            record["close"] = closes[i]        # <-- store close price for target generation
            rows.append(record)

        # Polite delay between symbols (with slight jitter)
        time.sleep(0.3 + random.uniform(0, 0.2))
        logger.info("Processed %s — %d rows so far", symbol, len(rows))

    if failed_symbols:
        logger.warning("Skipped %d symbols due to download failures: %s",
                       len(failed_symbols), ", ".join(failed_symbols[:5]))
    return pd.DataFrame(rows)


# ----------------------------------------------------------------------
# NEW: Walk‑Forward validation function (enhancement)
# ----------------------------------------------------------------------
def run_walk_forward_validation(dataset: pd.DataFrame, config: dict) -> dict:
    """
    Perform walk‑forward validation on the dataset.

    Returns:
        dict: Combined OOS predictions, actuals, strategy returns, and metrics.
    """
    logger.info("\n" + "=" * 60)
    logger.info("WALK‑FORWARD VALIDATION STARTED")
    logger.info("=" * 60)

    # Unpack config
    wf_config = config["walk_forward"]
    target_type = wf_config["target_type"]
    forecast_horizon = wf_config["forecast_horizon_days"]
    val_strat = wf_config["validation_strategy"]
    scaler_type = wf_config["preprocessing"]["scaler_type"]
    trading_cfg = wf_config["trading"]
    random_seed = wf_config.get("random_seed", 42)

    np.random.seed(random_seed)

    # Ensure dataset has required columns
    feature_cols = FEATURE_COLUMNS  # from features.py
    for col in feature_cols:
        if col not in dataset.columns:
            raise ValueError(f"Feature column '{col}' missing from dataset")

    if "close" not in dataset.columns:
        raise ValueError("Dataset missing 'close' column (required for target generation)")

    # Sort by date (critical for chronology)
    dataset = dataset.sort_values("date").reset_index(drop=True)

    # Generate targets using TargetGenerator
    target_gen = TargetGenerator(
        target_type=target_type,
        forecast_horizon=forecast_horizon,
        price_col="close"
    )
    # We'll apply target generation per fold to avoid look‑ahead.

    # Walk‑forward splitter
    splitter = WalkForwardSplitter(
        train_window=val_strat["train_window_size"],
        val_window=val_strat["validation_window_size"],
        step_size=val_strat.get("step_size", val_strat["validation_window_size"]),
        embargo_days=val_strat.get("embargo_days", forecast_horizon),
        forecast_horizon=forecast_horizon,
        method=val_strat["method"]
    )

    folds = splitter.split(dataset)
    logger.info("Number of folds: %d", len(folds))

    if not folds:
        logger.warning("No folds generated – insufficient data for walk‑forward.")
        return {}

    # Containers for OOS predictions
    all_preds = []
    all_actuals = []
    all_strategy_returns = []

    for i, fold in enumerate(folds):
        logger.info("\n--- Fold %d/%d ---", i+1, len(folds))
        train_idx = list(range(fold.train_start, fold.train_end + 1))
        val_idx = list(range(fold.val_start, fold.val_end + 1))

        train_data = dataset.iloc[train_idx].copy()
        val_data = dataset.iloc[val_idx].copy()

        # Generate targets for train and val separately
        # We already have the 'close' column.
        # For target generation, we need the price column to compute future returns.
        # We'll use the TargetGenerator on each subset.
        # However, TargetGenerator uses shift(-horizon) which requires future data.
        # For training, it's safe because we're looking forward within the train set.
        # For validation, we also compute target using its own future (which is available).
        # This is correct because target is generated from future returns, but we are only
        # using it for evaluation – no leakage into training features.
        # The split ensures that train_data and val_data are disjoint and chronological.

        # Generate targets for train
        train_target, train_data_with_target = target_gen.generate(train_data, inplace=False)
        train_data = train_data_with_target
        # The target column is named according to target_type? Actually TargetGenerator adds columns:
        # 'pct_return', 'log_return', 'directional', and if inplace=True, 'target'.
        # We'll use the 'target' column if inplace=True, but we used inplace=False.
        # Let's just use the target we got: train_target is a Series.
        # We'll add a 'target' column to train_data.
        train_data["target"] = train_target

        # Same for val
        val_target, val_data_with_target = target_gen.generate(val_data, inplace=False)
        val_data = val_data_with_target
        val_data["target"] = val_target

        # Drop rows with NaN target (due to insufficient future data at end of each subset)
        train_data = train_data.dropna(subset=["target"])
        val_data = val_data.dropna(subset=["target"])

        if len(train_data) < 50 or len(val_data) < 10:
            logger.warning(f"Fold {i+1}: insufficient samples (train={len(train_data)}, val={len(val_data)}). Skipping.")
            continue

        # Split X, y
        X_train = train_data[feature_cols].values.astype(np.float32)
        y_train = train_data["target"].values.astype(np.float32)
        X_val = val_data[feature_cols].values.astype(np.float32)
        y_val = val_data["target"].values.astype(np.float32)

        # Scale per fold – fit on train only
        scaler = TimeAwareScaler(scaler_type)
        X_train_scaled = scaler.fit_transform(X_train)
        X_val_scaled = scaler.transform(X_val)

        # Choose model based on target type
        if target_type == "Directional":
            # Classification
            model = XGBClassifier(
                n_estimators=200,
                max_depth=4,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                eval_metric="logloss",
                random_state=random_seed,
                use_label_encoder=False,
                tree_method="hist",
            )
            # Convert targets to int for classification
            y_train_cls = y_train.astype(int)
            y_val_cls = y_val.astype(int)
            model.fit(X_train_scaled, y_train_cls, eval_set=[(X_val_scaled, y_val_cls)], verbose=False)
            pred_val = model.predict_proba(X_val_scaled)[:, 1]  # probability of positive class
            # For directional, we treat predictions as probabilities; we'll use threshold 0.5 to get signals.
            # But we need numeric returns for trading. We'll convert probability to return signal:
            # We'll use the probability as a "predicted return" proxy? Or we can use the raw predicted class?
            # Since we want to simulate trading, we need a continuous predicted return.
            # As a heuristic, we can use the probability as a proxy for return magnitude.
            # Alternatively, we can treat predictions as class and set return = ±1.
            # For simplicity, we'll use the probability as predicted return.
            pred_val_continuous = pred_val  # range [0,1]
            # But actual returns are binary? We need actual returns for metrics.
            # We can use the percentage return as actual (y_val) which is continuous.
            # However, y_val is directional (0,1,-1). We'll use the actual percentage return from the dataset.
            # We need actual percentage return for metrics. We have it in train_data['pct_return'].
            # But we didn't store it. We'll recompute or store.
            # Let's store actual percentage return in a column.
            # We'll compute it during target generation.
            # Actually, we have val_data['pct_return'] from target_gen.
            actual_return = val_data["pct_return"].values
        else:
            # Regression (Log_Return, Percentage_Return)
            model = XGBRegressor(
                n_estimators=200,
                max_depth=4,
                learning_rate=0.05,
                subsample=0.8,
                colsample_bytree=0.8,
                eval_metric="rmse",
                random_state=random_seed,
                tree_method="hist",
            )
            model.fit(X_train_scaled, y_train, eval_set=[(X_val_scaled, y_val)], verbose=False)
            pred_val = model.predict(X_val_scaled)
            actual_return = y_val  # already continuous

        # Trading simulation
        trade_sim = TradingSimulator(
            long_threshold=trading_cfg["long_threshold"],
            short_threshold=trading_cfg["short_threshold"],
            transaction_cost_bps=trading_cfg["transaction_cost_bps"],
            slippage_bps=trading_cfg["slippage_bps"],
            allow_short=trading_cfg["allow_short"]
        )
        signals, costs, strategy_ret = trade_sim.simulate(pred_val, actual_return)

        # Store
        all_preds.extend(pred_val)
        all_actuals.extend(actual_return)
        all_strategy_returns.extend(strategy_ret)

        logger.info("Fold %d: val samples=%d, pred mean=%.4f, actual mean=%.4f",
                    i+1, len(val_data), np.mean(pred_val), np.mean(actual_return))

        # Cleanup
        del train_data, val_data, X_train, X_val, y_train, y_val, model
        gc.collect()

    # Combine all OOS predictions
    if not all_preds:
        logger.warning("No OOS predictions generated. Walk‑forward validation aborted.")
        return {}

    all_preds = np.array(all_preds)
    all_actuals = np.array(all_actuals)
    all_strategy_returns = np.array(all_strategy_returns)

    # Compute financial metrics
    metrics = compute_all_metrics(all_preds, all_actuals, all_strategy_returns)

    logger.info("\n" + "-" * 40)
    logger.info("WALK‑FORWARD OOS PERFORMANCE")
    logger.info("-" * 40)
    for k, v in metrics.items():
        if isinstance(v, float):
            logger.info(f"{k:>20}: {v:.4f}")
        else:
            logger.info(f"{k:>20}: {v}")
    logger.info("-" * 40)

    return {
        "predictions": all_preds,
        "actuals": all_actuals,
        "strategy_returns": all_strategy_returns,
        "metrics": metrics,
        "num_folds": len(folds)
    }


# ----------------------------------------------------------------------
# Main training function
# ----------------------------------------------------------------------
def main():
    logger.info("Building dataset from %d symbols...", len(TRAINING_UNIVERSE))
    dataset = build_dataset()

    if dataset.empty:
        logger.error("No data retrieved. Check network / Yahoo Finance.")
        return

    logger.info("Dataset: %d rows, positive rate %.1f%%",
                len(dataset), dataset["label"].mean() * 100)

    if len(dataset) < 500:
        logger.error("Too few rows (%d).", len(dataset))
        return

    # --- NEW: Walk‑forward validation if enabled ---
    if ENHANCEMENT_CONFIG["walk_forward"]["enabled"]:
        wf_result = run_walk_forward_validation(dataset, ENHANCEMENT_CONFIG)
        if wf_result:
            # Optionally store metrics to a file for later review
            joblib.dump(wf_result, "walk_forward_results.joblib")
            logger.info("Walk‑forward results saved to walk_forward_results.joblib")
        else:
            logger.warning("Walk‑forward validation produced no results.")

    # --- Continue with existing training pipeline ---
    X = dataset[FEATURE_COLUMNS]
    y = dataset["label"]

    # Time-based split: fit, calibration, test
    cutoff_calib = dataset["date"].quantile(0.64, interpolation="lower")
    cutoff_test = dataset["date"].quantile(0.8, interpolation="lower")
    fit_mask = dataset["date"] < cutoff_calib
    calib_mask = (dataset["date"] >= cutoff_calib) & (dataset["date"] < cutoff_test)
    test_mask = dataset["date"] >= cutoff_test

    X_fit, y_fit = X[fit_mask], y[fit_mask]
    X_calib, y_calib = X[calib_mask], y[calib_mask]
    X_test, y_test = X[test_mask], y[test_mask]

    logger.info("Time-based 3-way split — calibration cutoff: %s, test cutoff: %s",
                cutoff_calib.date(), cutoff_test.date())
    logger.info(
        "Fit:   %d rows (%s to %s), positive rate %.1f%%",
        len(X_fit),
        dataset.loc[fit_mask, "date"].min().date(),
        dataset.loc[fit_mask, "date"].max().date(),
        y_fit.mean() * 100,
    )
    logger.info(
        "Calib: %d rows (%s to %s), positive rate %.1f%%",
        len(X_calib),
        dataset.loc[calib_mask, "date"].min().date(),
        dataset.loc[calib_mask, "date"].max().date(),
        y_calib.mean() * 100,
    )
    logger.info(
        "Test:  %d rows (%s to %s), positive rate %.1f%%",
        len(X_test),
        dataset.loc[test_mask, "date"].min().date(),
        dataset.loc[test_mask, "date"].max().date(),
        y_test.mean() * 100,
    )

    if len(X_test) < 50 or y_test.nunique() < 2:
        logger.error(
            "Test set too small or single-class after the time split (%d rows). "
            "Need a longer training history to get a trustworthy holdout.",
            len(X_test),
        )
        return
    if len(X_calib) < 50 or y_calib.nunique() < 2:
        logger.error(
            "Calibration set too small or single-class (%d rows) — can't safely "
            "calibrate. Need a longer training history.",
            len(X_calib),
        )
        return

    # Compute scale_pos_weight to handle class imbalance
    scale_pos_weight = (y_fit == 0).sum() / (y_fit == 1).sum()
    logger.info("Scale pos weight: %.2f", scale_pos_weight)

    base_model = XGBClassifier(
        n_estimators=300,
        max_depth=4,
        learning_rate=0.05,
        subsample=0.8,
        colsample_bytree=0.8,
        scale_pos_weight=scale_pos_weight,
        eval_metric="logloss",
        random_state=42,
        use_label_encoder=False,
    )
    base_model.fit(X_fit, y_fit)

    raw_probs_test = base_model.predict_proba(X_test)[:, 1]

    calibrated_model = CalibratedClassifierCV(base_model, method="sigmoid", cv="prefit")
    calibrated_model.fit(X_calib, y_calib)

    preds = calibrated_model.predict(X_test)
    probs = calibrated_model.predict_proba(X_test)[:, 1]

    raw_brier = brier_score_loss(y_test, raw_probs_test)
    calibrated_brier = brier_score_loss(y_test, probs)

    logger.info("\nOut-of-time test performance (dates the model never trained on):")
    logger.info("\n%s", classification_report(y_test, preds))
    logger.info("ROC-AUC: %.3f (ranking ability — calibration doesn't change this)", roc_auc_score(y_test, probs))
    logger.info(
        "Brier score (lower is better; measures how trustworthy the probability "
        "number itself is, not just the ranking): raw=%.4f -> calibrated=%.4f (%s)",
        raw_brier, calibrated_brier,
        "improved" if calibrated_brier < raw_brier else "no improvement — check calibration set size",
    )

    joblib.dump(calibrated_model, "model.pkl")
    logger.info("Calibrated model saved to model.pkl")

    logger.info("=" * 80)
    logger.info("Training completed successfully")
    logger.info("Rows : %d", len(dataset))
    logger.info("Model saved : model.pkl")
    if ENHANCEMENT_CONFIG["walk_forward"]["enabled"]:
        logger.info("Walk‑forward results saved : walk_forward_results.joblib")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()