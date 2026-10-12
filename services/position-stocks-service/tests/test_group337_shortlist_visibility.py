"""group337 - position-stocks keeps what became of every shortlisted pick (ENTERED / SKIPPED:<reason> / NOT_TRIED) and serves it, with the
ranking weights and the quality-gate pass count, on GET /shortlist.
Run from services/position-stocks-service:  python3 -m pytest tests/test_group337_shortlist_visibility.py -q"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
import main as m
from orders.entry import InsufficientCapitalSkip
from screening.quality_gate import QualitySignal
import tests.test_group163_entry_precheck as _pre             # module import: its test classes must not be collected here
from tests.test_group163_entry_precheck import _cand, db  # noqa: F401


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(SHORTLIST_ENABLED=True, SHORTLIST_FALLTHROUGH=True, SHORTLIST_MAX=5, BOARD_ENABLED=False, ENTRY_PRECHECK=False,
                     MIN_PREFERRED_SCALP_POSITIONS=0).items():
        monkeypatch.setattr(config, k, v, raising=False)
    m._last_shortlist.update(at=None, rows=[], evaluated=0, passed=0, entered_symbol=None)
    m._capital_starved.clear()                       # a capital skip in one test must not put the symbol on cooldown in the next
    yield
    m._last_shortlist.update(at=None, rows=[], evaluated=0, passed=0, entered_symbol=None)
    m._capital_starved.clear()


def _cycle(db, monkeypatch, symbols, attempt):
    """Run one AUTO cycle over `symbols` (scan order = given order, all pass the quality gate); `attempt(symbol)` -> (position | None, skip reason | None)
    or raises InsufficientCapitalSkip."""
    cands = [_cand(s, 10.0) for s in symbols]
    for i, c in enumerate(cands):
        c.composite_score = 50.0 - i
    _pre.TestInTheCycle()._setup(db, monkeypatch, cands)

    async def _check(symbol, cached=None):
        return QualitySignal(symbol=symbol, fundamental_score=80.0, technical_score=80.0, market_cap_cr=5000.0)
    monkeypatch.setattr(m.quality_gate, "check", _check)
    monkeypatch.setattr(m, "_attempt_entry_with_skip_reason", lambda db_, c, q=None: attempt(c.symbol))
    return asyncio.run(m._run_cycle(db, trigger="AUTO"))


def _outcomes():
    return {r["symbol"]: r["outcome"] for r in m._last_shortlist["rows"]}


def test_entered_pick_is_marked_and_the_rest_are_not_tried(db, monkeypatch):
    _cycle(db, monkeypatch, ["AAA", "BBB", "CCC"], lambda s: (SimpleNamespace(id=1), None))
    assert _outcomes() == {"AAA": "ENTERED", "BBB": "NOT_TRIED", "CCC": "NOT_TRIED"}
    assert m._last_shortlist["entered_symbol"] == "AAA"


def test_symbol_specific_skip_falls_through_and_is_recorded(db, monkeypatch):
    def attempt(sym):
        return (None, "VWAP_BELOW:price under VWAP") if sym == "AAA" else (SimpleNamespace(id=2), None)
    _cycle(db, monkeypatch, ["AAA", "BBB", "CCC"], attempt)
    assert _outcomes() == {"AAA": "SKIPPED:VWAP_BELOW", "BBB": "ENTERED", "CCC": "NOT_TRIED"}
    assert m._last_shortlist["entered_symbol"] == "BBB"


def test_capital_skip_is_recorded_and_next_pick_tried(db, monkeypatch):
    def attempt(sym):
        if sym == "AAA":
            raise InsufficientCapitalSkip("AAA: INSUFFICIENT_CAPITAL")
        return SimpleNamespace(id=3), None
    _cycle(db, monkeypatch, ["AAA", "BBB"], attempt)
    assert _outcomes() == {"AAA": "SKIPPED:INSUFFICIENT_CAPITAL", "BBB": "ENTERED"}


def test_global_skip_ends_the_cycle(db, monkeypatch):
    _cycle(db, monkeypatch, ["AAA", "BBB"], lambda s: (None, "SERVICE_NOT_ARMED"))
    assert _outcomes() == {"AAA": "SKIPPED:SERVICE_NOT_ARMED", "BBB": "NOT_TRIED"}
    assert m._last_shortlist["entered_symbol"] is None


def test_route_exposes_picks_weights_and_counts(db, monkeypatch):
    _cycle(db, monkeypatch, ["AAA", "BBB"], lambda s: (SimpleNamespace(id=4), None))
    body = m.shortlist_route()
    assert body["enabled"] is True and body["max"] == 5
    assert body["entered_symbol"] == "AAA" and body["passed_quality_gate"] == 2 and body["evaluated"] == 2
    assert set(body["weights"]) == {"scan", "board", "quality", "zone"}
    assert [p["symbol"] for p in body["picks"]] == ["AAA", "BBB"] and body["picks"][0]["outcome"] == "ENTERED"
