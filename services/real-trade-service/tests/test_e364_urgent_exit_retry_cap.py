"""E364: a stop_hit / emergency_gap_down exit is retried after at most EXIT_RETRY_URGENT_MAX_COOLDOWN_SECONDS (120 s), not the 900 s cap of the other exits."""
from __future__ import annotations

import re
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "exit_engine" / "exit.py").read_text(encoding="utf-8")


def _helper():
    i = SRC.index("_URGENT_EXIT_REASONS = ")
    j = SRC.index("def _should_skip_exit_this_cycle")
    cfg = types.SimpleNamespace(EXIT_RETRY_MAX_COOLDOWN_SECONDS=900, EXIT_RETRY_BASE_COOLDOWN_SECONDS=60, EXIT_RETRY_URGENT_MAX_COOLDOWN_SECONDS=120)
    ns = {"config": cfg, "Optional": __import__("typing").Optional, "models": types.SimpleNamespace(TradePosition=object)}
    exec(SRC[i:j], ns)
    return ns["_retry_cooldown_seconds"]


def _pos(n):
    return types.SimpleNamespace(consecutive_exit_failures=n)


def test_normal_exit_keeps_the_900_cap():
    f = _helper()
    assert [f(_pos(n), "target_hit") for n in (1, 2, 3, 5, 8)] == [60, 120, 240, 900, 900]
    assert f(_pos(6), None) == 900


def test_urgent_exits_are_capped_at_120():
    f = _helper()
    for r in ("stop_hit", "emergency_gap_down"):
        assert [f(_pos(n), r) for n in (1, 2, 3, 5, 8)] == [60, 120, 120, 120, 120]


def test_both_call_sites_pass_the_reason_and_the_env_is_wired():
    assert "_should_skip_exit_this_cycle(position, reason)" in SRC
    cfg = (ROOT / "config.py").read_text(encoding="utf-8")
    assert re.search(r'EXIT_RETRY_URGENT_MAX_COOLDOWN_SECONDS.*"120"', cfg)
