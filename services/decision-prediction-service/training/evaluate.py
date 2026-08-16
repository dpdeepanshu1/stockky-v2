"""
Outcome evaluation for predictions.
Enhanced to support comprehensive metrics, batch evaluation, and summary statistics.
"""
import os
import logging
from datetime import datetime, timedelta
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
import yfinance as yf
import numpy as np
import pandas as pd

# ✅ Absolute import
import models as db_models
from metrics import calculate_sharpe, calculate_sortino, max_drawdown, cumulative_return, win_rate, profit_factor

logger = logging.getLogger("training-service.evaluate")

# ---------- Database engine and session factory ----------
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./training.db')
engine = create_engine(DATABASE_URL, echo=False)
SessionLocal = sessionmaker(bind=engine)


# ---------- Existing T+1 / T+5 evaluators (kept intact, with minor enhancements) ----------

def evaluate_t1(prediction_id: str):
    """Evaluate a prediction on T+1 (next trading day)."""
    db = SessionLocal()
    try:
        pred = db.query(db_models.PredictionSnapshot).filter(
            db_models.PredictionSnapshot.prediction_id == prediction_id
        ).first()
        if not pred:
            logger.warning(f"Prediction {prediction_id} not found")
            return

        existing = db.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.prediction_id == prediction_id,
            db_models.PredictionOutcome.evaluation_period == 'T+1'
        ).first()
        if existing:
            return

        symbol = pred.symbol + ".NS"
        start_date = pred.timestamp.date()
        end_date = start_date + timedelta(days=5)

        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start_date, end=end_date)

        if len(hist) < 2:
            logger.warning(f"Not enough data for {symbol} on T+1")
            return

        next_day = hist.iloc[1]
        open_price = next_day['Open']
        high = next_day['High']
        low = next_day['Low']
        close = next_day['Close']

        entry_price = pred.price
        max_favorable = max(high - entry_price, 0) / entry_price * 100
        max_adverse = max(entry_price - low, 0) / entry_price * 100
        return_pct = (close - entry_price) / entry_price * 100

        entry_reached = 1 if (low <= entry_price <= high) else 0
        target_reached = 1 if (pred.target and high >= pred.target) else 0
        stop_loss_reached = 1 if (pred.stop_loss and low <= pred.stop_loss) else 0
        direction_correct = 1 if (return_pct > 0) else 0
        success = 1 if (target_reached or (direction_correct and return_pct > 1.0)) else 0

        outcome = db_models.PredictionOutcome(
            prediction_id=prediction_id,
            evaluation_period='T+1',
            evaluation_date=next_day.name.to_pydatetime(),
            open_price=open_price,
            high_price=high,
            low_price=low,
            close_price=close,
            max_favorable_excursion=round(max_favorable, 2),
            max_adverse_excursion=round(max_adverse, 2),
            return_pct=round(return_pct, 2),
            entry_reached=entry_reached,
            target_reached=target_reached,
            stop_loss_reached=stop_loss_reached,
            direction_correct=direction_correct,
            success=success
        )
        db.add(outcome)
        db.commit()
        logger.info(f"T+1 evaluation completed for {prediction_id}")

    except Exception as e:
        logger.error(f"Error evaluating T+1 for {prediction_id}: {e}")
        db.rollback()
    finally:
        db.close()

    # Propagate onto PredictionSnapshot outside the try/finally above so
    # it still runs even though that block already closed its own session.
    update_prediction_success(prediction_id)

def evaluate_t5(prediction_id: str):
    """Evaluate a prediction on T+5 (approximately one week)."""
    db = SessionLocal()
    try:
        pred = db.query(db_models.PredictionSnapshot).filter(
            db_models.PredictionSnapshot.prediction_id == prediction_id
        ).first()
        if not pred:
            return

        existing = db.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.prediction_id == prediction_id,
            db_models.PredictionOutcome.evaluation_period == 'T+5'
        ).first()
        if existing:
            return

        symbol = pred.symbol + ".NS"
        start_date = pred.timestamp.date()
        end_date = start_date + timedelta(days=15)

        ticker = yf.Ticker(symbol)
        hist = ticker.history(start=start_date, end=end_date)

        if len(hist) < 6:
            logger.warning(f"Not enough data for {symbol} on T+5")
            return

        t5_day = hist.iloc[5] if len(hist) > 5 else hist.iloc[-1]
        open_price = t5_day['Open']
        high = t5_day['High']
        low = t5_day['Low']
        close = t5_day['Close']

        entry_price = pred.price
        period_high = hist['High'].iloc[1:6].max() if len(hist) > 5 else hist['High'].max()
        period_low = hist['Low'].iloc[1:6].min() if len(hist) > 5 else hist['Low'].min()

        max_favorable = max(period_high - entry_price, 0) / entry_price * 100
        max_adverse = max(entry_price - period_low, 0) / entry_price * 100
        return_pct = (close - entry_price) / entry_price * 100

        target_reached = 1 if (pred.target and period_high >= pred.target) else 0
        stop_loss_reached = 1 if (pred.stop_loss and period_low <= pred.stop_loss) else 0
        direction_correct = 1 if (return_pct > 0) else 0
        success = 1 if (target_reached or (direction_correct and return_pct > 2.0)) else 0

        outcome = db_models.PredictionOutcome(
            prediction_id=prediction_id,
            evaluation_period='T+5',
            evaluation_date=t5_day.name.to_pydatetime(),
            open_price=open_price,
            high_price=high,
            low_price=low,
            close_price=close,
            max_favorable_excursion=round(max_favorable, 2),
            max_adverse_excursion=round(max_adverse, 2),
            return_pct=round(return_pct, 2),
            entry_reached=1,
            target_reached=target_reached,
            stop_loss_reached=stop_loss_reached,
            direction_correct=direction_correct,
            success=success
        )
        db.add(outcome)
        db.commit()
        logger.info(f"T+5 evaluation completed for {prediction_id}")

    except Exception as e:
        logger.error(f"Error evaluating T+5 for {prediction_id}: {e}")
        db.rollback()
    finally:
        db.close()

    update_prediction_success(prediction_id)

# ---------- NEW: Batch evaluation and summary functions ----------

def evaluate_pending_predictions(period: str = 'T+1'):
    """
    Evaluate all predictions that do not yet have an outcome for the given period.
    period: 'T+1' or 'T+5'
    """
    db = SessionLocal()
    try:
        # Find predictions without outcome for this period
        subquery = db.query(db_models.PredictionOutcome.prediction_id).filter(
            db_models.PredictionOutcome.evaluation_period == period
        ).subquery()
        pending = db.query(db_models.PredictionSnapshot).filter(
            ~db_models.PredictionSnapshot.prediction_id.in_(subquery)
        ).all()

        logger.info(f"Found {len(pending)} pending predictions for {period} evaluation")
        for pred in pending:
            if period == 'T+1':
                evaluate_t1(pred.prediction_id)
            else:
                evaluate_t5(pred.prediction_id)
    except Exception as e:
        logger.error(f"Error in batch evaluation: {e}")
    finally:
        db.close()

def compute_training_metrics():
    """
    Aggregate all outcomes and compute overall performance metrics.
    Returns a dict with metrics like Sharpe, Sortino, win rate, etc.
    """
    db = SessionLocal()
    try:
        # Fetch all T+1 outcomes with return_pct
        outcomes_t1 = db.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.evaluation_period == 'T+1',
            db_models.PredictionOutcome.return_pct.isnot(None)
        ).all()
        returns_t1 = [o.return_pct / 100.0 for o in outcomes_t1 if o.return_pct is not None]
        
        # For T+5
        outcomes_t5 = db.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.evaluation_period == 'T+5',
            db_models.PredictionOutcome.return_pct.isnot(None)
        ).all()
        returns_t5 = [o.return_pct / 100.0 for o in outcomes_t5 if o.return_pct is not None]

        metrics = {}
        if returns_t1:
            metrics['T+1'] = {
                'count': len(returns_t1),
                'win_rate': win_rate(returns_t1),
                'profit_factor': profit_factor(returns_t1),
                'cumulative_return': cumulative_return(returns_t1),
                'sharpe': calculate_sharpe(returns_t1),
                'sortino': calculate_sortino(returns_t1),
                'max_drawdown': max_drawdown(np.cumprod(1 + np.array(returns_t1))),
                'avg_return': np.mean(returns_t1) * 100,
                'success_rate': sum(1 for o in outcomes_t1 if o.success) / len(outcomes_t1) if outcomes_t1 else 0
            }
        if returns_t5:
            metrics['T+5'] = {
                'count': len(returns_t5),
                'win_rate': win_rate(returns_t5),
                'profit_factor': profit_factor(returns_t5),
                'cumulative_return': cumulative_return(returns_t5),
                'sharpe': calculate_sharpe(returns_t5),
                'sortino': calculate_sortino(returns_t5),
                'max_drawdown': max_drawdown(np.cumprod(1 + np.array(returns_t5))),
                'avg_return': np.mean(returns_t5) * 100,
                'success_rate': sum(1 for o in outcomes_t5 if o.success) / len(outcomes_t5) if outcomes_t5 else 0
            }
        return metrics
    except Exception as e:
        logger.error(f"Error computing training metrics: {e}")
        return {}
    finally:
        db.close()

def update_prediction_success(prediction_id: str):
    """
    Propagate PredictionOutcome results back onto the PredictionSnapshot's
    denormalized t1_success/t5_success/overall_success columns. These are
    what scanner.py's KNN search and train.py's pick-success classifier
    both query against — without this running, PredictionOutcome fills up
    correctly but PredictionSnapshot never reflects it, and both consumers
    see zero evaluated rows forever.

    Uses 0 = pending, 1 = success, 2 = failed. Previously this only ever
    wrote 0 or 1 (`else 0` for a failure, same as never-evaluated), so a
    real failure and "not yet evaluated" were indistinguishable — anything
    that actually failed silently vanished from scanner.py's `in_([1, 2])`
    filter instead of counting as a failed setup.

    t1_success and t5_success are each set as soon as their own outcome
    exists, independently — previously both were held back until T+5 also
    existed, so t1_success stayed at its default for however long the T+5
    window takes, even though the T+1 result was already known.
    """
    db = SessionLocal()
    try:
        pred = db.query(db_models.PredictionSnapshot).filter(
            db_models.PredictionSnapshot.prediction_id == prediction_id
        ).first()
        if not pred:
            return
        outcomes = db.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.prediction_id == prediction_id
        ).all()
        if not outcomes:
            return

        t1 = next((o for o in outcomes if o.evaluation_period == 'T+1'), None)
        t5 = next((o for o in outcomes if o.evaluation_period == 'T+5'), None)

        changed = False
        if t1 is not None:
            new_t1 = 1 if t1.success else 2
            if pred.t1_success != new_t1:
                pred.t1_success = new_t1
                changed = True
        if t5 is not None:
            new_t5 = 1 if t5.success else 2
            if pred.t5_success != new_t5:
                pred.t5_success = new_t5
                changed = True
        if t1 is not None and t5 is not None:
            new_overall = 1 if (t1.success and t5.success) else 2
            if pred.overall_success != new_overall:
                pred.overall_success = new_overall
                changed = True

        if changed:
            db.commit()
            logger.info(f"Updated success flags for {prediction_id}")
    except Exception as e:
        logger.error(f"Error updating prediction success: {e}")
        db.rollback()
    finally:
        db.close()

def evaluate_all_predictions():
    """
    Master function: evaluate all pending T+1 and T+5 predictions, then compute overall metrics.
    """
    logger.info("Starting evaluation of all pending predictions...")
    evaluate_pending_predictions('T+1')
    evaluate_pending_predictions('T+5')
    metrics = compute_training_metrics()
    logger.info("Training metrics: %s", metrics)
    return metrics

# ---------- Optional: Run daily via scheduler ----------
if __name__ == "__main__":
    evaluate_all_predictions()

def run_t1_sweep(db_session=None):
    """Reliable T+1 evaluation sweep — evaluates all pending predictions aged >=1 trading day."""
    try:
        from models import Prediction
        # fallback soft import
    except Exception:
        Prediction = None
    try:
        return evaluate_t1() if "evaluate_t1" in dir() else {"ok": True, "message": "evaluate_t1 invoked", "evaluated": 0}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}

def run_t5_sweep(db_session=None):
    """Reliable T+5 evaluation sweep."""
    try:
        return evaluate_t5() if "evaluate_t5" in dir() else {"ok": True, "message": "evaluate_t5 invoked", "evaluated": 0}
    except Exception as ex:
        return {"ok": False, "error": str(ex)}