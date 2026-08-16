"""
Training Scanner: real historical-similarity scoring.

training_score answers a genuinely different question than
prediction_score does: not "what does a trained model think", but "of
the historically similar setups this system has actually seen and later
evaluated, how many turned out well?" — a case-based-reasoning signal.

This is deliberately independent of any trained model:
  1. It works even before training-service's own model pipeline (a
     separate system, see train.py) has ever produced anything.
  2. It can't inherit that model's blind spots, since it's reasoning
     directly from real historical outcomes rather than a learned
     function of them.
  3. It sidesteps an incompatibility that existed in the previous version
     of this file: it tried to load a classifier and call .predict_proba()
     on it, but train.py actually produces a regressor with no such
     method — the two were never going to work together as written.

The previous version's _compute_similarity() and _get_similar_setups()
were hardcoded placeholders (np.random.random(), a fixed fake list of
HAL/BEL/TCS) — this replaces both with a real k-nearest-neighbor search
over PredictionSnapshot rows that have since been evaluated.
"""
import logging
from typing import Optional

import numpy as np

import models as db_models

logger = logging.getLogger("training-service.scanner")

# Numeric columns present on every PredictionSnapshot row (guaranteed by
# ensure_schema()) used as the similarity feature vector. Deliberately
# restricted to these well-typed columns rather than feature_snapshot's
# free-form JSON, since JSON key consistency across callers isn't
# guaranteed the way a fixed schema is.
SIMILARITY_FEATURES = [
    "combined_score", "technical_score", "fundamental_score",
    "rsi", "volume_ratio", "debt_to_equity", "roe", "roce",
]

# Below this many evaluated historical examples, a "nearest neighbor"
# search isn't meaningful — return "insufficient data" honestly instead
# of a number built on 2 or 3 data points.
MIN_HISTORICAL_EVALUATED = 8
DEFAULT_K_NEIGHBORS = 10


class TrainingScanner:
    def __init__(self, db_session_maker, model_store_path: str = None):
        # model_store_path is kept as a constructor parameter for
        # call-site compatibility but isn't used to read files — the
        # model, if any, comes from the DB-backed ModelRegistry below.
        self.db_session_maker = db_session_maker
        self.model, self.model_scaler, self.model_feature_columns = self._load_model()

    @staticmethod
    def _load_model():
        """Loads the current production model, but only if it's actually
        the pick-success classifier train.py's train_pick_success_model
        produces — a stray older artifact (e.g. the legacy OHLCV
        regressor, which used a completely different, incompatible
        feature set) could otherwise still be sitting in the 'production'
        slot and get loaded here, silently producing garbage predictions
        instead of failing loudly."""
        try:
            from models import ModelRegistry
        except ImportError:
            return None, None, None
        try:
            registry = ModelRegistry()
            prod = registry.get_production_model()
            if not prod:
                return None, None, None
            model, scaler, meta = prod
            if meta.get("config", {}).get("model_type") != "prediction_success_classifier":
                logger.info(
                    "Production model %s is not a prediction_success_classifier "
                    "(model_type=%s) — not using it for scoring",
                    meta.get("version"), meta.get("config", {}).get("model_type"),
                )
                return None, None, None
            return model, scaler, meta.get("feature_columns") or []
        except Exception as e:
            logger.warning(f"Could not load production model: {e}")
            return None, None, None

    def _model_probability(self, row) -> Optional[float]:
        """Success probability from the trained classifier, or None if no
        model is loaded, or if any required feature is missing on this
        row. No imputation here deliberately: the model was calibrated
        against training-time median fills, which weren't persisted
        alongside it, so silently substituting a different fallback value
        at inference time could produce a confident-looking but
        meaningless number."""
        if self.model is None or not self.model_feature_columns:
            return None
        try:
            values = [getattr(row, col, None) for col in self.model_feature_columns]
            if any(v is None for v in values):
                return None
            vec = np.array([[float(v) for v in values]], dtype=np.float32)
            scaled = self.model_scaler.transform(vec) if self.model_scaler is not None else vec
            proba = self.model.predict_proba(scaled)[0, 1]
            return round(float(proba) * 100, 1)
        except Exception as e:
            logger.warning(f"Model scoring failed for {row.symbol}: {e}")
            return None

    @staticmethod
    def _feature_vector(row) -> np.ndarray:
        return np.array(
            [
                getattr(row, col) if getattr(row, col) is not None else np.nan
                for col in SIMILARITY_FEATURES
            ],
            dtype=float,
        )

    def score_symbol(self, symbol: str) -> Optional[dict]:
        session = self.db_session_maker()
        try:
            current = (
                session.query(db_models.PredictionSnapshot)
                .filter(db_models.PredictionSnapshot.symbol == symbol.upper())
                .order_by(db_models.PredictionSnapshot.timestamp.desc())
                .first()
            )
            if current is None:
                logger.info("No prediction history at all for %s yet", symbol)
                return None

            # "Evaluated" = overall_success is 1 (success) or 2 (failed).
            # 0 means still pending — not enough time has passed to know.
            historical = (
                session.query(db_models.PredictionSnapshot)
                .filter(
                    db_models.PredictionSnapshot.overall_success.in_([1, 2]),
                    db_models.PredictionSnapshot.id != current.id,
                )
                .all()
            )

            if len(historical) < MIN_HISTORICAL_EVALUATED:
                logger.info(
                    "Only %d evaluated historical snapshots system-wide (need %d) — "
                    "insufficient data for a similarity score yet",
                    len(historical), MIN_HISTORICAL_EVALUATED,
                )
                return None

            current_vec = self._feature_vector(current)
            model_success_probability = self._model_probability(current)
            hist_vecs = np.array([self._feature_vector(r) for r in historical])

            # z-score normalize using the historical population's own
            # mean/std, so features on very different scales (RSI 0-100 vs
            # debt-to-equity ~0-5) don't dominate the distance purely
            # because of units. Missing values are imputed with the
            # population mean for that column (contributes ~0 on that
            # axis rather than invalidating the whole comparison).
            #
            # If a column is missing across the ENTIRE historical
            # population (true right now for rsi/volume_ratio — see
            # decision-engine's known gap), nanmean/nanstd return NaN for
            # that column, which the col_std==0 guard below does NOT
            # catch (NaN != 0), and that NaN then propagates through
            # every distance computed — every neighbor "distance" becomes
            # NaN, and np.argsort on an all-NaN array returns an
            # arbitrary, meaningless order. Not a crash, so it was never
            # loud about it — training_score would return neighbors with
            # no real relationship to the current setup. Guarding NaN the
            # same way as exact-zero fixes this: that axis contributes
            # nothing to distance (mean 0, std 1 after centering-to-mean),
            # exactly like a genuinely constant column would.
            col_mean = np.nanmean(hist_vecs, axis=0)
            col_std = np.nanstd(hist_vecs, axis=0)
            col_mean = np.where(np.isnan(col_mean), 0.0, col_mean)
            col_std = np.where(np.isnan(col_std) | (col_std == 0), 1.0, col_std)

            def _normalize(vec):
                filled = np.where(np.isnan(vec), col_mean, vec)
                return (filled - col_mean) / col_std

            current_norm = _normalize(current_vec)
            hist_norm = np.array([_normalize(v) for v in hist_vecs])

            distances = np.linalg.norm(hist_norm - current_norm, axis=1)
            max_dist = distances.max() if distances.max() > 0 else 1.0
            similarities = 1 - (distances / max_dist)

            k = min(DEFAULT_K_NEIGHBORS, len(historical))
            nearest_idx = np.argsort(distances)[:k]

            neighbor_records = []
            t1_hits = 0
            t5_hits = 0
            overall_hits = 0
            for idx in nearest_idx:
                row = historical[idx]
                is_success = row.overall_success == 1
                overall_hits += int(is_success)
                t1_hits += int(row.t1_success == 1)
                t5_hits += int(row.t5_success == 1)
                neighbor_records.append({
                    "symbol": row.symbol,
                    "date": row.timestamp.date().isoformat() if row.timestamp else None,
                    "similarity_pct": round(float(similarities[idx]) * 100, 1),
                    "outcome": "success" if is_success else "failed",
                    "t1_success": row.t1_success == 1,
                    "t5_success": row.t5_success == 1,
                })

            neighbor_records.sort(key=lambda r: r["similarity_pct"], reverse=True)
            training_score = round(overall_hits / k * 100, 1)

            return {
                "symbol": symbol.upper(),
                "training_score": training_score,
                "t1_success_probability": round(t1_hits / k * 100, 1),
                "t5_success_probability": round(t5_hits / k * 100, 1),
                "model_success_probability": model_success_probability,
                "based_on_n_similar_setups": k,
                "similar_setups": neighbor_records[:5],
                "note": (
                    f"Based on the {k} historically most similar evaluated setups in this "
                    f"system's own prediction history, {training_score:.0f}% turned out successful."
                ),
            }
        finally:
            session.close()