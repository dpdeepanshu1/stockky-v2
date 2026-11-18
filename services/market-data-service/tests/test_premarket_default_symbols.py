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
    monkeypatch.setattr(m, "_premarket_starting", False)      # round 630: a fake thread never clears the claim


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


# Round 630 (E656): two background starts in a row start one job; the claim is released when the job ends or cannot start.
def _bg_env(monkeypatch):
    import threading
    started = []

    class FakeThread:
        def __init__(self, target=None, daemon=None, name=None, **kw):
            self.target = target
            started.append(self)

        def start(self):
            pass

    monkeypatch.setattr(threading, "Thread", FakeThread)
    calls = []
    monkeypatch.setattr(sp, "precalculate_surprise_baselines", lambda syms, force=False: calls.append(list(syms)) or {})
    monkeypatch.setattr(sp, "get_premarket_progress", lambda: {"is_running": False})
    return started, calls


def test_second_background_start_is_already_running(monkeypatch):
    started, calls = _bg_env(monkeypatch)
    first = m.surprise_premarket_run(body=None, symbols="A,B", background=True)
    second = m.surprise_premarket_run(body=None, symbols="A,B", background=True)
    assert first["accepted"] is True and len(started) == 1
    assert second["accepted"] is False and second["already_running"] is True and len(started) == 1


def test_claim_released_after_the_job_and_after_a_failure(monkeypatch):
    started, calls = _bg_env(monkeypatch)
    m.surprise_premarket_run(body=None, symbols="A", background=True)
    started[0].target()
    assert calls == [["A"]] and m._premarket_starting is False
    assert m.surprise_premarket_run(body=None, symbols="A", background=True)["accepted"] is True

    def boom(syms, force=False):
        raise RuntimeError("yfinance down")

    monkeypatch.setattr(sp, "precalculate_surprise_baselines", boom)
    started[1].target()                                   # must not raise out of the thread
    assert m._premarket_starting is False


def test_claim_released_when_the_thread_cannot_start(monkeypatch):
    import threading

    class Boom:
        def __init__(self, *a, **kw): pass
        def start(self): raise RuntimeError("can't start new thread")

    monkeypatch.setattr(threading, "Thread", Boom)
    monkeypatch.setattr(sp, "get_premarket_progress", lambda: {"is_running": False})
    with pytest.raises(RuntimeError):
        m.surprise_premarket_run(body=None, symbols="A", background=True)
    assert m._premarket_starting is False
