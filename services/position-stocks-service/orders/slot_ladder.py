"""orders/slot_ladder.py - E273 round 35: the slot size ladder is live. A setup's size grows or shrinks by what its own closed trades say (plan: "Slot promotion").

setup_engine._size starts every cell (setup x time slot) at quarter (09:20) or half size and has always honoured a `promoted` set, but nothing ever supplied it, so a
cell could never earn more and never lose any. This module reads the setup stamps of the closed trades still held (setup_stats.summarize) and returns, per cell
"<setup id>|<slot>" with at least setup_stats.MIN_TRADES closed trades (and as many with a readable R):
  promoted  - verdict POSITIVE (the 90 % lower bound of its average R is above zero): the engine sizes it at full size (before the soft penalties);
  demoted   - average R below zero and not proven positive: the size is cut by SLOT_LADDER_DEMOTE_FACTOR (default 0.5, floor SIZE_FLOOR) - "if it loses, it shrinks".
A cell with fewer trades is neither (NOT_ENOUGH_DATA) and keeps its starting size. Penalties still apply on top. Switch SLOT_LADDER_ENABLED (off = every cell keeps its starting
size). Rows are read from the database at most every SLOT_LADDER_CACHE_S seconds; any error = no ladder (starting sizes). Pure helpers never raise.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Dict, Iterable, Optional

import config
from orders import setup_stats

logger = logging.getLogger("position-stocks-slot-ladder")

_cache: Dict[str, Any] = {"at": 0.0, "val": None}


def enabled() -> bool:
    return bool(getattr(config, "SLOT_LADDER_ENABLED", True))


def from_rows(rows: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """{"promoted": {cell,...}, "demoted": {cell,...}, "cells": setup_stats.summarize(rows)}. Never raises."""
    try:
        cells = setup_stats.summarize(rows)
        promoted, demoted = set(), set()
        for key, c in cells.items():
            if key.endswith("|ALL") or key.startswith("LEGACY_SHORTLIST|"):
                continue
            if c.get("verdict") == "POSITIVE":
                promoted.add(key)
            elif c.get("verdict") == "NOT_PROVEN" and (c.get("avg_r") is not None and c["avg_r"] < 0):
                demoted.add(key)
        return {"promoted": promoted, "demoted": demoted, "cells": cells}
    except Exception:  # noqa: BLE001
        return {"promoted": set(), "demoted": set(), "cells": {}}


def rows_from_positions(rows: Iterable[Any]) -> list:
    """The dict rows setup_stats reads, from settled ScalpPosition rows (E275 round 37: with the best prices, the side and the entry time as epoch seconds)."""
    out = []
    for r in rows:
        try:
            from tz_utils import as_aware
            ts = as_aware(r.opened_at).timestamp() if r.opened_at else None
        except Exception:  # noqa: BLE001
            ts = None
        out.append({"flow_json": r.flow_json, "realized_pnl": r.realized_pnl, "entry_price": r.entry_price, "stop_price": r.stop_price, "quantity": r.quantity,
                    "opened_at": str(r.opened_at or ""), "opened_ts": ts, "max_price_seen": getattr(r, "max_price_seen", None),
                    "min_price_seen": getattr(r, "min_price_seen", None), "side": getattr(r, "side", None)})
    return out


def _load(db) -> Dict[str, Any]:
    from models import ScalpPosition
    rows = (db.query(ScalpPosition).filter(ScalpPosition.realized_pnl.isnot(None))
            .order_by(ScalpPosition.opened_at.desc()).limit(1000).all())
    return from_rows(rows_from_positions(rows))


_cells_cache: Dict[str, Any] = {"at": 0.0, "val": None}


def cells(db) -> Dict[str, Any]:
    """E275 round 37: setup_stats cells for the exit side, whatever SLOT_LADDER_ENABLED says (that switch is about entry size). Same cache time; {} on any error."""
    try:
        ttl = max(0.0, float(getattr(config, "SLOT_LADDER_CACHE_S", 300.0)))
        now = time.time()
        if _cells_cache["val"] is not None and now - float(_cells_cache["at"]) < ttl:
            return _cells_cache["val"]
        val = _load(db).get("cells") or {}
        _cells_cache["at"], _cells_cache["val"] = now, val
        return val
    except Exception:  # noqa: BLE001
        return {}


def get(db) -> Dict[str, Any]:
    """The current ladder ({"promoted", "demoted", "cells"}); empty sets when the switch is off or the read fails. Cached for SLOT_LADDER_CACHE_S seconds."""
    empty: Dict[str, Any] = {"promoted": set(), "demoted": set(), "cells": {}}
    if not enabled():
        return empty
    try:
        ttl = max(0.0, float(getattr(config, "SLOT_LADDER_CACHE_S", 300.0)))
        now = time.time()
        if _cache["val"] is not None and now - float(_cache["at"]) < ttl:
            return _cache["val"]
        val = _load(db)
        _cache["at"], _cache["val"] = now, val
        return val
    except Exception:  # noqa: BLE001
        logger.debug("slot ladder: read failed (starting sizes apply)", exc_info=True)
        return empty


def reset() -> None:
    _cache["at"], _cache["val"] = 0.0, None
    _cells_cache["at"], _cells_cache["val"] = 0.0, None


def demote_factor(setup_id: Optional[str], slot: Optional[str], ladder: Optional[Dict[str, Any]], side: str = "LONG") -> float:
    """1.0, or SLOT_LADDER_DEMOTE_FACTOR (0.05..1.0, default 0.5) when the cell "<setup id>|<slot>" is demoted. Never raises."""
    try:
        if not setup_id or f"{setup_stats.cell_id(setup_id, side)}|{slot or 'later'}" not in ((ladder or {}).get("demoted") or ()):   # E274 round 44: a short S2 / S3 reads its own cell
            return 1.0
        return max(0.05, min(1.0, float(getattr(config, "SLOT_LADDER_DEMOTE_FACTOR", 0.5))))
    except (TypeError, ValueError):
        return 1.0
