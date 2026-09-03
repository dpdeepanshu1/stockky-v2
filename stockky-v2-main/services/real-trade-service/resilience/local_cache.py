"""
resilience/local_cache.py — Short-Term Trading Upgrade (2026-09-02)

Last-known-good cache backed by trade_resilience_cache (same shared DB,
no extra infra). Two layers:

1. Generic snapshot store (save_snapshot / load_snapshot) — used by
   watchlist_engine/sources.py to save the last successful Tier 1 payload
   so Tier 2 is only reached when BOTH the live call AND the local cache
   miss. Also used for market-data-service responses.

2. Open-position snapshot (snapshot_open_positions / reconcile_on_startup) —
   called every cycle in cycle_runner.py. If a mid-cycle DB write fails and
   the service restarts, this snapshot lets reconcile_on_startup detect and
   flag the drift instead of silently trusting either the (potentially
   inconsistent) DB state or the Dhan broker state. Never auto-corrects;
   always logs a RECONCILE_MISMATCH audit event for human review.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

logger = logging.getLogger("real-trade-local-cache")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Generic key-value snapshot ───────────────────────────────────────────────

def save_snapshot(db, key: str, payload: dict) -> None:
    """Upsert a JSON payload under `key` in trade_resilience_cache."""
    try:
        import models
        row = db.query(models.ResilienceCache).filter_by(key=key).first()
        if row is None:
            row = models.ResilienceCache(key=key, payload_json="{}")
            db.add(row)
        row.payload_json = json.dumps(payload, default=str)
        row.updated_at = _now()
        db.commit()
    except Exception as exc:
        logger.warning("local_cache.save_snapshot[%s]: %s", key, exc)


def load_snapshot(db, key: str) -> dict | None:
    """Return the stored payload for `key`, or None if not found / unreadable."""
    try:
        import models
        row = db.query(models.ResilienceCache).filter_by(key=key).first()
        if row is None:
            return None
        return json.loads(row.payload_json)
    except Exception as exc:
        logger.warning("local_cache.load_snapshot[%s]: %s", key, exc)
        return None


# ── Open-position snapshot (per-cycle safety net) ────────────────────────────

def snapshot_open_positions(db, positions: list) -> None:
    """
    Snapshot the current open positions at the top of every cycle_runner pass,
    BEFORE any exit evaluation or order placement. If the DB write later in
    the cycle fails and the process restarts, reconcile_on_startup can detect
    the discrepancy and log a RECONCILE_MISMATCH for human review.

    Call this right after `open_positions(db, mode)` in cycle_runner.py.
    One snapshot per mode — key includes the mode so DEMO and REAL don't
    overwrite each other.
    """
    if not positions:
        return
    mode = positions[0].mode if positions else "unknown"
    payload = {
        "as_of": _now().isoformat(),
        "positions": [
            {
                "id":               p.id,
                "symbol":           p.symbol,
                "qty_open":         p.qty_open,
                "avg_entry_price":  p.avg_entry_price,
                "current_stop":     p.current_stop,
                "current_target":   p.current_target,
                "status":           p.status,
            }
            for p in positions
        ],
    }
    save_snapshot(db, f"open_positions_last_known_good_{mode}", payload)


def reconcile_on_startup(db) -> None:
    """
    Run once when real-trade-service boots (called from main.py's startup hook).
    Compares the live DB state (all OPEN positions) against the last snapshot
    for each mode. Any symbol set mismatch is logged as RECONCILE_MISMATCH
    in the audit trail — a human should then confirm Dhan's broker state
    is the source of truth.

    Never auto-corrects: the purpose is visibility, not silent fixup.
    """
    try:
        import models
        from audit.logger import log_action

        for mode in ("DEMO", "REAL"):
            snap = load_snapshot(db, f"open_positions_last_known_good_{mode}")
            if not snap:
                continue

            live_symbols = {
                p.symbol
                for p in db.query(models.TradePosition)
                .filter_by(mode=mode, status="OPEN")
                .all()
            }
            snap_symbols = {row["symbol"] for row in snap.get("positions", [])}

            if live_symbols != snap_symbols:
                only_live = live_symbols - snap_symbols
                only_snap = snap_symbols - live_symbols
                detail = (
                    f"mode={mode} "
                    f"snap_as_of={snap.get('as_of')} "
                    f"in_live_not_snap={sorted(only_live)} "
                    f"in_snap_not_live={sorted(only_snap)}"
                )
                log_action(db, actor="system", action="RECONCILE_MISMATCH", mode=mode, detail=detail)
                logger.warning(
                    "real-trade startup reconcile: MISMATCH detected — %s", detail
                )
            else:
                logger.info(
                    "real-trade startup reconcile: %s OK (%d open positions match snapshot)",
                    mode, len(live_symbols),
                )
    except Exception as exc:
        logger.warning("reconcile_on_startup failed (non-fatal): %s", exc)
