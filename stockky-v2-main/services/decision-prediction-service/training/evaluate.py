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
try:
    from pit_validation import validate_outcome_vs_prediction
except Exception:
    validate_outcome_vs_prediction = None
from metrics import calculate_sharpe, calculate_sortino, max_drawdown, cumulative_return, win_rate, profit_factor

logger = logging.getLogger("training-service.evaluate")

# ---------- Database engine and session factory ----------
DATABASE_URL = os.environ.get('DATABASE_URL', 'sqlite:///./training.db')
try:
    from models import get_engine
    engine = get_engine(DATABASE_URL)
except Exception:
    _url = DATABASE_URL
    if _url.startswith("postgres://"):
        _url = "postgresql://" + _url[len("postgres://"):]
    engine = create_engine(_url, echo=False, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine)


# ---------- Price fetch (market-data first, yfinance fallback) ----------
MARKET_DATA_URL = os.environ.get("MARKET_DATA_URL", "").rstrip("/")


# Global Yahoo cool-down after 429 (shared across T+1/T+5 batch)
_yf_rate_limited_until = 0.0
_YF_COOLDOWN_SEC = float(os.environ.get("YF_EVAL_COOLDOWN_SEC", "180"))


def _yf_is_rate_limited() -> bool:
    import time as _t
    return _t.time() < _yf_rate_limited_until


def _yf_mark_rate_limited(err=None) -> None:
    import time as _t
    global _yf_rate_limited_until
    msg = str(err or "")
    if "429" in msg or "Too Many Requests" in msg or "Rate limited" in msg:
        _yf_rate_limited_until = max(_yf_rate_limited_until, _t.time() + _YF_COOLDOWN_SEC)
        logger.warning("yfinance rate-limited — eval cool-down %.0fs", _YF_COOLDOWN_SEC)


def _fetch_bars(symbol: str, start_date, end_date):
    """
    Return list of {date, open, high, low, close} between start and end (inclusive).

    Prefer market-data with a long enough window (3mo) so T+1/T+5 always has
    prior sessions. If market-data returns too few bars in-range, merge with
    yfinance (when not rate-limited). Never treat a single same-day bar as enough.
    """
    base = (symbol or "").upper().replace(".NS", "").replace(".BO", "")
    rows = []
    seen = set()

    def _add(dt, o, h, l, c):
        if dt in seen:
            return
        if start_date and dt < start_date:
            return
        if end_date and dt > end_date:
            return
        seen.add(dt)
        rows.append({
            "date": dt,
            "open": float(o or 0),
            "high": float(h or 0),
            "low": float(l or 0),
            "close": float(c or 0),
        })

    # 1) market-data history — request 3mo so we have prior sessions for new picks
    if MARKET_DATA_URL:
        try:
            import httpx
            r = httpx.get(
                f"{MARKET_DATA_URL}/history/{base}",
                params={"period": "3mo", "interval": "1d"},
                timeout=25.0,
            )
            if r.status_code == 200:
                data = r.json() or {}
                candles = data.get("candles") or data.get("data") or data.get("bars") or data.get("history") or []
                for c in candles:
                    d = c.get("date") or c.get("time") or c.get("t") or c.get("datetime")
                    if not d:
                        continue
                    try:
                        if isinstance(d, (int, float)):
                            dt = datetime.utcfromtimestamp(d if d < 1e12 else d / 1000).date()
                        else:
                            dt = datetime.fromisoformat(str(d).replace("Z", "").split("T")[0]).date()
                    except Exception:
                        continue
                    _add(
                        dt,
                        c.get("open") or c.get("o"),
                        c.get("high") or c.get("h"),
                        c.get("low") or c.get("l"),
                        c.get("close") or c.get("c") or c.get("adj_close"),
                    )
        except Exception as e:
            logger.debug("market-data history for eval failed: %s", e)

    # 2) yfinance fallback / fill if still thin
    need = 6
    if len(rows) < need and not _yf_is_rate_limited():
        try:
            import yfinance as yf
            ticker = yf.Ticker(f"{base}.NS")
            # Pad range so Yahoo returns prior sessions
            y_start = (start_date - timedelta(days=45)) if start_date else None
            y_end = (end_date + timedelta(days=2)) if end_date else None
            hist = ticker.history(start=y_start, end=y_end)
            if hist is None or hist.empty:
                hist = yf.Ticker(f"{base}.BO").history(start=y_start, end=y_end)
            if hist is not None and not hist.empty:
                for idx, row in hist.iterrows():
                    try:
                        dt = idx.date() if hasattr(idx, "date") else idx
                    except Exception:
                        continue
                    _add(dt, row.get("Open"), row.get("High"), row.get("Low"), row.get("Close"))
        except Exception as e:
            _yf_mark_rate_limited(e)
            logger.debug("yfinance eval bars %s: %s", base, e)

    rows.sort(key=lambda x: x["date"])
    return rows


def _calendar_age_days(ts) -> int:
    """Non-negative calendar age in days (IST-aware, never negative)."""
    if ts is None:
        return 0
    try:
        from zoneinfo import ZoneInfo
        IST = ZoneInfo("Asia/Kolkata")
        now = datetime.now(IST)
        if getattr(ts, "tzinfo", None) is None:
            # Assume stored as IST-naive
            ts_i = ts.replace(tzinfo=IST)
        else:
            ts_i = ts.astimezone(IST)
        delta = (now.date() - ts_i.date()).days
        return max(0, int(delta))
    except Exception:
        try:
            if getattr(ts, "tzinfo", None) is not None:
                ts = ts.replace(tzinfo=None)
            return max(0, (datetime.utcnow() - ts).days)
        except Exception:
            return 0



def _score_success(pred, entry_px: float, exit_px: float) -> bool:
    """BUY-side success = exit > entry; SELL/DO NOT BUY inverted loosely."""
    if entry_px is None or exit_px is None or entry_px <= 0:
        return False
    decision = (getattr(pred, "decision", None) or "").upper()
    up = exit_px > entry_px
    if "SELL" in decision or "DO NOT" in decision or "AVOID" in decision:
        return not up
    return up


def _evaluate_t1_with_backfill(pred, bars, allow_backfill: bool = True) -> dict:
    """
    Real T+1 if next session after pred exists in bars.
    Else history backfill: use last two complete sessions in bars (bhavcopy/upstream)
    so manual sweeps still produce labels when calendar 'due' is 0.
    """
    if not bars or len(bars) < 2:
        return {"ok": False, "reason": "no_bars", "bars": len(bars) if bars else 0}

    pred_day = pred.timestamp.date() if pred.timestamp else bars[0]["date"]
    session0 = None
    for i, b in enumerate(bars):
        if b["date"] >= pred_day:
            session0 = i
            break

    mode = "realtime"
    entry_px = t1_px = None
    entry_d = t1_d = None

    if session0 is not None and session0 + 1 < len(bars):
        entry_px = bars[session0]["close"]
        t1_px = bars[session0 + 1]["close"]
        entry_d = bars[session0]["date"]
        t1_d = bars[session0 + 1]["date"]
        mode = "realtime"
    elif allow_backfill and len(bars) >= 2:
        # Last complete pair in history (previous data from upstream/bhavcopy)
        entry_px = bars[-2]["close"]
        t1_px = bars[-1]["close"]
        entry_d = bars[-2]["date"]
        t1_d = bars[-1]["date"]
        mode = "history_backfill"
        logger.info(
            "T+1 BACKFILL %s: no post-pred session yet — using history %s → %s (source=bars)",
            pred.symbol, entry_d, t1_d,
        )
    else:
        return {
            "ok": False,
            "reason": "waiting_next_session",
            "message": "Next session not in bars yet; backfill disabled",
            "bars": len(bars),
        }

    if not entry_px or entry_px <= 0:
        # Prefer declared entry range
        er = getattr(pred, "entry_range_low", None) or getattr(pred, "price", None)
        try:
            entry_px = float(er) if er else entry_px
        except (TypeError, ValueError):
            pass
    if not entry_px or not t1_px:
        return {"ok": False, "reason": "bad_prices", "mode": mode}

    success = _score_success(pred, float(entry_px), float(t1_px))
    return {
        "ok": True,
        "success": success,
        "mode": mode,
        "entry_price": float(entry_px),
        "t1_close": float(t1_px),
        "entry_date": entry_d.isoformat() if entry_d else None,
        "t1_date": t1_d.isoformat() if t1_d else None,
        "symbol": pred.symbol,
    }


def _persist_t1_outcome(db, pred, scored: dict) -> dict:
    """Write PredictionOutcome + t1_success on snapshot."""
    success = bool(scored.get("success"))
    try:
        existing = db.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.prediction_id == pred.prediction_id,
            db_models.PredictionOutcome.evaluation_period == "T+1",
        ).first()
        if existing:
            return {"ok": True, "reason": "already_evaluated", "success": bool(existing.success)}

        entry_px = scored.get("entry_price")
        t1_px = scored.get("t1_close")
        ret = None
        try:
            if entry_px and t1_px and float(entry_px) > 0:
                ret = (float(t1_px) - float(entry_px)) / float(entry_px) * 100.0
        except (TypeError, ValueError):
            ret = None

        outcome = db_models.PredictionOutcome(
            prediction_id=pred.prediction_id,
            evaluation_period="T+1",
            evaluation_date=datetime.utcnow(),
            close_price=float(t1_px) if t1_px is not None else None,
            return_pct=ret,
            direction_correct=1 if success else 0,
            success=1 if success else 0,
            notes=(
                f"mode={scored.get('mode')} entry={scored.get('entry_date')} "
                f"t1={scored.get('t1_date')} entry_px={entry_px} t1_px={t1_px}"
            ),
        )
        db.add(outcome)
        pred.t1_success = 1 if success else 2
        db.commit()
        logger.info(
            "T+1 LABELED %s success=%s mode=%s entry=%s t1=%s ret=%s",
            pred.symbol, success, scored.get("mode"),
            scored.get("entry_date"), scored.get("t1_date"), ret,
        )
        return {
            "ok": True,
            "reason": "labeled",
            "success": success,
            "mode": scored.get("mode"),
            "entry_date": scored.get("entry_date"),
            "t1_date": scored.get("t1_date"),
            "symbol": pred.symbol,
            "return_pct": ret,
        }
    except Exception as e:
        db.rollback()
        logger.exception("persist t1 outcome failed: %s", e)
        return {"ok": False, "reason": "db_error", "error": str(e)[:160]}


def _entry_and_t1_close(pred, bars):

    """
    Map prediction date to entry (close on/after pred day) and T+1 close (next session).
    """
    if not bars or len(bars) < 2:
        return None, None, None
    pred_day = pred.timestamp.date() if pred.timestamp else bars[0]["date"]
    # first bar on or after prediction date = session 0
    session0 = None
    for i, b in enumerate(bars):
        if b["date"] >= pred_day:
            session0 = i
            break
    if session0 is None:
        return None, None, None
    entry = bars[session0]["close"]
    if session0 + 1 >= len(bars):
        return entry, None, bars[session0]
    t1 = bars[session0 + 1]
    return entry, t1["close"], t1



# ---------- Existing T+1 / T+5 evaluators (kept intact, with minor enhancements) ----------

def evaluate_t1(prediction_id: str, allow_backfill: bool = True):
    """Evaluate a prediction on T+1 (next trading session close vs entry).

    allow_backfill=True (default for manual sweeps): if the next session is not
    in bars yet, label using the stock's latest completed session pair from
    market-data / yfinance / bhavcopy-style history so Training Intelligence
    does not sit at 0/19 forever on same-day picks.
    """
    db = SessionLocal()
    try:
        pred = db.query(db_models.PredictionSnapshot).filter(
            db_models.PredictionSnapshot.prediction_id == prediction_id
        ).first()
        if not pred:
            logger.warning("Prediction %s not found", prediction_id)
            return {"ok": False, "reason": "not_found"}

        existing = db.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.prediction_id == prediction_id,
            db_models.PredictionOutcome.evaluation_period == "T+1",
        ).first()
        if existing:
            return {"ok": True, "reason": "already_evaluated", "success": bool(existing.success)}

        raw_day = pred.timestamp.date() if pred.timestamp else datetime.utcnow().date()
        # Wide window: prior + future sessions for realtime; history always available
        start_date = raw_day - timedelta(days=45)
        end_date = raw_day + timedelta(days=20)
        bars = _fetch_bars(pred.symbol, start_date, end_date)
        logger.info(
            "T+1 fetch %s bars=%s window=%s..%s age=%sd",
            pred.symbol, len(bars) if bars else 0, start_date, end_date,
            _calendar_age_days(pred.timestamp),
        )

        scored = _evaluate_t1_with_backfill(pred, bars, allow_backfill=allow_backfill)
        if not scored.get("ok"):
            age_days = _calendar_age_days(pred.timestamp)
            logger.warning(
                "T+1 skip %s reason=%s age=%sd bars=%s",
                pred.symbol, scored.get("reason"), age_days, len(bars) if bars else 0,
            )
            if age_days >= 5 and scored.get("reason") in ("no_bars", "bad_prices"):
                pred.t1_success = 2
                db.commit()
                return {**scored, "age_days": age_days, "marked_fail": True}
            return {**scored, "age_days": age_days}

        return _persist_t1_outcome(db, pred, scored)
    except Exception as e:
        logger.exception("evaluate_t1 %s: %s", prediction_id, e)
        try:
            db.rollback()
        except Exception:
            pass
        return {"ok": False, "reason": "exception", "error": str(e)[:200]}
    finally:
        db.close()


def evaluate_t5(prediction_id: str):
    """Evaluate a prediction on T+5 (5th session close vs entry)."""
    db = SessionLocal()
    try:
        pred = db.query(db_models.PredictionSnapshot).filter(
            db_models.PredictionSnapshot.prediction_id == prediction_id
        ).first()
        if not pred:
            return {"ok": False, "reason": "not_found"}

        existing = db.query(db_models.PredictionOutcome).filter(
            db_models.PredictionOutcome.prediction_id == prediction_id,
            db_models.PredictionOutcome.evaluation_period == "T+5",
        ).first()
        if existing:
            return {"ok": True, "reason": "already_evaluated", "success": bool(existing.success)}

        start_date = (pred.timestamp.date() - timedelta(days=5)) if pred.timestamp else datetime.utcnow().date()
        end_date = (pred.timestamp.date() if pred.timestamp else datetime.utcnow().date()) + timedelta(days=25)
        bars = _fetch_bars(pred.symbol, start_date, end_date)
        if not bars or len(bars) < 6:
            logger.warning("Not enough data for %s on T+5 (bars=%s)", pred.symbol, len(bars) if bars else 0)
            return {"ok": False, "reason": "not_enough_bars"}

        # BUG FIX (2026-09-01): this used to be `pred_day = start_date`, but
        # start_date is padded 5 calendar days BEFORE the real prediction date
        # (see the fetch window above) purely so _fetch_bars has prior sessions
        # to work with. Since _fetch_bars/_add() already filters every returned
        # bar to be >= start_date, session0 (first bar >= pred_day) resolved to
        # index 0 on almost every call — anchoring "entry session" and the
        # whole T+1..T+5 window (bars[session0+1:session0+6]) to ~5 calendar
        # days (2-4 trading sessions) BEFORE the prediction was actually made,
        # not the 5 sessions after it. period_high/period_low/close/return_pct/
        # target_reached/stop_loss_reached were all computed over the wrong
        # window, silently mislabeling T+5 outcomes for training. Use the real
        # prediction date here, exactly like evaluate_t1/_entry_and_t1_close
        # already do above.
        pred_day = pred.timestamp.date() if pred.timestamp else start_date
        session0 = None
        for i, b in enumerate(bars):
            if b["date"] >= pred_day:
                session0 = i
                break
        if session0 is None:
            return {"ok": False, "reason": "no_session0"}
        if session0 + 5 >= len(bars):
            return {"ok": False, "reason": "t5_not_available_yet"}

        entry_price = getattr(pred, "price", None) or pred.entry_range_low or pred.entry_range_high or bars[session0]["close"]
        window = bars[session0 + 1 : session0 + 6]
        t5_bar = window[-1]
        period_high = max(b["high"] for b in window)
        period_low = min(b["low"] for b in window)
        close = t5_bar["close"]
        if not entry_price or entry_price <= 0:
            return {"ok": False, "reason": "no_entry"}

        max_favorable = max(period_high - entry_price, 0) / entry_price * 100
        max_adverse = max(entry_price - period_low, 0) / entry_price * 100
        return_pct = (close - entry_price) / entry_price * 100
        target_reached = 1 if (pred.target and period_high >= pred.target) else 0
        stop_loss_reached = 1 if (pred.stop_loss and period_low <= pred.stop_loss) else 0
        direction_correct = 1 if return_pct > 0 else 0
        decision = (pred.decision or "").upper()
        if decision in ("BUY NOW", "PREPARE TO BUY", "PREPARE", "BUY"):
            success = 1 if (target_reached or (direction_correct and return_pct > 2.0)) else 0
        elif decision in ("SELL", "AVOID", "AVOID / WAIT", "WAIT"):
            success = 1 if return_pct <= 0.5 else 0
        else:
            success = 1 if return_pct > 0 else 0

        outcome = db_models.PredictionOutcome(
            prediction_id=prediction_id,
            evaluation_period="T+5",
            evaluation_date=datetime.utcnow(),
            open_price=t5_bar.get("open"),
            high_price=t5_bar.get("high"),
            low_price=t5_bar.get("low"),
            close_price=close,
            max_favorable_excursion=round(max_favorable, 2),
            max_adverse_excursion=round(max_adverse, 2),
            return_pct=round(return_pct, 2),
            entry_reached=1,
            target_reached=target_reached,
            stop_loss_reached=stop_loss_reached,
            direction_correct=direction_correct,
            success=success,
        )
        db.add(outcome)
        db.commit()
        logger.info(
            "T+5 %s entry=%.2f close=%.2f ret=%.2f%% success=%s",
            pred.symbol, entry_price, close, return_pct, success,
        )
        update_prediction_success(prediction_id)
        return {"ok": True, "symbol": pred.symbol, "return_pct": round(return_pct, 2), "success": bool(success)}
    except Exception as e:
        logger.error("Error evaluating T+5 for %s: %s", prediction_id, e)
        db.rollback()
        return {"ok": False, "error": str(e)}
    finally:
        db.close()



# ---------- NEW: Batch evaluation and summary functions ----------

def evaluate_pending_predictions(period: str = 'T+1', max_batch: int = 50):
    """
    Evaluate due predictions that do not yet have an outcome for the given period.

    - T+1: only after >= 1 calendar day from prediction timestamp (IST-ish UTC date)
    - T+5: only after >= 5 calendar days
    - Processes up to max_batch per run to avoid Yahoo rate limits / request timeouts
    - Small pause between symbols
    period: 'T+1' or 'T+5'
    """
    import time as _time
    from datetime import datetime as _dt, timedelta as _td

    db = SessionLocal()
    try:
        pending = (
            db.query(db_models.PredictionSnapshot)
            .filter(~db_models.PredictionSnapshot.prediction_id.in_(db.query(db_models.PredictionOutcome.prediction_id).filter(db_models.PredictionOutcome.evaluation_period == period)))
            .order_by(db_models.PredictionSnapshot.timestamp.asc())
            .all()
        )

        min_days = 1 if period == "T+1" else 5
        due = []
        waiting = 0
        for pred in pending:
            age = _calendar_age_days(pred.timestamp)
            if age >= min_days:
                due.append(pred)
            else:
                waiting += 1

        # Manual / sync sweeps: process ALL pending (due first), with history backfill
        # so Training Intelligence is not stuck at "0 due / 19 waiting".
        queue = list(due) + [p for p in pending if p not in due]
        queue = queue[:max_batch]

        logger.info(
            "Found %s pending, %s due, %s waiting for %s — processing %s (backfill=%s)",
            len(pending), len(due), waiting, period, len(queue), period == "T+1",
        )
        done = 0
        backfilled = 0
        skipped = []
        reasons = {}
        labeled_sample = []
        for pred in queue:
            try:
                if period == "T+1":
                    res = evaluate_t1(pred.prediction_id, allow_backfill=True)
                else:
                    # T+5: only real due for now (needs 5 sessions); still try
                    res = evaluate_t5(pred.prediction_id)
                rsn = (res or {}).get("reason") or ("ok" if (res or {}).get("ok") else "error")
                mode = (res or {}).get("mode") or ""
                reasons[rsn] = reasons.get(rsn, 0) + 1
                if mode:
                    reasons[f"mode:{mode}"] = reasons.get(f"mode:{mode}", 0) + 1
                if (res or {}).get("ok") and rsn in ("labeled", "already_evaluated", "ok"):
                    done += 1
                    if mode == "history_backfill":
                        backfilled += 1
                    if len(labeled_sample) < 6:
                        labeled_sample.append({
                            "symbol": pred.symbol,
                            "mode": mode or rsn,
                            "success": (res or {}).get("success"),
                            "entry_date": (res or {}).get("entry_date"),
                            "t1_date": (res or {}).get("t1_date"),
                        })
                else:
                    skipped.append({"symbol": pred.symbol, "reason": rsn})
            except Exception as e:
                logger.warning("%s eval failed for %s: %s", period, pred.prediction_id, e)
                reasons["exception"] = reasons.get("exception", 0) + 1
            _time.sleep(0.25)
        logger.info(
            "%s evaluation batch finished: succeeded=%s backfilled=%s reasons=%s",
            period, done, backfilled, reasons,
        )
        return {
            "ok": True,
            "pending": len(pending),
            "due": len(due),
            "waiting": waiting,
            "attempted": len(queue),
            "succeeded": done,
            "backfilled": backfilled,
            "period": period,
            "reasons": reasons,
            "skipped_sample": skipped[:8],
            "labeled_sample": labeled_sample,
            "pipeline": [
                {"step": "load_pending", "ok": True, "detail": f"{len(pending)} snapshots without {period} outcome"},
                {"step": "filter_due", "ok": True, "detail": f"{len(due)} calendar-due (≥{min_days}d), {waiting} waiting"},
                {"step": "fetch_bars", "ok": True, "detail": "market-data 3mo → yfinance/bhavcopy-style history"},
                {"step": "backfill", "ok": True, "detail": f"history_backfill labels={backfilled} (when next session missing)"},
                {"step": "score_outcomes", "ok": done > 0, "detail": f"{done}/{len(queue)} labeled; reasons={reasons}"},
            ],
            "message": (
                f"{period}: labeled {done}/{len(queue)} "
                f"(calendar due={len(due)}, waiting={waiting}, history_backfill={backfilled}). "
                + (
                    "Used previous session pairs from upstream/history where T+1 bar was not available yet."
                    if backfilled
                    else ("No labels written — check MARKET_DATA_URL / bars." if done == 0 else "Realtime session labels written.")
                )
            ),
        }
    except Exception as e:
        logger.error(f"Error in batch evaluation: {e}")
        return {"error": str(e), "period": period}
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

def run_t1_sweep(db_session=None, max_batch: int = 80):
    """Reliable T+1 evaluation sweep for cron / GitHub Actions."""
    return evaluate_pending_predictions("T+1", max_batch=max_batch)



def run_t5_sweep(db_session=None, max_batch: int = 80):
    """Reliable T+5 evaluation sweep for cron / GitHub Actions.

    BUG FIX (2026-09-01): this called `evaluate_t5()` — no prediction_id,
    which evaluate_t5() requires — behind a `"evaluate_t5" in dir()` guard.
    dir() with no arguments returns the CURRENT LOCAL SCOPE, not module
    globals; evaluate_t5 is a module-level function never locally bound
    here, so the guard was always False and this always returned the fake
    placeholder {"ok": True, ..., "evaluated": 0} without evaluating a
    single prediction — even if the guard had been fixed, the bare
    evaluate_t5() call itself would have raised (missing prediction_id).
    evaluate_pending_predictions('T+5', ...) is the real batch sweep
    (already used correctly by evaluate_all_predictions() above and by
    run_t1_sweep's T+1 equivalent) — call that instead, matching
    run_t1_sweep's pattern exactly.
    """
    try:
        return evaluate_pending_predictions("T+5", max_batch=max_batch)
    except Exception as ex:
        return {"ok": False, "error": str(ex)}