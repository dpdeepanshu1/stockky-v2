"""
E217 - entry_engine.entry.evaluate_mode: after Dhan rejected a BUY as outside the exchange price band the code logged "skipping this candidate for
the rest of today" but nothing enforced it, so the next candidate row for the symbol was sent (and rejected) again. Now the symbol is remembered for
the IST day and WAITs (gate_tag "circuit_rejected_today"); another symbol and the next day are unaffected.

Run from services/real-trade-service:
    python3 -m pytest tests/test_e217_circuit_rejected_symbol_not_retried.py -q
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import entry_engine.entry as en

SRC = (Path(__file__).resolve().parent.parent / "entry_engine" / "entry.py").read_text(encoding="utf-8")


def setup_function(_):
    en._CIRCUIT_REJECTED.clear()


def test_marked_symbol_is_blocked_same_day_only():
    en._mark_circuit_rejected("REAL", "ABC")
    assert en._circuit_rejected_today("REAL", "ABC")
    assert not en._circuit_rejected_today("REAL", "XYZ")
    assert not en._circuit_rejected_today("DEMO", "ABC")
    en._CIRCUIT_REJECTED[("REAL", "ABC")] = "2000-01-01"
    assert not en._circuit_rejected_today("REAL", "ABC")


def test_reject_branch_marks_and_loop_checks():
    i = SRC.index("elif dhan_client.is_circuit_limit_error(str(ex)):")
    assert "_mark_circuit_rejected(mode, cand.symbol)" in SRC[i:i + 2500]
    assert "if _circuit_rejected_today(mode, cand.symbol):" in SRC
    assert SRC.index("Gate 1c (E217)") < SRC.index("# ── Gate 2: price available")
