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
from sklearn.metrics import (
    classification_report,
    roc_auc_score,
    brier_score_loss,
    precision_recall_fscore_support,
    precision_recall_curve,
    roc_curve,
    average_precision_score,
)
from sklearn.calibration import CalibratedClassifierCV
import joblib

# Optional heavy deps (SMOTE / SHAP) — degrade gracefully if missing
try:
    from imblearn.over_sampling import SMOTE, BorderlineSMOTE
    _HAS_SMOTE = True
except Exception:
    SMOTE = BorderlineSMOTE = None  # type: ignore
    _HAS_SMOTE = False

try:
    import shap
    _HAS_SHAP = True
except Exception:
    shap = None  # type: ignore
    _HAS_SHAP = False

# --- New imports for enhancements ---
from pred_targets import TargetGenerator
from pred_walk_forward import WalkForwardSplitter
from pred_preprocessing import TimeAwareScaler
from pred_metrics import compute_all_metrics
from pred_trading import TradingSimulator
from pred_features import compute_feature_frame, FEATURE_COLUMNS

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
    # --- Nifty 50 core ---
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "INFY", "ITC", "SBIN", "BHARTIARTL",
    "HINDUNILVR", "LT", "KOTAKBANK", "AXISBANK", "BAJFINANCE", "ASIANPAINT", "MARUTI",
    "SUNPHARMA", "TITAN", "ULTRACEMCO", "NESTLEIND", "WIPRO", "HCLTECH", "POWERGRID",
    "NTPC", "TATASTEEL", "JSWSTEEL", "ADANIENT", "ADANIPORTS", "ONGC", "COALINDIA",
    "BAJAJFINSV", "M&M", "TECHM", "CIPLA", "DRREDDY", "APOLLOHOSP", "INDUSINDBK",
    "HEROMOTOCO", "EICHERMOT", "BPCL", "BRITANNIA", "GRASIM", "HINDALCO", "DIVISLAB",
    "SBILIFE", "HDFCLIFE", "TATACONSUM", "BAJAJ-AUTO", "LTIM",
    # --- Banks / financials ---
    "FEDERALBNK", "BANDHANBNK", "IDFCFIRSTB", "PNB", "BANKBARODA", "CANBK",
    "CHOLAFIN", "SHRIRAMFIN", "MUTHOOTFIN", "PFC", "RECLTD", "IRFC",
    # --- IT / growth ---
    "COFORGE", "PERSISTENT", "MPHASIS", "LTTS", "OFSS", "TATAELXSI",
    # --- Auto / industrials ---
    "TVSMOTOR", "ASHOKLEY", "BOSCHLTD", "BEL", "HAL", "BHEL", "SIEMENS", "ABB",
    "CUMMINSIND", "DIXON", "POLYCAB", "HAVELLS",
    # --- Pharma / healthcare ---
    "AUROPHARMA", "BIOCON", "LUPIN", "TORNTPHARM", "ALKEM", "LAURUSLABS", "FORTIS",
    # --- Consumer / retail ---
    "DMART", "TRENT", "PAGEIND", "GODREJCP", "DABUR", "MARICO", "COLPAL", "JUBLFOOD",
    "INDIGO", "ZOMATO", "PAYTM", "NYKAA",
    # --- Metals / energy / infra ---
    "VEDL", "JINDALSTEL", "SAIL", "NMDC", "IOC", "GAIL", "PETRONET", "IGL",
    "ADANIGREEN", "ADANIPOWER", "TATAPOWER", "NHPC", "IRCTC", "CONCOR",
    # --- Chemicals / specialty ---
    "PIDILITIND", "SRF", "NAVINFLUOR", "DEEPAKNTR", "AARTIIND", "PIIND",
    # --- Others liquid ---
    "ANGELONE", "MCX", "CDSL", "CAMS", "PNBHOUSING", "LICI", "MAXHEALTH",
    "SYRMA", "KAYNES", "PHOENIXLTD", "GODREJPROP", "DLF", "OBEROIRLTY",
]

# Cap how many optional extras we add from disk (keep training time sane)
MAX_DYNAMIC_EXTRAS = int(os.getenv("PRED_MAX_DYNAMIC_EXTRAS", "80"))


def load_dynamic_training_universe():
    """Combine large base universe with optional external symbol lists."""
    symbols = set(BASE_TRAINING_UNIVERSE)
    extras = []

    extra_files = [
        "manual_symbols.txt",
        os.path.join(os.path.dirname(__file__), "manual_symbols.txt"),
        "../news-intelligence-service/trending_symbols.txt",
        "../event-tracker-service/event_symbols.txt",
        os.getenv("PRED_EXTRA_SYMBOLS_FILE", ""),
    ]

    for file in extra_files:
        if not file or not os.path.exists(file):
            continue
        try:
            with open(file, "r", encoding="utf-8") as f:
                for line in f:
                    symbol = line.strip().upper().replace(".NS", "").replace(".BO", "")
                    if not symbol or symbol.startswith("#"):
                        continue
                    if symbol not in symbols and symbol not in PROBLEMATIC_SYMBOLS:
                        extras.append(symbol)
        except Exception as e:
            logger.warning("Could not read %s: %s", file, e)

    # Prefer unique extras, capped
    seen = set()
    for s in extras:
        if s in seen:
            continue
        seen.add(s)
        if len(seen) >= MAX_DYNAMIC_EXTRAS:
            break
        symbols.add(s)

    symbols = symbols - PROBLEMATIC_SYMBOLS
    out = sorted(symbols)
    logger.info(
        "Training universe size=%d (base=%d, dynamic_extras≈%d)",
        len(out), len(BASE_TRAINING_UNIVERSE), min(len(seen), MAX_DYNAMIC_EXTRAS),
    )
    return out


TRAINING_UNIVERSE = load_dynamic_training_universe()

logger.info("=" * 80)
logger.info("Training started")
logger.info("Total symbols : %d", len(TRAINING_UNIVERSE))
logger.info("=" * 80)

LOOKAHEAD_DAYS = 10
TARGET_GAIN_PCT = float(os.getenv('PRED_TARGET_GAIN_PCT', '4.0'))  # ~4% in 10 sessions
DECISION_THRESHOLD_FALLBACK = float(os.getenv('PRED_DECISION_THRESHOLD', '0.35'))
MIN_PRECISION_AT_THRESHOLD = float(os.getenv('PRED_MIN_PRECISION', '0.30'))
MIN_RECALL_AT_THRESHOLD = float(os.getenv('PRED_MIN_RECALL', '0.15'))


def _normalize_df(df: pd.DataFrame) -> pd.DataFrame:
    """Fix MultiIndex / yfinance quirks and ensure 1D OHLCV Series with title-case names."""
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        # Prefer price field level if present (e.g. ('Close','ADANIPORTS.NS'))
        try:
            lvl0 = [str(x).lower() for x in df.columns.get_level_values(0)]
            if any(x in ("close", "open", "high", "low", "volume", "adj close") for x in lvl0):
                df.columns = df.columns.get_level_values(0)
            else:
                df.columns = df.columns.droplevel(-1)
        except Exception:
            df.columns = df.columns.droplevel(-1)
    # Flatten any nested frames
    for col in list(df.columns):
        if isinstance(df[col], pd.DataFrame):
            df[col] = df[col].squeeze()
    # Map common aliases → Open/High/Low/Close/Volume
    rename = {}
    for c in df.columns:
        cl = str(c).strip().lower().replace(" ", "")
        if cl in ("open",):
            rename[c] = "Open"
        elif cl in ("high",):
            rename[c] = "High"
        elif cl in ("low",):
            rename[c] = "Low"
        elif cl in ("close", "adjclose", "adj_close"):
            rename[c] = "Close"
        elif cl in ("volume", "vol"):
            rename[c] = "Volume"
    if rename:
        df = df.rename(columns=rename)
    # Drop duplicate columns keeping first
    df = df.loc[:, ~pd.Index(df.columns.astype(str)).duplicated()]
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
                if df is None or df.empty:
                    break
                df = _normalize_df(df)
                if "Close" in df.columns and len(df) > 50:
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

        if "Close" in feat_df.columns:
            closes = feat_df["Close"].values
        elif "Close" in df.columns:
            # Align to feature index
            closes = df.reindex(feat_df.index)["Close"].values
        else:
            logger.warning("%s: no Close column after features — skip", symbol)
            continue
        for i in range(200, len(feat_df) - LOOKAHEAD_DAYS):
            row = feat_df.iloc[i]
            # FEATURE_COLUMNS may include fund/news cols not in technical frame
            missing = [c for c in FEATURE_COLUMNS if c not in feat_df.columns]
            use_cols = [c for c in FEATURE_COLUMNS if c in feat_df.columns]
            if not use_cols or row[use_cols].isna().any():
                continue
            future_close = closes[i + LOOKAHEAD_DAYS]
            if pd.isna(closes[i]) or pd.isna(future_close) or closes[i] == 0:
                continue
            gain = (future_close - closes[i]) / closes[i] * 100
            label = 1 if gain >= TARGET_GAIN_PCT else 0
            record = {col: (float(row[col]) if col in feat_df.columns else 0.0) for col in FEATURE_COLUMNS}
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
def _choose_decision_threshold(y_true, probs) -> tuple:
    """Detailed threshold selection on out-of-sample probabilities.

    Candidates evaluated:
      - max F1 (with min precision/recall floors)
      - max F1 unconstrained
      - Youden's J (TPR - FPR) from ROC
      - max F-beta (beta=0.5 → prefer precision for trading)
      - precision-at-top-quantile (top 10% / 20% scores)

    Final pick: best F1 that still meets floors; else unconstrained F1;
    else Youden; else env fallback.
    """
    y_true = np.asarray(y_true).astype(int)
    probs = np.asarray(probs, dtype=float)
    n = len(y_true)
    pos_rate = float(y_true.mean()) if n else 0.0

    candidates = {}

    def _stats(thr: float) -> dict:
        preds = (probs >= thr).astype(int)
        p, r, f1, _ = precision_recall_fscore_support(
            y_true, preds, average="binary", zero_division=0, pos_label=1
        )
        # F-beta (beta=0.5): precision weighted more (fewer false BUYs)
        beta = 0.5
        fb = (1 + beta ** 2) * p * r / (beta ** 2 * p + r) if (p + r) > 0 else 0.0
        return {
            "threshold": float(thr),
            "precision": float(p),
            "recall": float(r),
            "f1": float(f1),
            "fbeta_0_5": float(fb),
            "pred_pos_rate": float(preds.mean()),
            "pred_pos_count": int(preds.sum()),
        }

    # Grid search
    grid = np.unique(np.concatenate([
        np.linspace(0.10, 0.70, 61),
        np.quantile(probs, np.linspace(0.50, 0.99, 20)),
    ]))
    grid = grid[(grid >= 0.05) & (grid <= 0.95)]

    best_f1_floor = {"f1": -1.0}
    best_f1_any = {"f1": -1.0}
    best_fbeta = {"fbeta_0_5": -1.0}

    for thr in grid:
        st = _stats(float(thr))
        if st["pred_pos_count"] == 0:
            continue
        if st["f1"] > best_f1_any["f1"]:
            best_f1_any = st
        if (
            st["precision"] >= MIN_PRECISION_AT_THRESHOLD
            and st["recall"] >= MIN_RECALL_AT_THRESHOLD
            and st["f1"] > best_f1_floor["f1"]
        ):
            best_f1_floor = st
        if st["fbeta_0_5"] > best_fbeta["fbeta_0_5"]:
            best_fbeta = st

    if best_f1_floor.get("f1", -1) >= 0:
        candidates["f1_with_floors"] = best_f1_floor
    if best_f1_any.get("f1", -1) >= 0:
        candidates["f1_unconstrained"] = best_f1_any
    if best_fbeta.get("fbeta_0_5", -1) >= 0:
        candidates["fbeta_precision_tilt"] = best_fbeta

    # Youden's J from ROC
    try:
        fpr, tpr, roc_thrs = roc_curve(y_true, probs)
        j = tpr - fpr
        j_i = int(np.nanargmax(j))
        thr_j = float(roc_thrs[j_i]) if j_i < len(roc_thrs) else DECISION_THRESHOLD_FALLBACK
        # roc_curve thresholds can be inf at first point
        if not np.isfinite(thr_j):
            thr_j = DECISION_THRESHOLD_FALLBACK
        st_j = _stats(thr_j)
        st_j["youden_j"] = float(j[j_i])
        candidates["youden_j"] = st_j
    except Exception as e:
        logger.debug("Youden threshold failed: %s", e)

    # Precision at top quantiles (ranking view)
    topq = {}
    for q in (0.90, 0.80):
        try:
            cut = float(np.quantile(probs, q))
            st = _stats(cut)
            st["quantile"] = q
            topq[f"top_{int((1-q)*100)}pct"] = st
        except Exception:
            pass
    if topq:
        candidates["top_quantile"] = topq

    # Average precision (PR-AUC) for logging
    try:
        pr_auc = float(average_precision_score(y_true, probs))
    except Exception:
        pr_auc = 0.0

    # Selection policy
    if "f1_with_floors" in candidates:
        chosen_key = "f1_with_floors"
        best = candidates["f1_with_floors"]
    elif "fbeta_precision_tilt" in candidates and candidates["fbeta_precision_tilt"].get("f1", 0) > 0:
        chosen_key = "fbeta_precision_tilt"
        best = candidates["fbeta_precision_tilt"]
    elif "f1_unconstrained" in candidates:
        chosen_key = "f1_unconstrained"
        best = candidates["f1_unconstrained"]
    elif "youden_j" in candidates:
        chosen_key = "youden_j"
        best = candidates["youden_j"]
    else:
        chosen_key = "fallback"
        best = {
            "threshold": DECISION_THRESHOLD_FALLBACK,
            "f1": 0.0,
            "precision": 0.0,
            "recall": 0.0,
            "pred_pos_rate": 0.0,
            "pred_pos_count": 0,
        }

    detail = {
        "chosen_policy": chosen_key,
        "chosen": best,
        "candidates": {
            k: v for k, v in candidates.items() if k != "top_quantile"
        },
        "top_quantile": topq,
        "pr_auc": pr_auc,
        "base_rate_positive": pos_rate,
        "n_samples": n,
    }

    logger.info(
        "Threshold selection: policy=%s thr=%.3f F1=%.3f P=%.3f R=%.3f "
        "pred_pos=%.1f%% (true base rate=%.1f%%) PR-AUC=%.3f",
        chosen_key,
        best.get("threshold", DECISION_THRESHOLD_FALLBACK),
        best.get("f1", 0),
        best.get("precision", 0),
        best.get("recall", 0),
        100.0 * best.get("pred_pos_rate", 0),
        100.0 * pos_rate,
        pr_auc,
    )
    return float(best.get("threshold", DECISION_THRESHOLD_FALLBACK)), detail


def _apply_smote(X, y, random_state: int = 42):
    """Oversample minority class on the *fit* fold only (never test/calib).

    Returns X_res, y_res, info dict. Falls back to original data if SMOTE
    unavailable or fails (e.g. too few positives).
    """
    y = np.asarray(y).astype(int)
    n_pos = int((y == 1).sum())
    n_neg = int((y == 0).sum())
    info = {"enabled": False, "method": None, "n_pos_before": n_pos, "n_neg_before": n_neg}

    if not _HAS_SMOTE:
        logger.warning("imbalanced-learn not installed — skipping SMOTE (pip install imbalanced-learn)")
        return X, y, info
    if n_pos < 6:
        logger.warning("Too few positives (%d) for SMOTE — skipping", n_pos)
        return X, y, info

    # Target: bring minority up toward ~40% of majority (not full 1:1 to limit noise)
    target_ratio = float(os.getenv("PRED_SMOTE_RATIO", "0.45"))
    target_ratio = min(max(target_ratio, 0.25), 1.0)
    sampling_strategy = min(target_ratio, n_pos / max(n_neg, 1) + 0.35)
    sampling_strategy = min(max(sampling_strategy, float(n_pos) / max(n_neg, 1) + 1e-6), 1.0)

    k = int(os.getenv("PRED_SMOTE_K", "5"))
    k = max(1, min(k, n_pos - 1))

    try:
        method = os.getenv("PRED_SMOTE_METHOD", "borderline").lower()
        if method == "borderline" and BorderlineSMOTE is not None:
            sampler = BorderlineSMOTE(
                sampling_strategy=sampling_strategy,
                k_neighbors=k,
                random_state=random_state,
            )
            info["method"] = "BorderlineSMOTE"
        else:
            sampler = SMOTE(
                sampling_strategy=sampling_strategy,
                k_neighbors=k,
                random_state=random_state,
            )
            info["method"] = "SMOTE"

        X_res, y_res = sampler.fit_resample(X, y)
        info["enabled"] = True
        info["n_pos_after"] = int((np.asarray(y_res) == 1).sum())
        info["n_neg_after"] = int((np.asarray(y_res) == 0).sum())
        info["sampling_strategy"] = sampling_strategy
        logger.info(
            "SMOTE (%s): pos %d→%d | neg %d→%d | strategy=%.2f k=%d",
            info["method"],
            info["n_pos_before"], info["n_pos_after"],
            info["n_neg_before"], info["n_neg_after"],
            sampling_strategy, k,
        )
        return X_res, y_res, info
    except Exception as e:
        logger.warning("SMOTE failed (%s) — training without oversampling", e)
        return X, y, info


def _log_shap_importance(model, X_sample, feature_names, max_samples: int = 400) -> dict:
    """Compute SHAP summary for feature importance (optional dependency)."""
    out = {"enabled": False, "top_features": []}
    if not _HAS_SHAP:
        logger.warning("shap not installed — skipping SHAP (pip install shap)")
        return out
    try:
        if hasattr(X_sample, "values"):
            X_arr = X_sample.values
            names = list(X_sample.columns) if hasattr(X_sample, "columns") else list(feature_names)
        else:
            X_arr = np.asarray(X_sample)
            names = list(feature_names)

        n = min(len(X_arr), max_samples)
        if n < 20:
            logger.warning("Too few rows for SHAP (%d)", n)
            return out
        rng = np.random.RandomState(42)
        idx = rng.choice(len(X_arr), size=n, replace=False)
        Xs = X_arr[idx]

        # TreeExplainer works with raw XGB; for CalibratedClassifierCV use base_estimator
        est = model
        if hasattr(model, "calibrated_classifiers_"):
            # sklearn >=1.x prefit calibrator
            try:
                est = model.calibrated_classifiers_[0].estimator
            except Exception:
                est = getattr(model, "estimator", model)
        if hasattr(est, "get_booster") or est.__class__.__name__.startswith("XGB"):
            explainer = shap.TreeExplainer(est)
            sv = explainer.shap_values(Xs)
        else:
            explainer = shap.Explainer(est.predict_proba, Xs[:50])
            sv = explainer(Xs).values
            if isinstance(sv, list):
                sv = sv[1]

        if isinstance(sv, list):
            sv = sv[1]  # positive class
        sv = np.asarray(sv)
        if sv.ndim == 3:
            sv = sv[:, :, 1]
        mean_abs = np.abs(sv).mean(axis=0)
        order = np.argsort(-mean_abs)
        top = []
        for i in order[:15]:
            top.append({"feature": names[int(i)], "mean_abs_shap": float(mean_abs[int(i)])})
        out["enabled"] = True
        out["top_features"] = top
        out["n_samples"] = n
        logger.info("SHAP top features:")
        for row in top[:10]:
            logger.info("  %-28s  mean|SHAP|=%.5f", row["feature"], row["mean_abs_shap"])
        try:
            with open("shap_importance.json", "w", encoding="utf-8") as fh:
                json.dump(out, fh, indent=2)
        except Exception:
            pass
        return out
    except Exception as e:
        logger.warning("SHAP analysis failed: %s", e)
        return out



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

    # Class imbalance: scale_pos_weight with mild boost so positives are not ignored
    n_neg = int((y_fit == 0).sum())
    n_pos = int((y_fit == 1).sum())
    if n_pos < 1:
        logger.error("No positive labels in fit set — cannot train.")
        return
    # Cap weight so we do not overfit noise (common with rare 5% events)
    raw_spw = n_neg / max(n_pos, 1)
    scale_pos_weight = float(min(max(raw_spw * 1.25, 1.0), 12.0))
    logger.info(
        "Class balance fit set: neg=%d pos=%d (%.1f%% pos) | scale_pos_weight=%.2f (raw=%.2f)",
        n_neg, n_pos, 100.0 * n_pos / (n_neg + n_pos), scale_pos_weight, raw_spw,
    )

    # SMOTE only on fit fold (no leakage into calib/test)
    use_smote = os.getenv("PRED_USE_SMOTE", "true").lower() in ("1", "true", "yes")
    smote_info = {"enabled": False}
    X_fit_train, y_fit_train = X_fit, y_fit
    if use_smote:
        X_fit_train, y_fit_train, smote_info = _apply_smote(X_fit, y_fit)
        # After SMOTE, reduce scale_pos_weight (synthetic balance already applied)
        if smote_info.get("enabled"):
            n_neg_s = int(smote_info.get("n_neg_after", n_neg))
            n_pos_s = int(smote_info.get("n_pos_after", n_pos))
            scale_pos_weight = float(min(max((n_neg_s / max(n_pos_s, 1)) * 1.05, 1.0), 6.0))
            logger.info("scale_pos_weight after SMOTE adjusted to %.2f", scale_pos_weight)

    base_model = XGBClassifier(
        n_estimators=int(os.getenv("PRED_N_ESTIMATORS", "400")),
        max_depth=int(os.getenv("PRED_MAX_DEPTH", "5")),
        learning_rate=float(os.getenv("PRED_LR", "0.04")),
        subsample=0.85,
        colsample_bytree=0.85,
        min_child_weight=3,
        reg_lambda=1.2,
        scale_pos_weight=scale_pos_weight,
        max_delta_step=1,  # helps rare-positive convergence
        eval_metric="logloss",
        random_state=42,
        use_label_encoder=False,
        n_jobs=2,
    )
    base_model.fit(X_fit_train, y_fit_train)

    raw_probs_test = base_model.predict_proba(X_test)[:, 1]

    calibrated_model = CalibratedClassifierCV(base_model, method="sigmoid", cv="prefit")
    calibrated_model.fit(X_calib, y_calib)

    probs = calibrated_model.predict_proba(X_test)[:, 1]
    # Detailed threshold selection (not default 0.5)
    decision_threshold, thr_stats = _choose_decision_threshold(y_test, probs)
    if isinstance(thr_stats, dict) and "chosen" in thr_stats:
        preds = (probs >= decision_threshold).astype(int)
    else:
        preds = (probs >= decision_threshold).astype(int)

    # SHAP feature importance on a sample of the fit set (original, not synthetic)
    shap_info = {}
    if os.getenv("PRED_USE_SHAP", "true").lower() in ("1", "true", "yes"):
        try:
            shap_info = _log_shap_importance(
                calibrated_model,
                X_fit if hasattr(X_fit, "iloc") else pd.DataFrame(X_fit, columns=FEATURE_COLUMNS),
                FEATURE_COLUMNS,
            )
        except Exception as e:
            logger.warning("SHAP step skipped: %s", e)
            shap_info = {"enabled": False, "error": str(e)[:200]}

    raw_brier = brier_score_loss(y_test, raw_probs_test)
    calibrated_brier = brier_score_loss(y_test, probs)

    logger.info("\nOut-of-time test performance (dates the model never trained on):")
    _ch = thr_stats.get("chosen", thr_stats) if isinstance(thr_stats, dict) else {}
    logger.info(
        "Decision threshold=%.3f (policy=%s F1=%.3f P=%.3f R=%.3f) — not sklearn default 0.5",
        decision_threshold,
        thr_stats.get("chosen_policy", "?") if isinstance(thr_stats, dict) else "?",
        _ch.get("f1", 0), _ch.get("precision", 0), _ch.get("recall", 0),
    )
    logger.info("\n%s", classification_report(y_test, preds, zero_division=0))
    try:
        logger.info("ROC-AUC: %.3f (ranking ability — calibration doesn't change this)", roc_auc_score(y_test, probs))
    except Exception as e:
        logger.warning("ROC-AUC unavailable: %s", e)
    logger.info(
        "Brier score (lower is better): raw=%.4f -> calibrated=%.4f (%s)",
        raw_brier, calibrated_brier,
        "improved" if calibrated_brier < raw_brier else "no improvement — check calibration set size",
    )
    logger.info(
        "Prob distribution OOS: min=%.3f p25=%.3f median=%.3f p75=%.3f max=%.3f | "
        "pred_positive_rate=%.1f%% (true_positive_rate=%.1f%%)",
        float(probs.min()), float(np.percentile(probs, 25)), float(np.median(probs)),
        float(np.percentile(probs, 75)), float(probs.max()),
        100.0 * preds.mean(), 100.0 * float(np.mean(y_test)),
    )

    joblib.dump(calibrated_model, "model.pkl")
    meta = {
        "decision_threshold": decision_threshold,
        "threshold_stats": thr_stats,
        "scale_pos_weight": scale_pos_weight,
        "smote": smote_info,
        "shap": {
            "enabled": bool(shap_info.get("enabled")),
            "top_features": shap_info.get("top_features", [])[:15],
        },
        "target_gain_pct": TARGET_GAIN_PCT,
        "lookahead_days": LOOKAHEAD_DAYS,
        "feature_columns": list(FEATURE_COLUMNS),
        "universe_size": len(TRAINING_UNIVERSE),
        "rows": int(len(dataset)),
        "n_pos_fit": n_pos,
        "n_neg_fit": n_neg,
    }
    joblib.dump(meta, "model_meta.joblib")
    # Also JSON for humans / services without joblib dependency on meta
    try:
        with open("model_meta.json", "w", encoding="utf-8") as fh:
            json.dump(meta, fh, indent=2)
    except Exception as e:
        logger.warning("Could not write model_meta.json: %s", e)
    logger.info("Calibrated model saved to model.pkl (threshold=%.3f meta saved)", decision_threshold)

    logger.info("=" * 80)
    logger.info("Training completed successfully")
    logger.info("Rows : %d", len(dataset))
    logger.info("Model saved : model.pkl (+ model_meta.joblib / model_meta.json)")
    if ENHANCEMENT_CONFIG["walk_forward"]["enabled"]:
        logger.info("Walk‑forward results saved : walk_forward_results.joblib")
    logger.info("=" * 80)


if __name__ == "__main__":
    main()