"""
resilience/pause_state.py  (group 183b / item 7)

Pauses that are held in memory with a monotonic deadline (group 160 dead-symbol pause in market_feed/feed.py, group 172
no-daily-history pause in candidate_engine/candidates.py) were lost on every restart, so each redeploy asked once again about
every symbol that was already known to have no price / no history. This module stores them in trade_resilience_cache (same
table and save_snapshot/load_snapshot pair the ATR cache uses) as WALL-clock deadlines and converts back to monotonic ones on
load.

Rules: never raises; nothing is written until a module has called its load_*_from_db() at startup (so tests and one-off
imports never touch the DB); writes are debounced (one write per key per PAUSE_STATE_FLUSH_DELAY_S, default 5 s) and done in a
short-lived daemon thread with its own session; PAUSE_STATE_PERSIST=0 turns the whole thing off.
"""
import logging
import os
import threading
import time
from typing import Callable, Dict

logger = logging.getLogger("real-trade-pause-state")

MAX_ITEMS = 2000
_LOCK = threading.Lock()
_PENDING: Dict[str, threading.Timer] = {}


def enabled() -> bool:
    return ((os.getenv("PAUSE_STATE_PERSIST") or "").strip() or "1") not in ("0", "false", "False")


def _delay_s() -> float:
    try:
        v = float((os.getenv("PAUSE_STATE_FLUSH_DELAY_S") or "").strip() or "5")
    except ValueError:
        return 5.0
    return v if v == v and v >= 0 else 5.0


def mono_to_wall(mono_until: float) -> float:
    return time.time() + (float(mono_until) - time.monotonic())


def wall_to_mono(wall_until: float) -> float:
    return time.monotonic() + (float(wall_until) - time.time())


def save_items(db, key: str, items: dict) -> None:
    """Write {symbol: {...}} under `key` (newest MAX_ITEMS kept by deadline). Never raises."""
    try:
        from resilience.local_cache import save_snapshot
        if len(items) > MAX_ITEMS:
            keep = sorted(items.items(), key=lambda kv: float((kv[1] or {}).get("u", 0)), reverse=True)[:MAX_ITEMS]
            items = dict(keep)
        save_snapshot(db, key, {"v": 1, "items": items})
    except Exception as e:  # noqa: BLE001
        logger.warning("pause_state.save_items[%s] failed (non-fatal): %s", key, e)


def load_items(db, key: str) -> dict:
    """Stored items whose wall-clock deadline ("u") is still in the future. Never raises."""
    out: dict = {}
    try:
        from resilience.local_cache import load_snapshot
        snap = load_snapshot(db, key)
        raw = snap.get("items") if isinstance(snap, dict) else None
        if not isinstance(raw, dict):
            return out
        now = time.time()
        for sym, ent in raw.items():
            try:
                if isinstance(sym, str) and sym and isinstance(ent, dict) and float(ent.get("u")) > now:
                    out[sym] = ent
            except (TypeError, ValueError):
                continue
    except Exception as e:  # noqa: BLE001
        logger.warning("pause_state.load_items[%s] failed (non-fatal): %s", key, e)
    return out


def _run(key: str, builder: Callable[[], dict]) -> None:
    with _LOCK:
        _PENDING.pop(key, None)
    try:
        from db import get_session_factory
        db = get_session_factory()()
        try:
            save_items(db, key, builder())
        finally:
            db.close()
    except Exception as e:  # noqa: BLE001
        logger.warning("pause_state flush[%s] failed (non-fatal): %s", key, e)


def schedule_flush(key: str, builder: Callable[[], dict]) -> None:
    """Debounced write of builder() under `key`. Never raises."""
    try:
        if not enabled():
            return
        with _LOCK:
            if key in _PENDING:
                return
            t = threading.Timer(_delay_s(), _run, args=(key, builder))
            t.daemon = True
            _PENDING[key] = t
        t.start()
    except Exception as e:  # noqa: BLE001
        logger.debug("pause_state.schedule_flush[%s] failed: %s", key, e)
