# services/training-service/train.py
"""
Training script for training‑service.
Uses financial ML best practices: walk‑forward, per‑fold scaling, financial metrics.
Enhanced with model versioning, database logging, and configurable inputs.
"""
import os
import sys
import json
import logging
import argparse
import numpy as np
import pandas as pd
import yfinance as yf
import xgboost as xgb
import joblib
import gc
import time
import random
import threading
import signal
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, Optional

# Import our modules
from targets import TargetGenerator
from walk_forward import WalkForwardSplitter
from preprocessing import TimeAwareScaler
from metrics import compute_all_metrics
from trading import TradingSimulator
from sklearn.metrics import accuracy_score, precision_score, recall_score, f1_score, roc_auc_score

# Single source of truth for which PredictionSnapshot columns represent a
# "setup" — shared with scanner.py's KNN similarity search so the two
# systems can't drift apart.
from scanner import SIMILARITY_FEATURES

# Optional imports for enhanced functionality
try:
    from models import ModelRegistry
    HAS_MODEL_REGISTRY = True
except ImportError:
    HAS_MODEL_REGISTRY = False

try:
    from sqlalchemy.orm import Session
    import models as db_models
    HAS_DB = True
except ImportError:
    HAS_DB = False

# If you have a features.py file, import it; otherwise define a minimal feature set.
try:
    from features import compute_feature_frame, FEATURE_COLUMNS
except ImportError:
    # Fallback: define a simple feature set (you can expand)
    FEATURE_COLUMNS = ['sma_10', 'sma_30', 'ema_10', 'rsi', 'volatility', 'volume_sma']
    def compute_feature_frame(df):
        df = df.copy()
        df['sma_10'] = df['Close'].rolling(10).mean()
        df['sma_30'] = df['Close'].rolling(30).mean()
        df['ema_10'] = df['Close'].ewm(span=10, adjust=False).mean()
        df['rsi'] = compute_rsi(df['Close'], 14)
        df['volatility'] = df['Close'].pct_change().rolling(10).std()
        df['volume_sma'] = df['Volume'].rolling(10).mean()
        return df.dropna()
    def compute_rsi(series, period=14):
        delta = series.diff()
        gain = (delta.where(delta > 0, 0)).rolling(period).mean()
        loss = (-delta.where(delta < 0, 0)).rolling(period).mean()
        rs = gain / loss
        rsi = 100 - (100 / (1 + rs))
        return rsi

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger("training-service")

# ---------- IST timezone helper ----------
IST = ZoneInfo("Asia/Kolkata")

def ist_now() -> datetime:
    """Return current time as a naive datetime in IST (UTC+5:30)."""
    return datetime.now(IST).replace(tzinfo=None)

# ---------- Numpy conversion helper ----------
def convert_numpy(obj):
    """Recursively convert numpy types to Python native types."""
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        return float(obj)
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if isinstance(obj, dict):
        return {k: convert_numpy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [convert_numpy(v) for v in obj]
    if isinstance(obj, (np.bool_, bool)):
        return bool(obj)
    return obj

# ---------- Configuration ----------
DEFAULT_SYMBOLS = [
    "TCS", "INFY", "RELIANCE", "HDFCBANK", "ICICIBANK",
    "HCLTECH", "SBIN", "LT", "MARUTI", "SUNPHARMA"
]

DEFAULT_TRAINING_CONFIG = {
    "target_type": "Log_Return",
    "forecast_horizon_days": 5,
    "validation_strategy": {
        "method": "WalkForward",
        "train_window_size": 126,    # reduced
        "validation_window_size": 21,
        "step_size": 21,
        "embargo_days": 5
    },
    "preprocessing": {
        "scaler_type": "RobustScaler"
    },
    "model": {
        "max_depth": 4,
        "learning_rate": 0.05,
        "n_estimators": 150,
        "subsample": 0.8,
        "colsample_bytree": 0.8,
        "reg_alpha": 0.5,
        "reg_lambda": 1.0,
        "tree_method": "hist"
    },
    "trading": {
        "long_threshold": 0.0,
        "short_threshold": 0.0,
        "transaction_cost_bps": 5.0,
        "slippage_bps": 2.0,
        "allow_short": False
    },
    "random_seed": 42,
    "data": {
        "symbols": DEFAULT_SYMBOLS,
        "period": "2y"
    }
}

# ---------- Lock and progress files ----------
LOCK_FILE = 'training.lock'
PROGRESS_FILE = 'training_progress.json'

# Thread-safe abort event
abort_event = threading.Event()

def request_abort():
    """Set the abort event. Called by the API to stop training."""
    logger.info("request_abort() called – setting abort_event")
    abort_event.set()
    # Optionally remove the lock file to release the lock for future runs
    if os.path.exists(LOCK_FILE):
        try:
            os.remove(LOCK_FILE)
            logger.info("Lock file removed by request_abort()")
        except Exception as e:
            logger.warning(f"Could not remove lock file in request_abort: {e}")

def write_progress(current_fold, total_folds, elapsed_seconds=None, stage: str = None, detail: dict = None):
    """Write current progress to a JSON file, polled by /api/train/progress
    for the animated Training tab view. current_fold/total_folds are the
    legacy OHLCV pipeline's fold-based progress (-1 means aborted); stage/
    detail are used by train_pick_success_model instead, since a single
    classifier fit doesn't have folds the same way."""
    data = {
        'current_fold': current_fold,
        'total_folds': total_folds,
        'timestamp': time.time(),
        'elapsed': elapsed_seconds,
        'stage': stage,
        'detail': detail or {},
    }
    try:
        with open(PROGRESS_FILE, 'w') as f:
            json.dump(data, f)
    except Exception:
        pass


def get_training_progress():
    """Reads whatever write_progress() last wrote. Returns a default idle
    state if training hasn't run yet or the file's missing/unreadable."""
    if not os.path.exists(PROGRESS_FILE):
        return {"stage": "idle", "detail": {}, "timestamp": None}
    try:
        with open(PROGRESS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return {"stage": "idle", "detail": {}, "timestamp": None}

def lock_checker():
    """Background thread that checks the lock file and sets abort event if missing."""
    while not abort_event.is_set():
        time.sleep(2.0)  # Check every 2 seconds
        if not os.path.exists(LOCK_FILE):
            logger.warning("Lock file missing – aborting training.")
            abort_event.set()
            break

def check_abort():
    """Check if abort event is set or lock file is missing; raise KeyboardInterrupt."""
    if abort_event.is_set():
        logger.info("check_abort() raising KeyboardInterrupt because abort_event is set")
        raise KeyboardInterrupt("Training aborted by user.")
    if not os.path.exists(LOCK_FILE):
        logger.warning("check_abort() raising KeyboardInterrupt because lock file is missing")
        raise KeyboardInterrupt("Lock file removed – training aborted.")

# ---------- XGBoost callback for abort ----------
class AbortCallback(xgb.callback.TrainingCallback):
    """XGBoost callback that checks abort_event after each boosting round."""
    def after_iteration(self, model, epoch, evals_log):
        if abort_event.is_set():
            logger.info("AbortCallback: abort_event detected, raising exception to stop training.")
            raise KeyboardInterrupt("Training aborted by user during XGBoost fit.")
        return False  # Continue normally if not set

# ---------- Helpers ----------
def fetch_symbol_data(symbol, period="2y", max_retries=5):
    """Fetch OHLCV data with exponential backoff for rate limits."""
    tickers = [f"{symbol}.NS", f"{symbol}.BO", symbol]
    for ticker in tickers:
        for attempt in range(max_retries):
            try:
                df = yf.download(ticker, period=period, interval="1d", auto_adjust=True,
                                 progress=False, threads=False)
                if not df.empty and "Close" in df.columns:
                    return df
                else:
                    break  # try next ticker
            except Exception as e:
                if "Rate limit" in str(e) or "Too Many Requests" in str(e):
                    wait = (2 ** attempt) * 10 + random.uniform(0, 10)
                    logger.warning(f"Rate limit for {ticker}, retrying in {wait:.1f}s (attempt {attempt+1}/{max_retries})")
                    time.sleep(wait)
                else:
                    logger.warning(f"Error fetching {ticker}: {e}")
                    break
    return pd.DataFrame()

def build_multi_symbol_dataset(symbols, period="2y"):
    """Build dataset with delays between symbols to avoid rate limits."""
    all_rows = []
    total = len(symbols)
    for idx, sym in enumerate(symbols):
        logger.info(f"Fetching {sym} ({idx+1}/{total})...")
        check_abort()
        df = fetch_symbol_data(sym, period)
        if df.empty:
            logger.warning(f"No data for {sym}")
            continue
        df = df[['Open','High','Low','Close','Volume']].copy()
        df.columns = ['open','high','low','close','volume']
        df['symbol'] = sym
        df = df.dropna()
        feat = compute_feature_frame(df)
        feat = feat.dropna(subset=FEATURE_COLUMNS)
        if len(feat) < 50:
            logger.warning(f"Too few rows for {sym}: {len(feat)}")
            continue
        feat['symbol'] = sym
        feat['date'] = feat.index
        all_rows.append(feat)
        # Longer delay between symbols (3-5 seconds)
        delay = 3.0 + random.uniform(0, 2.0)
        time.sleep(delay)
    if not all_rows:
        raise ValueError("No data collected")
    full = pd.concat(all_rows, ignore_index=True)
    full = full.sort_values(['symbol','date']).reset_index(drop=True)
    return full

def save_training_run_to_db(session, config, metrics, fold_details, model_version, dataset_size, num_symbols):
    """Save training run details using the provided session."""
    if not HAS_DB:
        return
    try:
        run = db_models.TrainingRun(
            run_timestamp=ist_now(),
            config=json.dumps(convert_numpy(config)),
            dataset_size=dataset_size,
            num_symbols=num_symbols,
            model_version=model_version,
            walk_forward_metrics=json.dumps(convert_numpy(metrics)),
            fold_details=json.dumps(convert_numpy(fold_details))
        )
        session.add(run)
        session.commit()
        logger.info("Training run logged to database")
    except Exception as e:
        logger.error(f"Failed to log training run to DB: {e}")
        session.rollback()

# ============================================================
# Pick-success classifier: trains on the system's own real BUY /
# PREPARE TO BUY calls and their actual T+1 outcomes, instead of the
# generic OHLCV regressor below (run_training_pipeline / DEFAULT_SYMBOLS),
# which trains on raw price history for a fixed 10-symbol list unrelated
# to what decision-engine actually calls BUY on, and was never loaded
# anywhere to score anything. This is the model scanner.py loads and uses
# at request time; the OHLCV pipeline is kept below for reference but is
# no longer wired to /train.
# ============================================================

PICK_MODEL_FEATURES = SIMILARITY_FEATURES + ["market_score", "event_risk"]

# A KNN lookup (scanner.py) can work meaningfully with very few examples —
# it's just measuring distance. A classifier is fitting real parameters
# across 10 features and needs enough rows for a train/holdout split to
# mean anything; 30 is a floor, not a target.
MIN_TRAINING_SAMPLES = 30


def train_pick_success_model(
    db_session,
    model_store_path: str = "./model-store",
    min_samples: int = MIN_TRAINING_SAMPLES,
    label_source: str = "t1_outcome",
) -> Dict[str, Any]:
    """
    Trains a classifier to predict pick success from the features available
    at the moment decision-engine made the call (combined_score,
    technical_score, fundamental_score, rsi, volume_ratio, debt_to_equity,
    roe, roce, market_score, event_risk).

    label_source controls what "success" means:
      - "t1_outcome" (default): PredictionSnapshot.t1_success — a same/
        next-day heuristic. Requires evaluate.py's update_prediction_success()
        to be wired up (it's called from evaluate_t1/evaluate_t5).
      - "trade_pnl": realized P&L from closed PaperTrade rows (trades.py) —
        whether the simulated position, held to target/stop-loss/max
        holding period against real daily prices, actually made money.

    Honors the same abort_event/check_abort() cooperative-cancellation
    mechanism as run_training_pipeline, via check_abort() at the same kind
    of checkpoints and an AbortCallback on the XGBoost fit — so DELETE
    /lock stops this pipeline the same way it stops the legacy one.
    """
    os.makedirs(model_store_path, exist_ok=True)
    abort_event.clear()
    checker_thread = threading.Thread(target=lock_checker, daemon=True)
    checker_thread.start()
    _train_start_time = time.time()
    write_progress(0, 0, 0, stage="loading_data", detail={"label_source": label_source})

    report: Dict[str, Any] = {
        "model_type": "prediction_success_classifier",
        "label_source": label_source,
        "timestamp": ist_now().isoformat(),
        "status": "insufficient_data",
        "dataset_size": 0,
        "num_symbols": 0,
        "model_version": None,
        "metrics": {},
    }

    try:
        check_abort()

        if not HAS_DB:
            logger.warning("No DB layer available — cannot train pick-success model")
            write_progress(0, 0, time.time() - _train_start_time, stage="idle", detail={"reason": "no_db"})
            return report

        rows = []
        if label_source == "trade_pnl":
            closed_trades = (
                db_session.query(db_models.PaperTrade)
                .filter(db_models.PaperTrade.status == "CLOSED")
                .order_by(db_models.PaperTrade.entry_date.asc())
                .all()
            )
            pred_by_id = {
                p.prediction_id: p
                for p in db_session.query(db_models.PredictionSnapshot).filter(
                    db_models.PredictionSnapshot.prediction_id.in_(
                        [t.prediction_id for t in closed_trades]
                    )
                ).all()
            }
            for trade in closed_trades:
                pred = pred_by_id.get(trade.prediction_id)
                if pred is None:
                    continue
                row = {col: getattr(pred, col, None) for col in PICK_MODEL_FEATURES}
                row["event_risk"] = float(bool(row.get("event_risk")))
                row["label"] = 1 if (trade.pnl_amount or 0) > 0 else 0
                row["timestamp"] = pred.timestamp
                row["symbol"] = pred.symbol
                rows.append(row)
        elif label_source == "t1_outcome":
            evaluated = (
                db_session.query(db_models.PredictionSnapshot)
                .filter(db_models.PredictionSnapshot.t1_success.in_([1, 2]))
                .order_by(db_models.PredictionSnapshot.timestamp.asc())
                .all()
            )
            for pred in evaluated:
                row = {col: getattr(pred, col, None) for col in PICK_MODEL_FEATURES}
                row["event_risk"] = float(bool(row.get("event_risk")))
                row["label"] = 1 if pred.t1_success == 1 else 0
                row["timestamp"] = pred.timestamp
                row["symbol"] = pred.symbol
                rows.append(row)
        else:
            raise ValueError(f"Unknown label_source: {label_source!r}")

        report["dataset_size"] = len(rows)
        report["num_symbols"] = len({r["symbol"] for r in rows})
        write_progress(
            0, 0, time.time() - _train_start_time, stage="data_loaded",
            detail={
                "dataset_size": len(rows),
                "num_symbols": len({r["symbol"] for r in rows}),
                "symbols_sample": sorted({r["symbol"] for r in rows})[:15],
            },
        )

        if len(rows) < min_samples:
            logger.info(
                "Only %d labeled examples for label_source=%s (need %d) — skipping training",
                len(rows), label_source, min_samples,
            )
            write_progress(0, 0, time.time() - _train_start_time, stage="idle",
                            detail={"reason": "insufficient_data", "dataset_size": len(rows)})
            return report

        check_abort()
        df = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)

        if df["label"].nunique() < 2:
            logger.warning(
                "All %d labeled examples have the same outcome — a classifier "
                "can't learn from a single class yet. Skipping training.",
                len(df),
            )
            report["status"] = "single_class_only"
            write_progress(0, 0, time.time() - _train_start_time, stage="idle",
                            detail={"reason": "single_class_only"})
            return report

        split_idx = int(len(df) * 0.8)
        train_df, holdout_df = df.iloc[:split_idx], df.iloc[split_idx:]
        write_progress(
            0, 0, time.time() - _train_start_time, stage="splitting",
            detail={"train_samples": len(train_df), "holdout_samples": len(holdout_df)},
        )

        # Median-impute on the training split only (then apply the same
        # fill values to holdout) so a column that's currently always-null
        # upstream (rsi/volume_ratio, pending technical-analysis-service)
        # doesn't produce NaNs, without leaking holdout stats into training.
        #
        # If a column is entirely missing across every training row (true
        # right now for rsi/volume_ratio), its median is itself NaN, and
        # fillna(NaN) is a no-op — the NaN would reach the scaler. That
        # doesn't currently hard-crash only because RobustScaler happens
        # to tolerate NaN internally (nanmedian/nanquantile) and XGBoost
        # happens to treat the result as "missing" — an accidental safety
        # net that breaks the moment either component changes. Falling
        # back to 0.0 here makes the behavior explicit and correct
        # regardless of which scaler/model is used later.
        fill_values = train_df[SIMILARITY_FEATURES].median(numeric_only=True)
        fill_values = fill_values.reindex(SIMILARITY_FEATURES).fillna(0.0)
        train_df = train_df.fillna(fill_values)
        holdout_df = holdout_df.fillna(fill_values)

        X_train = train_df[PICK_MODEL_FEATURES].astype(np.float32).values
        y_train = train_df["label"].values
        X_holdout = holdout_df[PICK_MODEL_FEATURES].astype(np.float32).values
        y_holdout = holdout_df["label"].values

        scaler = TimeAwareScaler(scaler_type="RobustScaler")
        X_train_scaled = scaler.fit_transform(X_train)

        check_abort()
        write_progress(0, 0, time.time() - _train_start_time, stage="fitting_model",
                        detail={"n_estimators": 200, "train_samples": len(train_df)})
        model = xgb.XGBClassifier(
            n_estimators=200,
            max_depth=4,
            learning_rate=0.05,
            subsample=0.9,
            colsample_bytree=0.9,
            eval_metric="logloss",
            random_state=42,
            callbacks=[AbortCallback()],
        )
        try:
            model.fit(X_train_scaled, y_train)
        except KeyboardInterrupt:
            logger.warning("Pick-success classifier training aborted during fit.")
            raise

        check_abort()
        write_progress(0, 0, time.time() - _train_start_time, stage="evaluating",
                        detail={"holdout_samples": len(holdout_df)})

        metrics: Dict[str, Any] = {"train_size": len(train_df), "holdout_size": len(holdout_df)}
        if len(holdout_df) > 0:
            X_holdout_scaled = scaler.transform(X_holdout)
            y_pred = model.predict(X_holdout_scaled)
            metrics["accuracy"] = accuracy_score(y_holdout, y_pred)
            metrics["precision"] = precision_score(y_holdout, y_pred, zero_division=0)
            metrics["recall"] = recall_score(y_holdout, y_pred, zero_division=0)
            metrics["f1"] = f1_score(y_holdout, y_pred, zero_division=0)
            if len(set(y_holdout)) > 1:
                y_proba = model.predict_proba(X_holdout_scaled)[:, 1]
                metrics["roc_auc"] = roc_auc_score(y_holdout, y_proba)

        config = {
            "model_type": "prediction_success_classifier",
            "label_source": label_source,
            "feature_columns": PICK_MODEL_FEATURES,
            "min_samples": min_samples,
            "trained_at": ist_now().isoformat(),
        }

        model_version = None
        promotion_note = "no_model_registry"
        write_progress(0, 0, time.time() - _train_start_time, stage="saving_model",
                        detail={"metrics": convert_numpy(metrics)})
        if HAS_MODEL_REGISTRY:
            registry = ModelRegistry()
            candidate_version = registry.save_candidate_model(
                model, scaler, config, convert_numpy(metrics), feature_columns=PICK_MODEL_FEATURES
            )
            logger.info(f"Pick-success classifier saved as candidate {candidate_version}")

            # Champion/challenger: don't blindly overwrite whatever is
            # live. A training run that lands on an unlucky holdout split
            # or overfits shouldn't silently degrade the model actually
            # being used for scoring. Compare against current production
            # on the same metric it was actually evaluated on.
            should_promote = True
            promotion_note = "promoted: no existing production model to compare against"
            try:
                current_prod = registry.get_production_model()
            except Exception as e:
                current_prod = None
                logger.warning(f"Could not read current production model for comparison: {e}")

            if current_prod is not None:
                _, _, prod_meta = current_prod
                prod_label_source = (prod_meta.get("config") or {}).get("label_source")
                prod_metrics = prod_meta.get("metrics") or {}
                if prod_label_source != label_source:
                    # Different target definition — a t1_outcome F1 and a
                    # trade_pnl F1 aren't measuring the same thing, so
                    # there's nothing valid to compare. Promote directly
                    # rather than compare apples to oranges.
                    promotion_note = (
                        f"promoted: existing production model used label_source="
                        f"{prod_label_source!r}, not directly comparable to this "
                        f"run's {label_source!r}"
                    )
                else:
                    new_f1 = metrics.get("f1")
                    prod_f1 = prod_metrics.get("f1")
                    if new_f1 is None or prod_f1 is None:
                        promotion_note = "promoted: f1 unavailable on one side, defaulting to promote"
                    elif new_f1 >= prod_f1:
                        promotion_note = f"promoted: new f1={new_f1:.3f} >= current production f1={prod_f1:.3f}"
                    else:
                        should_promote = False
                        promotion_note = (
                            f"kept existing production model: new f1={new_f1:.3f} < "
                            f"current production f1={prod_f1:.3f} (candidate {candidate_version} "
                            f"saved but not promoted)"
                        )

            if should_promote:
                registry.promote_model(candidate_version)
                model_version = candidate_version
            else:
                model_version = None  # production unchanged; report still names the candidate below

            logger.info(promotion_note)
        else:
            logger.warning("HAS_MODEL_REGISTRY is False — trained model was not persisted anywhere")
            candidate_version = None

        save_training_run_to_db(
            session=db_session, config=config, metrics=convert_numpy(metrics),
            fold_details=[{
                "split": "chronological_80_20",
                "train_samples": len(train_df), "holdout_samples": len(holdout_df),
                "promotion_note": promotion_note,
            }],
            # candidate_version, not model_version: this history should show
            # every training attempt, including ones that trained fine but
            # weren't promoted because they didn't beat production — using
            # model_version here would silently drop those from the record.
            model_version=candidate_version, dataset_size=len(df), num_symbols=df["symbol"].nunique(),
        )

        report.update({
            "status": "trained",
            "model_version": model_version,  # None if trained but not promoted
            "candidate_version": candidate_version,
            "promoted": model_version is not None,
            "promotion_note": promotion_note,
            "metrics": convert_numpy(metrics),
        })
        write_progress(0, 0, time.time() - _train_start_time, stage="done",
                        detail={"promoted": model_version is not None, "promotion_note": promotion_note,
                                "metrics": convert_numpy(metrics)})
        return report

    except KeyboardInterrupt as e:
        logger.warning(f"Pick-success classifier training interrupted: {e}")
        report["status"] = "aborted"
        write_progress(0, 0, time.time() - _train_start_time, stage="aborted", detail={})
        return report
    finally:
        abort_event.set()


# ---------- Main Training Pipeline (legacy — no longer called by /train) ----------
def run_training_pipeline(
    config: Dict[str, Any],
    db_session: Optional[Session] = None,
    model_store_path: str = "./model-store",
) -> Dict[str, Any]:
    np.random.seed(config['random_seed'])
    random.seed(config['random_seed'])
    os.makedirs(model_store_path, exist_ok=True)

    # Ensure abort event is cleared at start
    abort_event.clear()
    logger.info("run_training_pipeline started, abort_event cleared")

    # Start the lock‑checking thread
    checker_thread = threading.Thread(target=lock_checker, daemon=True)
    checker_thread.start()

    try:
        # Check abort before heavy work
        check_abort()

        symbols = config['data']['symbols']
        logger.info(f"Fetching data for {len(symbols)} symbols...")
        df = build_multi_symbol_dataset(symbols, period=config['data']['period'])
        logger.info(f"Total dataset shape: {df.shape}")

        feature_cols = FEATURE_COLUMNS
        target_gen = TargetGenerator(
            target_type=config['target_type'],
            forecast_horizon=config['forecast_horizon_days'],
            price_col='close'
        )

        def apply_targets(group):
            t, g = target_gen.generate(group, inplace=False)
            group['target'] = t
            group['pct_return'] = g['pct_return']
            group['log_return'] = g['log_return']
            return group

        df = df.groupby('symbol', group_keys=False).apply(apply_targets)
        df = df.dropna(subset=['target'])
        logger.info(f"After target generation: {df.shape}")

        vc = config['validation_strategy']
        splitter = WalkForwardSplitter(
            train_window=vc['train_window_size'],
            val_window=vc['validation_window_size'],
            step_size=vc.get('step_size', vc['validation_window_size']),
            embargo_days=vc.get('embargo_days', config['forecast_horizon_days']),
            forecast_horizon=config['forecast_horizon_days'],
            method=vc['method']
        )

        df = df.sort_values('date').reset_index(drop=True)
        folds = splitter.split(df)
        total_folds = len(folds)
        logger.info(f"Number of folds: {total_folds}")
        if not folds:
            logger.error("No folds generated – insufficient data.")
            return None

        # Write initial progress
        write_progress(0, total_folds)

        all_preds = []
        all_actuals = []
        all_strategy_returns = []
        fold_reports = []
        start_time = time.time()

        for i, fold in enumerate(folds):
            # Check abort before each fold
            check_abort()

            logger.info(f"\n--- Fold {i+1}/{total_folds} ---")
            train_idx = list(range(fold.train_start, fold.train_end + 1))
            val_idx = list(range(fold.val_start, fold.val_end + 1))

            train_data = df.iloc[train_idx].copy()
            val_data = df.iloc[val_idx].copy()

            X_train = train_data[feature_cols].values.astype(np.float32)
            y_train = train_data['target'].values.astype(np.float32)
            X_val = val_data[feature_cols].values.astype(np.float32)
            y_val = val_data['target'].values.astype(np.float32)

            scaler = TimeAwareScaler(config['preprocessing']['scaler_type'])
            X_train_scaled = scaler.fit_transform(X_train)
            X_val_scaled = scaler.transform(X_val)

            # Create model with the abort callback
            model = xgb.XGBRegressor(
                **config['model'],
                random_state=config['random_seed'],
                eval_metric='rmse',
                callbacks=[AbortCallback()]   # <-- NEW: stops mid-fit
            )
            # Check abort before fit
            check_abort()

            try:
                model.fit(X_train_scaled, y_train, eval_set=[(X_val_scaled, y_val)], verbose=False)
            except KeyboardInterrupt:
                # Re-raise to be caught by the outer handler
                logger.warning("Training aborted during XGBoost fit.")
                raise

            pred_val = model.predict(X_val_scaled)

            trade_cfg = config['trading']
            trade_sim = TradingSimulator(
                long_threshold=trade_cfg['long_threshold'],
                short_threshold=trade_cfg['short_threshold'],
                transaction_cost_bps=trade_cfg['transaction_cost_bps'],
                slippage_bps=trade_cfg['slippage_bps'],
                allow_short=trade_cfg['allow_short']
            )
            signals, costs, strategy_ret = trade_sim.simulate(pred_val, y_val)

            all_preds.extend(pred_val)
            all_actuals.extend(y_val)
            all_strategy_returns.extend(strategy_ret)

            fold_reports.append({
                'fold': i+1,
                'train_start': df.iloc[fold.train_start]['date'].strftime('%Y-%m-%d'),
                'train_end': df.iloc[fold.train_end]['date'].strftime('%Y-%m-%d'),
                'val_start': df.iloc[fold.val_start]['date'].strftime('%Y-%m-%d'),
                'val_end': df.iloc[fold.val_end]['date'].strftime('%Y-%m-%d'),
                'train_samples': len(train_data),
                'val_samples': len(val_data)
            })

            del train_data, val_data, X_train, X_val, y_train, y_val, model
            gc.collect()

            # Write progress after each fold
            elapsed = int(time.time() - start_time)
            write_progress(i+1, total_folds, elapsed)

            # Check abort after fold (in case lock was removed during fit)
            check_abort()

        # After folds, check abort before final training
        check_abort()

        all_preds = np.array(all_preds)
        all_actuals = np.array(all_actuals)
        all_strategy_returns = np.array(all_strategy_returns)

        metrics = compute_all_metrics(all_preds, all_actuals, all_strategy_returns)

        logger.info("\n" + "="*50)
        logger.info("WALK‑FORWARD OOS PERFORMANCE")
        for k,v in metrics.items():
            logger.info(f"{k:>20}: {v:.4f}" if isinstance(v, float) else f"{k:>20}: {v}")
        logger.info("="*50)

        # Train final production model (with abort callback too)
        logger.info("Training production model on full dataset...")
        X_full = df[feature_cols].values.astype(np.float32)
        y_full = df['target'].values.astype(np.float32)

        final_scaler = TimeAwareScaler(config['preprocessing']['scaler_type'])
        X_full_scaled = final_scaler.fit_transform(X_full)

        final_model = xgb.XGBRegressor(
            **config['model'],
            random_state=config['random_seed'],
            eval_metric='rmse',
            callbacks=[AbortCallback()]   # <-- NEW
        )
        # Check abort before final fit
        check_abort()
        try:
            final_model.fit(X_full_scaled, y_full)
        except KeyboardInterrupt:
            logger.warning("Training aborted during final XGBoost fit.")
            raise

        # Check abort before saving model
        check_abort()

        model_version = None
        if HAS_MODEL_REGISTRY:
            registry = ModelRegistry()
            model_version = registry.save_production_model(final_model, final_scaler, config, metrics)
            logger.info(f"Production model saved with version: {model_version}")
        else:
            joblib.dump(final_model, os.path.join(model_store_path, 'model.pkl'))
            joblib.dump(final_scaler, os.path.join(model_store_path, 'scaler.pkl'))
            with open(os.path.join(model_store_path, 'training_config.json'), 'w') as f:
                json.dump(convert_numpy(config), f, indent=2)
            logger.info(f"Production model saved under {model_store_path} (legacy mode)")

        report = {
            'timestamp': ist_now().isoformat(),
            'dataset_size': len(df),
            'num_symbols': len(df['symbol'].unique()),
            'walk_forward_metrics': convert_numpy(metrics),
            'fold_details': convert_numpy(fold_reports),
            'production_model_saved': True,
            'model_version': model_version,
            'config': convert_numpy(config)
        }

        report_path = os.path.join(model_store_path, 'training_report.joblib')
        joblib.dump(report, report_path)
        logger.info(f"Training report saved to {report_path}")

        # Log training run to DB if session provided
        if HAS_DB and db_session is not None:
            save_training_run_to_db(
                db_session,
                config,
                metrics,
                fold_reports,
                model_version,
                len(df),
                len(df['symbol'].unique())
            )

        return report

    except KeyboardInterrupt as e:
        logger.warning(f"Training interrupted: {e}")
        # Write progress with current fold to indicate interruption
        write_progress(-1, total_folds)  # -1 means aborted
        raise  # re-raise to be caught by outer handler
    except Exception as e:
        logger.error(f"Training pipeline error: {e}")
        raise
    finally:
        # Signal the checker thread to stop
        abort_event.set()
        # Clean up progress file
        try:
            if os.path.exists(PROGRESS_FILE):
                os.remove(PROGRESS_FILE)
        except:
            pass

# ============================================================
# Entry point for FastAPI background task
# ============================================================
def train_model(db_session, model_store_path: str, label_source: str = "t1_outcome"):
    """
    Training entry point called by app.py's /train endpoint.

    Trains the pick-success classifier (train_pick_success_model) on the
    system's own recorded predictions and their real outcomes — not
    the OHLCV regressor (run_training_pipeline) below, which is kept for
    reference but is no longer the active training path.

    Args:
        db_session: SQLAlchemy session to use for logging
        model_store_path: Path to store trained models
        label_source: 't1_outcome' (default) or 'trade_pnl'
    """
    logger.info("=" * 60)
    logger.info(f"TRAINING STARTED (via train_model, label_source={label_source})")
    logger.info("=" * 60)

    model_store_path = model_store_path or os.environ.get("MODEL_STORE_PATH", "./model-store")

    try:
        report = train_pick_success_model(
            db_session, model_store_path=model_store_path, label_source=label_source
        )
        if report.get("status") == "trained":
            logger.info("Training completed successfully")
            logger.info(f"Dataset size: {report.get('dataset_size', 0)}")
            logger.info(f"Model version: {report.get('model_version', 'unknown')}")
        else:
            logger.info(f"Training did not produce a model: {report.get('status')}")
    except KeyboardInterrupt:
        logger.warning("Training aborted by user (KeyboardInterrupt).")
        # No need to raise; the lock will be released by the outer try-finally in app.py
    except Exception as e:
        logger.error(f"Training failed: {e}")
        if db_session is not None:
            db_session.rollback()
        raise

# ---------- Entry point ----------
if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Run Stockky training pipeline')
    parser.add_argument('--config', type=str, help='Path to training config JSON file (legacy OHLCV pipeline only)')
    parser.add_argument('--no-db', action='store_true', help='Disable database logging')
    parser.add_argument(
        '--legacy-ohlcv', action='store_true',
        help='Run the old OHLCV-regressor pipeline instead of the pick-success '
             'classifier that /train now uses. Kept for reference/comparison.',
    )
    args = parser.parse_args()

    if args.no_db:
        HAS_DB = False

    if args.legacy_ohlcv:
        config = DEFAULT_TRAINING_CONFIG
        if args.config and os.path.exists(args.config):
            with open(args.config, 'r') as f:
                config = json.load(f)
            logger.info(f"Loaded config from {args.config}")
        else:
            logger.info("Using default configuration")
        run_training_pipeline(
            config, db_session=None,
            model_store_path=os.environ.get("MODEL_STORE_PATH", "./model-store"),
        )
    else:
        db_session = None
        if HAS_DB:
            from sqlalchemy import create_engine
            from sqlalchemy.orm import sessionmaker
            db_url = os.environ.get("DATABASE_URL", "sqlite:///./training.db")
            engine = create_engine(db_url)
            db_models.Base.metadata.create_all(engine)
            db_session = sessionmaker(bind=engine)()
        train_model(db_session, os.environ.get("MODEL_STORE_PATH", "./model-store"))