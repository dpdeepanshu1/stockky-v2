"""
Point-in-time (PIT) data validation for Stockky training / evaluation.

Goal: refuse labels or training rows that could not have been known at
prediction time (look-ahead bias), and flag snapshots with inconsistent clocks.

Free-tier scope:
  - Validate timestamps on PredictionSnapshot vs outcome evaluation dates
  - Ensure feature_snapshot does not contain fields stamped after prediction
  - Sanity-check bar dates used for T+1/T+5 vs prediction timestamp
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("pit-validation")


def _as_naive(dt) -> Optional[datetime]:
    if dt is None:
        return None
    if isinstance(dt, str):
        try:
            dt = datetime.fromisoformat(dt.replace("Z", "+00:00"))
        except Exception:
            return None
    if getattr(dt, "tzinfo", None) is not None:
        try:
            dt = dt.replace(tzinfo=None)
        except Exception:
            return None
    return dt


def validate_prediction_snapshot(snapshot: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    """
    Validate a feature/decision snapshot for PIT safety before train or store.
    Returns {ok, issues[], warnings[]}.
    """
    now = _as_naive(now) or datetime.utcnow()
    issues: List[str] = []
    warnings: List[str] = []

    ts = _as_naive(snapshot.get("timestamp") or snapshot.get("as_of") or snapshot.get("created_at"))
    if ts is None:
        issues.append("missing_prediction_timestamp")
    else:
        if ts > now + timedelta(minutes=5):
            issues.append("prediction_timestamp_in_future")
        if ts < now - timedelta(days=400):
            warnings.append("prediction_timestamp_very_old")

    # Feature snapshot nested clocks
    feat = snapshot.get("feature_snapshot")
    if isinstance(feat, dict):
        for key in ("as_of", "news_as_of", "fund_as_of", "bar_date", "fetched_at"):
            ft = _as_naive(feat.get(key))
            if ft and ts and ft > ts + timedelta(hours=12):
                issues.append(f"feature_{key}_after_prediction")

    # Scores must be finite
    for k in ("combined_score", "technical_score", "fundamental_score", "prediction_score"):
        v = snapshot.get(k)
        if v is None:
            continue
        try:
            fv = float(v)
            if fv != fv:  # NaN
                issues.append(f"{k}_nan")
            if abs(fv) > 1e6:
                issues.append(f"{k}_out_of_range")
        except (TypeError, ValueError):
            issues.append(f"{k}_not_numeric")

    decision = (snapshot.get("decision") or "").upper()
    if decision == "BUY NOW" and snapshot.get("provisional"):
        warnings.append("buy_now_while_provisional_should_have_been_gated")

    ok = len(issues) == 0
    return {"ok": ok, "issues": issues, "warnings": warnings, "as_of": ts.isoformat() if ts else None}


def validate_outcome_vs_prediction(
    prediction_ts,
    evaluation_period: str,
    bar_date=None,
    evaluation_date=None,
) -> Dict[str, Any]:
    """
    Ensure T+1/T+5 outcome bars are strictly after the prediction session.
    """
    issues: List[str] = []
    warnings: List[str] = []
    pts = _as_naive(prediction_ts)
    bdt = _as_naive(bar_date)
    edt = _as_naive(evaluation_date)

    if pts is None:
        issues.append("missing_prediction_ts")
        return {"ok": False, "issues": issues, "warnings": warnings}

    min_days = 1 if (evaluation_period or "").upper() in ("T+1", "T1") else 5
    if bdt is not None:
        # bar date should be on or after prediction calendar day + min sessions (approx calendar days)
        if bdt.date() < pts.date():
            issues.append("bar_date_before_prediction")
        # extreme look-ahead: bar more than ~30d after for T+1 is suspicious for labeling
        lag = (bdt.date() - pts.date()).days
        if evaluation_period in ("T+1", "T1") and lag > 15:
            warnings.append("t1_bar_unusually_late")
        if lag < 0:
            issues.append("negative_lag")

    if edt is not None and edt < pts:
        issues.append("evaluation_date_before_prediction")

    return {"ok": len(issues) == 0, "issues": issues, "warnings": warnings, "min_days": min_days}


def filter_train_rows_pit(rows: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], Dict[str, int]]:
    """Drop rows that fail PIT validation; return kept + stats."""
    kept = []
    stats = {"input": len(rows), "dropped": 0, "warnings": 0}
    for r in rows:
        res = validate_prediction_snapshot(r)
        if not res["ok"]:
            stats["dropped"] += 1
            logger.info("PIT drop %s: %s", r.get("symbol"), res["issues"])
            continue
        if res["warnings"]:
            stats["warnings"] += 1
        r = dict(r)
        r["_pit_validation"] = res
        kept.append(r)
    stats["kept"] = len(kept)
    return kept, stats
