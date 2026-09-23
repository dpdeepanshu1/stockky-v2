"""pipeline_status.py — in-memory "what is the current/last cycle doing
right now" snapshot, for the frontend's Pipeline tab.

ADDED (session48 — "no live process shows and no stock name shows"): this
service had NO live-cycle visibility infrastructure at all, unlike
real-trade-service (which has had its own pipeline_status.py since
2026-08-27). The Pipeline tab here could only ever show a *completed*
result — either a one-off POST /cycle/run response rendered once after a
manual click, or /status's last_cycle_run_at timestamp — with zero way to
see a cycle actually in progress, or which stock(s) it's looking at while
it runs. Most ticks here finish in single-digit milliseconds (nothing to
watch even if it were live), but a tick that reaches the Quality Gate stage
makes real outbound HTTP calls to analysis-intelligence-service
(screening/quality_gate.py, config.QUALITY_GATE_TIMEOUT_S per call) and can
run for real seconds — exactly the case where "what is it doing right now"
actually matters and was previously invisible.

Single-process, module-level state is enough here, same assumption
real-trade-service's version makes: _run_cycle() only ever runs inside the
one background _trading_loop() task or synchronously inside POST
/cycle/run — never concurrently with itself (both paths already serialize
through _cycle_lock in main.py) — so there's no multi-writer race to guard
against.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

_state: dict[str, Any] = {
    "running": False,
    "trigger": None,           # "AUTO" | "MANUAL"
    "started_at": None,
    "stage": None,              # current stage name, e.g. "scan", "quality_gate"
    "stage_label": None,        # human label, e.g. "Scan (all 4 windows)"
    "stage_started_at": None,
    "candidates": [],           # top candidates the current/last scan surfaced
    "last_cycle": None,         # full summary dict from the most recently completed cycle
}


def start(trigger: str) -> None:
    now = datetime.now(timezone.utc).isoformat()
    _state["running"] = True
    _state["trigger"] = trigger
    _state["started_at"] = now
    _state["stage"] = "starting"
    _state["stage_label"] = "Starting…"
    _state["stage_started_at"] = now
    _state["candidates"] = []


def set_stage(name: str, label: str, candidates: Optional[list] = None) -> None:
    """Called from main.py's _stage() helper every time a stage completes,
    so the live view always reflects the most recently finished stage while
    the cycle is still running. `candidates` — when provided (only the scan
    stage passes this) — replaces the surfaced-candidates list so stock
    names show up live the moment scanning finishes, not just once the
    whole cycle is done."""
    _state["stage"] = name
    _state["stage_label"] = label
    _state["stage_started_at"] = datetime.now(timezone.utc).isoformat()
    if candidates is not None:
        _state["candidates"] = candidates


def finish(summary: dict) -> None:
    _state["running"] = False
    _state["stage"] = None
    _state["stage_label"] = None
    _state["last_cycle"] = summary


def snapshot() -> dict:
    return dict(_state)
