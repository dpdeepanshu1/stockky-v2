"""
pipeline_status.py — pure in-memory, best-effort observability for "what is
one evaluation cycle doing right now, and what did the last several do".

This is NOT part of the trading logic. Nothing in here can affect whether an
order is placed, sized, or rejected — every call into this module is wrapped
in try/except at the call site (see cycle_runner.py, candidate_engine/,
entry_engine/) so a bug or exception in status-tracking can never break a
real cycle. It also deliberately lives in process memory, not the DB: it
resets on deploy/restart, which is fine — it answers "what's happening now
and recently", not "what happened historically" (that's audit-log / orders /
positions, which already persist to the DB).

Why this exists: the dashboard's Pipeline tab previously only showed a
cycle's final counters, after the fact, and only for cycles triggered from
that browser tab. It had no visibility into Auto-Pilot's background ticks
(the common case once REAL is armed and the dashboard is closed), no
indication of which stage a running cycle was in, and no per-symbol detail
during candidate refresh / entry evaluation. This module fixes that without
touching a single line of trading logic.
"""
from __future__ import annotations

import time
from collections import deque
from datetime import datetime, timezone
from threading import Lock
from typing import Any, Optional

_LOCK = Lock()
_HISTORY_MAXLEN = 25

# One entry per mode ("DEMO" / "REAL").
_STATE: dict[str, dict[str, Any]] = {}
_HISTORY: dict[str, deque] = {"DEMO": deque(maxlen=_HISTORY_MAXLEN), "REAL": deque(maxlen=_HISTORY_MAXLEN)}

STAGES = (
    "dynamic_universe",  # dynamic_universe.refresh_dynamic_universe: widen/prune auto-tracked symbols
    "watchlist",         # watchlist.refresh_watchlist + entry.evaluate_watchlist_entries: catalyst detect + band-check trigger
    "candidates",     # refresh_candidates: pulling hot-picks / IPO from api-gateway
    "entry",          # entry_engine.evaluate_mode: pricing + risk-checking each candidate
    "fills",          # check_pending_fills (DEMO simulated fills)
    "expire",         # expire_stale_orders
    "exit",           # exit_engine.evaluate_mode
    "reconcile",      # REAL only: execution.reconcile
)


def _now() -> float:
    return time.monotonic()


def start_cycle(mode: str, trigger: str, warning: Optional[str] = None) -> None:
    """trigger: 'manual' (Run Cycle button / API call) or 'autopilot'.
    warning: optional best-effort note surfaced on the dashboard for this
    cycle (e.g. "started pre-market") — purely informational, never blocks
    or alters anything the cycle does."""
    with _LOCK:
        _STATE[mode] = {
            "trigger": trigger,
            "warning": warning,
            "started_at": _now(),
            "started_at_iso": datetime.now(timezone.utc).isoformat(),
            "stage": "starting",
            "stage_started_at": _now(),
            "stage_timings_ms": {},
            "current_symbol": None,
            "current_source": None,
            "symbols_done": 0,
            "symbols_total": 0,
            "running": True,
        }


def set_stage(mode: str, stage: str) -> None:
    with _LOCK:
        st = _STATE.get(mode)
        if st is None:
            return
        now = _now()
        prev_stage = st.get("stage")
        if prev_stage and prev_stage != "starting":
            elapsed_ms = round((now - st["stage_started_at"]) * 1000, 1)
            st["stage_timings_ms"][prev_stage] = elapsed_ms
        st["stage"] = stage
        st["stage_started_at"] = now
        st["current_symbol"] = None
        st["current_source"] = None
        st["symbols_done"] = 0
        st["symbols_total"] = 0


def set_source(mode: str, source: str) -> None:
    """Which upstream feed candidate-refresh is currently fetching (e.g.
    'hot_picks', 'ipo')."""
    with _LOCK:
        st = _STATE.get(mode)
        if st is not None:
            st["current_source"] = source


def set_symbol_progress(mode: str, symbol: Optional[str], done: int, total: int) -> None:
    """Which symbol entry/exit evaluation is currently on, out of how many
    this stage will look at this cycle."""
    with _LOCK:
        st = _STATE.get(mode)
        if st is not None:
            st["current_symbol"] = symbol
            st["symbols_done"] = done
            st["symbols_total"] = total


def end_cycle(mode: str, result: dict, error: Optional[str] = None) -> None:
    with _LOCK:
        st = _STATE.pop(mode, None)
        if st is None:
            return
        now = _now()
        # Close out whatever stage was still open.
        prev_stage = st.get("stage")
        if prev_stage and prev_stage != "starting":
            st["stage_timings_ms"].setdefault(
                prev_stage, round((now - st["stage_started_at"]) * 1000, 1)
            )
        total_ms = round((now - st["started_at"]) * 1000, 1)
        entry = result.get("entry") or {} if result else {}
        exit_ = result.get("exit") or {} if result else {}
        # Cap to 20 rows — one evaluate_mode() call already bounds itself to
        # 20 candidates (see entry_engine/entry.py), so this is a belt-and-
        # braces limit, not a truncation of real data.
        entry_details = (entry.get("entry_details") or [])[:20]
        record = {
            "trigger": st["trigger"],
            "warning": st.get("warning"),
            "started_at": st["started_at_iso"],
            "ended_at": datetime.now(timezone.utc).isoformat(),
            "duration_ms": total_ms,
            "stage_timings_ms": st["stage_timings_ms"],
            "new_candidates": (result or {}).get("new_candidates"),
            "entered": entry.get("entered"),
            "waited": entry.get("waited"),
            "rejected": entry.get("rejected"),
            "entry_details": entry_details,
            "fills": (result or {}).get("fills"),
            "expired_orders": (result or {}).get("expired_orders"),
            "full_exits": exit_.get("full_exits"),
            "partial_exits": exit_.get("partial_exits"),
            "auto_disarmed": (result or {}).get("auto_disarmed"),
            "error": error,
        }
        _HISTORY[mode].appendleft(record)


def get_status(mode: str) -> dict:
    """Live snapshot for the dashboard to poll. Safe to call at any time —
    returns a 'not currently running' shape when there's no active cycle."""
    with _LOCK:
        st = _STATE.get(mode)
        history = list(_HISTORY.get(mode, []))
        if st is None:
            return {
                "mode": mode,
                "running": False,
                "last_cycle": history[0] if history else None,
                "history": history,
            }
        now = _now()
        stage_elapsed_ms = round((now - st["stage_started_at"]) * 1000, 1)
        total_elapsed_ms = round((now - st["started_at"]) * 1000, 1)
        return {
            "mode": mode,
            "running": True,
            "trigger": st["trigger"],
            "warning": st.get("warning"),
            "started_at": st["started_at_iso"],
            "stage": st["stage"],
            "stages": STAGES,
            "stage_elapsed_ms": stage_elapsed_ms,
            "total_elapsed_ms": total_elapsed_ms,
            "current_symbol": st["current_symbol"],
            "current_source": st["current_source"],
            "symbols_done": st["symbols_done"],
            "symbols_total": st["symbols_total"],
            "stage_timings_ms": st["stage_timings_ms"],
            "last_cycle": history[0] if history else None,
            "history": history,
        }
