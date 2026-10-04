"""group101 item 27 remainder: a premarket baseline run with no symbols uses the live scan universe once loaded."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main as m
import surprise_premarket as sp


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("SURPRISE_UNIVERSE", raising=False)
    monkeypatch.delenv("SCAN_UNIVERSE", raising=False)
    monkeypatch.setattr(m, "_current_feed_universe", [])


def test_no_live_universe_yet_uses_builtin_list():
    assert m._premarket_default_symbols() == sp.default_universe_from_env()


def test_live_universe_is_used_once_loaded(monkeypatch):
    monkeypatch.setattr(m, "_current_feed_universe", ["TCS", " INFY ", "", "WIPRO"])
    assert m._premarket_default_symbols() == ["TCS", "INFY", "WIPRO"]


def test_env_universe_still_wins_over_live(monkeypatch):
    monkeypatch.setenv("SURPRISE_UNIVERSE", "AAA,BBB")
    monkeypatch.setattr(m, "_current_feed_universe", ["TCS"])
    assert m._premarket_default_symbols() == ["AAA", "BBB"]


def test_scan_universe_env_also_wins(monkeypatch):
    monkeypatch.setenv("SCAN_UNIVERSE", "CCC;DDD")
    monkeypatch.setattr(m, "_current_feed_universe", ["TCS"])
    assert m._premarket_default_symbols() == ["CCC", "DDD"]


def test_route_with_no_symbols_runs_on_the_live_universe(monkeypatch):
    seen = {}
    monkeypatch.setattr(m, "_current_feed_universe", ["TCS", "INFY"])
    monkeypatch.setattr(sp, "precalculate_surprise_baselines", lambda syms, force=False: seen.setdefault("syms", list(syms)) or {})
    monkeypatch.setattr(sp, "get_premarket_progress", lambda: {"is_running": False})
    out = m.surprise_premarket_run(body=None, symbols=None, background=False)
    assert seen["syms"] == ["TCS", "INFY"]


def test_route_explicit_symbols_are_untouched(monkeypatch):
    seen = {}
    monkeypatch.setattr(m, "_current_feed_universe", ["TCS", "INFY"])
    monkeypatch.setattr(sp, "precalculate_surprise_baselines", lambda syms, force=False: seen.setdefault("syms", list(syms)) or {})
    monkeypatch.setattr(sp, "get_premarket_progress", lambda: {"is_running": False})
    m.surprise_premarket_run(body=None, symbols="RELIANCE,HDFCBANK", background=False)
    assert seen["syms"] == ["RELIANCE", "HDFCBANK"]
