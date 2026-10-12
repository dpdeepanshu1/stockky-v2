"""entry_engine/shortlist.py - group332: the "best 3 to 5" step inside Gate 6, plus the Market Board nudge that feeds it.

Gate 6 ranks every risk-approved candidate of a cycle by composite score and enters the top ENTRY_MAX_NEW_PER_CYCLE. Missing
before: (1) the Market Board's verdict on the symbol (conviction, rank, zone, chase guard) never reached that ranking, and
(2) only the 20 OLDEST queued candidates reached it at all. This module adds
  * board_nudge(row)  - small CAPPED +/- composite change from the board row (ranking nudge only, never a gate),
  * build(approved)   - the shortlist: the best ENTRY_SHORTLIST_MAX (default 5) approved candidates that clear the composite
                        floor (UC bypasses it), best first; entries are drawn from its first ENTRY_MAX_NEW_PER_CYCLE.
Pure functions, no I/O. ENTRY_SHORTLIST_ENABLED=false / ENTRY_BOARD_NUDGE_ENABLED=false restore the old behaviour."""
from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

import config


def board_nudge(row: Optional[dict]) -> float:
    """Composite points for one board row, capped to +/-ENTRY_BOARD_MAX_NUDGE.
    + bull side, conviction >= 70 and rank <= 5 (the board's own PICK rule) -> up to the cap, scaled by conviction
    + bull side, conviction >= 60                                           -> half of that
    - bear side (the entry engine is long-only)                              -> -cap
    - extended past its order-block zone (chase guard)                       -> -cap/2
    0 no row / no data."""
    try:
        if not row or not getattr(config, "ENTRY_BOARD_NUDGE_ENABLED", True):
            return 0.0
        cap = float(config.ENTRY_BOARD_MAX_NUDGE)
        if cap <= 0:
            return 0.0
        side = row.get("side")
        if side == "bear":
            return -cap
        pts = 0.0
        conv = row.get("conviction")
        conv = float(conv) if conv is not None else None
        rank = row.get("rank")
        if side == "bull" and conv is not None:
            scale = max(0.0, min(1.0, (conv - 50.0) / 50.0))
            if conv >= 70 and rank is not None and int(rank) <= 5:
                pts = cap * scale
            elif conv >= 60:
                pts = cap * 0.5 * scale
        if row.get("extended"):
            pts -= cap / 2.0
        return max(-cap, min(cap, pts))
    except Exception:  # noqa: BLE001 - a nudge must never break a cycle
        return 0.0


def build(approved: List[dict]) -> Dict[str, List[dict]]:
    """approved: staged entries ({"composite_score", "is_upper_circuit", ...}). Returns
    {"shortlist": best ENTRY_SHORTLIST_MAX that clear the floor (UC bypasses), best first,
     "selected": its first ENTRY_MAX_NEW_PER_CYCLE, "skipped": every other approved entry}."""
    ranked = sorted(approved, key=lambda e: e["composite_score"], reverse=True)   # stable: ties keep processing order
    eligible = [e for e in ranked
                if e["composite_score"] >= config.ENTRY_MIN_COMPOSITE_SCORE or e.get("is_upper_circuit")]
    if getattr(config, "ENTRY_SHORTLIST_ENABLED", True):
        shortlist = eligible[: max(1, int(config.ENTRY_SHORTLIST_MAX))]
    else:
        shortlist = eligible[: config.ENTRY_MAX_NEW_PER_CYCLE]
    selected = shortlist[: config.ENTRY_MAX_NEW_PER_CYCLE]
    ids = {id(e) for e in selected}
    return {"shortlist": shortlist, "selected": selected, "skipped": [e for e in ranked if id(e) not in ids]}


# -- group337: what the last cycle's shortlist looked like and what became of each pick (GET /shortlist/{mode}, Board tab panel) ----------
# Display only: nothing in the entry path reads this back. In-memory, per mode, replaced by every cycle that ranked at least one approved entry.
_state_lock = threading.Lock()
_state: Dict[str, dict] = {}


def reset() -> None:
    with _state_lock:
        _state.clear()


def _pick_row(n: int, e: dict, status: str) -> dict:
    c = e.get("cand")
    return {
        "pick": n, "symbol": getattr(c, "symbol", None), "status": status, "outcome": None,
        "composite_score": round(float(e.get("composite_score") or 0.0), 1),
        "raw_composite_score": round(float(e.get("raw_composite_score") or 0.0), 1),
        "board_bonus": round(float(e.get("board_bonus") or 0.0), 1),
        "news_bonus": round(float(e.get("intraday_news_bonus") or 0.0), 1),
        "sector_bonus": round(float(e.get("us_sector_bonus") or 0.0), 1),
        "conviction": getattr(c, "conviction_score", None),
        "decision_label": getattr(c, "decision_label", None),
        "rr": round(float(e.get("rr") or 0.0), 2),
        "entry": e.get("entry_price"), "stop": e.get("stop_price"), "target": e.get("target_price"),
        "is_upper_circuit": bool(e.get("is_upper_circuit")),
    }


def record(mode: str, built: Dict[str, List[dict]], approved_count: int, selected: List[dict], reserve: List[dict]) -> None:
    """Remember this cycle's shortlist. status: SELECTED (placed first), RESERVE (placed only if a selected pick fails). Never raises."""
    try:
        sel_ids = {id(x) for x in selected}
        res_ids = {id(x) for x in reserve}
        rows, n = [], 0
        for e in built.get("shortlist", []):
            n += 1
            rows.append(_pick_row(n, e, "SELECTED" if id(e) in sel_ids else ("RESERVE" if id(e) in res_ids else "SHORTLISTED")))
        with _state_lock:
            _state[mode] = {"at": time.time(), "approved": int(approved_count), "rows": rows}
    except Exception:  # noqa: BLE001 - visibility must never break a cycle
        pass


def mark(mode: str, symbol: Optional[str], outcome: str) -> None:
    """Set what became of one pick: ENTERED, PLACEMENT_FAILED:<why>, NOT_NEEDED. Never raises."""
    try:
        with _state_lock:
            for r in (_state.get(mode) or {}).get("rows", []):
                if r["symbol"] == symbol:
                    r["outcome"] = outcome
    except Exception:  # noqa: BLE001
        pass


def finalize(mode: str) -> None:
    """Picks still without an outcome when the cycle ends were never placed: reserve picks that were not needed, or selected picks the cycle never reached."""
    try:
        with _state_lock:
            for r in (_state.get(mode) or {}).get("rows", []):
                if r["outcome"] is None:
                    r["outcome"] = "NOT_NEEDED" if r["status"] == "RESERVE" else "NOT_PLACED"
    except Exception:  # noqa: BLE001
        pass


def snapshot(mode: str) -> dict:
    """JSON-safe copy for GET /shortlist/{mode}, with the settings that shaped it."""
    with _state_lock:
        st = _state.get(mode) or {}
        rows = [dict(r) for r in st.get("rows", [])]
        at, approved = st.get("at"), st.get("approved", 0)
    return {
        "mode": mode, "enabled": bool(getattr(config, "ENTRY_SHORTLIST_ENABLED", True)),
        "max": int(config.ENTRY_SHORTLIST_MAX), "max_new_per_cycle": int(config.ENTRY_MAX_NEW_PER_CYCLE),
        "min_composite": float(config.ENTRY_MIN_COMPOSITE_SCORE), "fallthrough": bool(getattr(config, "ENTRY_SHORTLIST_FALLTHROUGH", True)),
        "board_nudge_enabled": bool(getattr(config, "ENTRY_BOARD_NUDGE_ENABLED", True)),
        "as_of": at, "approved_count": approved, "picks": rows,
    }
