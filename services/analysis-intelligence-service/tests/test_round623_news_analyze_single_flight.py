"""Round 623 (E648): /analyze runs one multi-source fetch per symbol at a time; a simultaneous second caller gets a copy of its answer.

Run from services/analysis-intelligence-service:  python3 -m pytest tests/test_round623_news_analyze_single_flight.py -q
"""
from __future__ import annotations

import os
import sys
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from news import main as nm  # noqa: E402


def _install(monkeypatch, delay=0.3, boom_first=False):
    calls = []
    gate = threading.Lock()

    def impl(symbol, company_name=None, force=False):
        with gate:
            calls.append(symbol)
            n = len(calls)
        time.sleep(delay)
        if boom_first and n == 1:
            raise RuntimeError("leader failed")
        return {"symbol": nm._base_symbol(symbol), "headline_count": 0, "headlines": [], "n": n}
    monkeypatch.setattr(nm, "_analyze_impl", impl)
    return calls


def _run(fn, args_list):
    out = [None] * len(args_list)
    err = [None] * len(args_list)

    def w(i, a):
        try:
            out[i] = fn(*a)
        except Exception as e:  # noqa: BLE001
            err[i] = e
    ts = [threading.Thread(target=w, args=(i, a)) for i, a in enumerate(args_list)]
    for t in ts:
        t.start()
        time.sleep(0.05)
    for t in ts:
        t.join(10)
    return out, err


def test_two_simultaneous_calls_run_one_fetch(monkeypatch):
    calls = _install(monkeypatch)
    out, err = _run(nm.analyze, [("CONFIPET",), ("CONFIPET",)])
    assert err == [None, None]
    assert len(calls) == 1
    assert out[0] == out[1] and out[0]["n"] == 1


def test_suffix_and_case_share_one_fetch(monkeypatch):
    calls = _install(monkeypatch)
    out, err = _run(nm.analyze, [("confipet.ns",), ("CONFIPET",), ("CONFIPET.BO",)])
    assert err == [None] * 3 and len(calls) == 1


def test_follower_gets_its_own_copy(monkeypatch):
    _install(monkeypatch)
    out, _ = _run(nm.analyze, [("FIBERWEB",), ("FIBERWEB",)])
    out[1]["headlines"].append("x")
    assert out[0]["headlines"] == []


def test_different_symbols_run_in_parallel(monkeypatch):
    calls = _install(monkeypatch)
    out, err = _run(nm.analyze, [("AAA",), ("BBB",)])
    assert err == [None, None] and len(calls) == 2


def test_sequential_calls_each_fetch_fresh(monkeypatch):
    calls = _install(monkeypatch, delay=0.0)
    nm.analyze("CONFIPET")
    nm.analyze("CONFIPET")
    assert len(calls) == 2 and nm._analyze_inflight == {}


def test_leader_failure_follower_runs_its_own(monkeypatch):
    calls = _install(monkeypatch, boom_first=True)
    out, err = _run(nm.analyze, [("CONFIPET",), ("CONFIPET",)])
    assert isinstance(err[0], RuntimeError)
    assert err[1] is None and out[1]["n"] == 2 and len(calls) == 2
    assert nm._analyze_inflight == {}


def test_joined_call_logs_once(monkeypatch, caplog):
    _install(monkeypatch)
    with caplog.at_level("INFO"):
        _run(nm.analyze, [("CONFIPET",), ("CONFIPET",)])
    assert sum("joined the one already running" in r.getMessage() for r in caplog.records) == 1
