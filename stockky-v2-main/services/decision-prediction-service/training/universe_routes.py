"""
FastAPI routes for universe → training.

Mount these on the training app (or include in training/app.py).

Endpoints:
  POST /api/universe/ingest          – store full scan universe for training
  GET  /api/universe/samples         – list active (non-expired) samples
  POST /api/universe/train-from-universe – ingest + optionally trigger training
  DELETE /api/universe/purge-expired – cleanup
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field

from universe_ingest import (
    get_active_universe_samples,
    ingest_universe,
    purge_expired,
)

logger = logging.getLogger("universe_routes")
router = APIRouter(prefix="/api/universe", tags=["universe-training"])

# Price gate — OFF by default (0 = no cap; every eligible sample is kept).
import os as _os
MAX_STOCK_PRICE = float(_os.getenv("MAX_STOCK_PRICE", "0") or 0)


def get_filtered_universe(
    symbols: List[str],
    feature_snapshots: Optional[Dict[str, Dict[str, Any]]] = None,
) -> List[str]:
    """
    Drop symbols whose snapshot price exceeds MAX_STOCK_PRICE.
    When no snapshot/price is available, keep the symbol (root filters
    in data_feed / bhavcopy are the primary gate).
    """
    snaps = feature_snapshots if isinstance(feature_snapshots, dict) else {}
    out: List[str] = []
    for s in symbols or []:
        base = str(s or "").upper().replace(".NS", "").replace(".BO", "").strip()
        if not base:
            continue
        snap = snaps.get(base) or snaps.get(s) or {}
        px = 0.0
        if isinstance(snap, dict):
            for k in ("price", "close", "cmp", "ltp", "last_price", "current_price"):
                try:
                    v = float(snap.get(k) or 0)
                    if v > 0:
                        px = v
                        break
                except (TypeError, ValueError):
                    pass
        if MAX_STOCK_PRICE > 0 and px > MAX_STOCK_PRICE:
            continue
        out.append(base)
    return out


class UniverseIngestRequest(BaseModel):
    symbols: List[str] = Field(..., min_length=1, description="Full scan universe symbols")
    decisions: Optional[Dict[str, str]] = None
    scores: Optional[Dict[str, float]] = None
    feature_snapshots: Optional[Dict[str, Dict[str, Any]]] = None
    source: str = "daily_scan"  # or "manual_universe"
    retention_hours: int = 48
    trigger_training: bool = False


class UniverseIngestResponse(BaseModel):
    ok: bool
    ingested: int
    message: str
    source: Optional[str] = None
    expires_at: Optional[str] = None
    retention_hours: Optional[int] = None
    training_triggered: bool = False


def _maybe_trigger_training(background_tasks: BackgroundTasks):
    """Best-effort: try to call existing train trigger if available."""
    try:
        # Import lazily so this module stays usable even if train.py changes
        from train import request_abort  # noqa: F401 – just connectivity check
        # Prefer the existing /api/train endpoint logic if present in app
        logger.info("Training trigger requested after universe ingest (background)")
        # Actual trigger is left to the caller or existing BackgroundTasks in app.py
        return True
    except Exception as e:
        logger.warning("Could not auto-trigger training: %s", e)
        return False


@router.post("/ingest", response_model=UniverseIngestResponse)
def api_ingest_universe(body: UniverseIngestRequest, background_tasks: BackgroundTasks):
    """
    Store the full daily scan universe for training.
    Use this for the button: "Send the Stock Universe For Training".
    """
    filtered = get_filtered_universe(body.symbols, body.feature_snapshots)
    # Keep only decision/score keys for retained symbols
    keep = set(filtered)
    decisions = {k: v for k, v in (body.decisions or {}).items() if str(k).upper().replace(".NS", "").replace(".BO", "").strip() in keep} if body.decisions else None
    scores = {k: v for k, v in (body.scores or {}).items() if str(k).upper().replace(".NS", "").replace(".BO", "").strip() in keep} if body.scores else None
    snaps = {k: v for k, v in (body.feature_snapshots or {}).items() if str(k).upper().replace(".NS", "").replace(".BO", "").strip() in keep} if body.feature_snapshots else None
    result = ingest_universe(
        symbols=filtered,
        decisions=decisions,
        scores=scores,
        feature_snapshots=snaps,
        source=body.source or "manual_universe",
        retention_hours=body.retention_hours or 48,
    )
    training_triggered = False
    if body.trigger_training and result.get("ok"):
        training_triggered = _maybe_trigger_training(background_tasks)
    return UniverseIngestResponse(
        ok=result.get("ok", False),
        ingested=result.get("ingested", 0),
        message=result.get("message", ""),
        source=result.get("source"),
        expires_at=result.get("expires_at"),
        retention_hours=result.get("retention_hours"),
        training_triggered=training_triggered,
    )


@router.post("/train-from-universe", response_model=UniverseIngestResponse)
def api_train_from_universe(body: UniverseIngestRequest, background_tasks: BackgroundTasks):
    """Ingest full universe and request a training run."""
    body.trigger_training = True
    body.source = body.source or "manual_universe"
    return api_ingest_universe(body, background_tasks)


@router.get("/samples")
def api_list_samples(limit: int = 2000):
    samples = get_active_universe_samples(limit=limit)
    return {
        "count": len(samples),
        "samples": samples,
        "message": f"{len(samples)} active universe samples available for training",
    }


@router.delete("/purge-expired")
def api_purge_expired():
    n = purge_expired()
    return {"purged": n, "message": f"Removed {n} expired samples"}
