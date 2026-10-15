"""group378: in-process catch-up for the post-close structure refresh (pure helpers + main.py wiring as source text).
Run from services/api-gateway:  python3 -m pytest tests/test_group378_structure_catchup.py -v"""
from __future__ import annotations

import ast
import os
import sys

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

import structure_refresh as sr  # noqa: E402


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in list(os.environ):
        if k.startswith("STRUCTURE_REFRESH_"):
            monkeypatch.delenv(k, raising=False)
    sr.reset_for_tests()
    yield
    sr.reset_for_tests()


@pytest.mark.parametrize("phase,want", [("closed", True), ("holiday", True), ("CLOSED", True),
                                        ("open", False), ("preopen", False), ("post", False), ("", False), (None, False)])
def test_only_runs_when_the_market_is_closed(phase, want):
    assert sr.catchup_wanted(phase, "idle") is want


def test_never_starts_on_top_of_a_running_job():
    assert sr.catchup_wanted("closed", "running") is False
    for st in ("idle", "done", "error", "stalled", "stopped", None):
        assert sr.catchup_wanted("closed", st) is True


def test_kill_switches(monkeypatch):
    monkeypatch.setenv("STRUCTURE_REFRESH_CATCHUP", "0")
    assert sr.catchup_enabled() is False and sr.catchup_wanted("closed", "idle") is False
    monkeypatch.delenv("STRUCTURE_REFRESH_CATCHUP")
    monkeypatch.setenv("STRUCTURE_REFRESH_ENABLED", "0")        # the whole job off also turns the catch-up off
    assert sr.catchup_enabled() is False


def test_defaults_and_floors(monkeypatch):
    assert sr.catchup_interval_s() == 1800.0 and sr.catchup_initial_delay_s() == 180.0
    monkeypatch.setenv("STRUCTURE_REFRESH_CATCHUP_INTERVAL_S", "5")
    assert sr.catchup_interval_s() == 60.0                       # never hammer the technical service
    monkeypatch.setenv("STRUCTURE_REFRESH_CATCHUP_INTERVAL_S", "junk")
    assert sr.catchup_interval_s() == 1800.0


def test_main_wiring():
    src = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    names = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert {"_structure_refresh_catchup_loop", "_start_structure_refresh_catchup"} <= names
    loop = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_structure_refresh_catchup_loop")
    body = ast.get_source_segment(src, loop)
    assert "catchup_wanted(_market_session_phase_ist()" in body and "_start_structure_refresh" in body and "CancelledError" in body
    hook = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_start_structure_refresh_catchup")
    assert any(getattr(d, "id", "") == "_on_startup" for d in hook.decorator_list)
    stop = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_stop_structure_refresh_catchup")
    assert any(getattr(d, "id", "") == "_on_shutdown" for d in stop.decorator_list)
    assert '"catchup": _catchup' in src                            # visible on /data-feed/structure-refresh/status
