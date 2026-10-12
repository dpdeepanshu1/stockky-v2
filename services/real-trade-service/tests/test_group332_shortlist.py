"""group332 - best-3-to-5 shortlist inside Gate 6, Market Board nudge, fetch-by-conviction, and the removed weak-market restrictions.
Run from services/real-trade-service:  python3 -m pytest tests/test_group332_shortlist.py -q"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from entry_engine import board_client, entry, shortlist
from tests.test_entry_evaluate_mode import pin, db, make_candidate, quotes, run  # noqa: F401
from tests.test_entry_evaluate_mode_remaining_coverage import fake_regime, no_preview_quotes  # noqa: F401

_CONFIG_SRC = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py"), encoding="utf-8").read()


def E(sym, score, uc=False):
    return {"cand": SimpleNamespace(symbol=sym), "composite_score": score, "is_upper_circuit": uc}


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(ENTRY_BOARD_NUDGE_ENABLED=True, ENTRY_BOARD_MAX_NUDGE=6.0, ENTRY_MIN_COMPOSITE_SCORE=50.0,
                     ENTRY_MAX_NEW_PER_CYCLE=3, ENTRY_SHORTLIST_ENABLED=True, ENTRY_SHORTLIST_MAX=5).items():
        monkeypatch.setattr(config, k, v, raising=False)
    board_client.reset()


class TestNudge:
    def test_values(self):
        assert shortlist.board_nudge({"side": "bull", "rank": 2, "conviction": 90}) == pytest.approx(4.8)
        assert shortlist.board_nudge({"side": "bear"}) == -6.0
        assert shortlist.board_nudge({"side": "bull", "rank": 9, "conviction": 40, "extended": True}) == -3.0
        assert shortlist.board_nudge({"side": "bull", "rank": 8, "conviction": 65}) == pytest.approx(0.9)

    def test_none_and_off_and_garbage(self, monkeypatch):
        assert shortlist.board_nudge(None) == 0.0 and shortlist.board_nudge({}) == 0.0
        assert shortlist.board_nudge({"side": "bull", "rank": "x", "conviction": "y"}) == 0.0
        monkeypatch.setattr(config, "ENTRY_BOARD_NUDGE_ENABLED", False)
        assert shortlist.board_nudge({"side": "bear"}) == 0.0

    def test_capped(self):
        assert abs(shortlist.board_nudge({"side": "bull", "rank": 1, "conviction": 100})) <= 6.0


class TestBuild:
    def test_shortlist_is_best_five_and_entries_best_three(self):
        o = shortlist.build([E(s, v) for s, v in zip("ABCDEFG", (60, 90, 70, 55, 80, 40, 65))])
        assert [e["cand"].symbol for e in o["shortlist"]] == ["B", "E", "C", "G", "A"]
        assert [e["cand"].symbol for e in o["selected"]] == ["B", "E", "C"]
        assert sorted(e["cand"].symbol for e in o["skipped"]) == ["A", "D", "F", "G"]

    def test_floor_and_uc_bypass(self):
        o = shortlist.build([E("LOW", 45), E("UC", 30, True)])
        assert [e["cand"].symbol for e in o["selected"]] == ["UC"]

    def test_shortlist_off_matches_old_top_n(self, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_SHORTLIST_ENABLED", False)
        o = shortlist.build([E(s, v) for s, v in zip("ABCDE", (90, 80, 70, 60, 55))])
        assert len(o["shortlist"]) == 3 and len(o["selected"]) == 3

    def test_empty(self):
        assert shortlist.build([]) == {"shortlist": [], "selected": [], "skipped": []}


class _Cli:
    def __init__(self, resp=None, boom=False, calls=None):
        self.resp, self.boom, self.calls = resp, boom, calls if calls is not None else []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, *a, **k):
        self.calls.append(1)
        if self.boom:
            raise RuntimeError("down")
        return self.resp


class TestBoardClient:
    def test_index_rows_bull_wins(self):
        idx = board_client.index_rows({"bullish": [{"symbol": "A", "conviction": 80}], "bearish": [{"symbol": "A"}, {"symbol": "B"}]})
        assert idx["A"]["side"] == "bull" and idx["B"]["side"] == "bear"

    def test_junk_payload(self):
        assert board_client.index_rows("x") == {} and board_client.index_rows({"bullish": [1, None]}) == {}

    def test_failure_is_no_nudge_and_cached(self, monkeypatch):
        calls = []
        monkeypatch.setattr(board_client.httpx, "AsyncClient", lambda *a, **k: _Cli(boom=True, calls=calls))
        assert asyncio.run(board_client.get_rows()) == {} and asyncio.run(board_client.get_rows()) == {}
        assert len(calls) == 1

    def test_stale_board_ignored(self, monkeypatch):
        resp = SimpleNamespace(status_code=200, json=lambda: {"stale": True, "bullish": [{"symbol": "A", "conviction": 90}]})
        monkeypatch.setattr(board_client.httpx, "AsyncClient", lambda *a, **k: _Cli(resp))
        assert asyncio.run(board_client.get_rows()) == {}

    def test_fresh_board_indexed(self, monkeypatch):
        resp = SimpleNamespace(status_code=200, json=lambda: {"bullish": [{"symbol": "A", "conviction": 90, "rank": 1}]})
        monkeypatch.setattr(board_client.httpx, "AsyncClient", lambda *a, **k: _Cli(resp))
        assert asyncio.run(board_client.get_rows())["A"]["side"] == "bull"


class TestFetchByConviction:
    def test_late_strong_candidate_reaches_the_pass(self, db):
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for i in range(25):
            c = make_candidate(db, symbol=f"WEAK{i}", conviction=55.0)
            c.received_at = now - timedelta(minutes=60 - i)
        late = make_candidate(db, symbol="LATESTAR", conviction=95.0)
        late.received_at = now
        db.commit()
        syms = [c.symbol for c in entry._fetch_candidates(db, "DEMO")]
        assert "LATESTAR" in syms and "WEAK0" in syms and len(syms) <= 25

    def test_off_restores_oldest_twenty(self, db, monkeypatch):
        monkeypatch.setattr(config, "ENTRY_FETCH_BY_CONVICTION", False)
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        for i in range(22):
            c = make_candidate(db, symbol=f"W{i}", conviction=55.0)
            c.received_at = now - timedelta(minutes=60 - i)
        late = make_candidate(db, symbol="LATESTAR", conviction=95.0)
        late.received_at = now
        db.commit()
        got = [c.symbol for c in entry._fetch_candidates(db, "DEMO")]
        assert len(got) == 20 and "LATESTAR" not in got


class TestWeakMarketRestrictionsRemoved:
    @pytest.mark.parametrize("name,val", [("ENTRY_REGIME_GATE_ENABLED", '"false"'), ("VIX_BLOCK_ABOVE", "0.0"),
                                          ("VIX_CARRY_BLOCK_ABOVE", "0.0"), ("VIX_CARRY_SPIKE_PCT", "0.0"),
                                          ("CARRY_MIN_NIFTY_CHANGE_PCT", "0.0")])
    def test_defaults_in_source(self, name, val):
        m = re.search(rf"^{name}\s*=.*$", _CONFIG_SRC, re.M)
        assert m and val in m.group(0), m and m.group(0)

    def test_weak_regime_no_longer_blocks(self, db, monkeypatch, caplog):
        monkeypatch.setattr(config, "ENTRY_REGIME_GATE_ENABLED", False)
        fake_regime(monkeypatch, ok=False, score=10, threshold=38)
        make_candidate(db, mode="REAL", decision_label="BUY NOW")
        quotes(monkeypatch, {})
        no_preview_quotes(monkeypatch)
        with caplog.at_level(logging.INFO, logger="real-trade-entry"):
            run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert "BUYs blocked" not in caplog.text

    def test_gate_can_be_switched_back_on(self, db, monkeypatch, caplog):
        monkeypatch.setattr(config, "ENTRY_REGIME_GATE_ENABLED", True)
        fake_regime(monkeypatch, ok=False, score=10, threshold=38)
        make_candidate(db, mode="REAL", decision_label="BUY NOW")
        quotes(monkeypatch, {})
        no_preview_quotes(monkeypatch)
        with caplog.at_level(logging.INFO, logger="real-trade-entry"):
            run(entry.evaluate_mode(db, "REAL", gate_armed=True))
        assert "REAL regime WEAK" in caplog.text

    def test_weak_nifty_no_carry_block_when_off(self, monkeypatch):
        from entry_engine import carry_context
        monkeypatch.setattr(config, "CARRY_MIN_NIFTY_CHANGE_PCT", 0.0)
        assert carry_context.market_reason(-3.0) is None


class TestGate6Integration:
    def test_cycle_logs_shortlist_and_enters_at_most_max_new(self, db, monkeypatch, caplog):
        from tests.test_entry_evaluate_mode import approve_all, tick
        monkeypatch.setattr(config, "ENTRY_CYCLE_QUALITY_FILTER_ENABLED", True)
        syms = ["AAA", "BBB", "CCC", "DDD", "EEE"]
        for s, cv in zip(syms, (95, 90, 85, 80, 75)):
            make_candidate(db, symbol=s, conviction=float(cv))
        quotes(monkeypatch, {s: tick(100.0, symbol=s) for s in syms})
        no_preview_quotes(monkeypatch)
        approve_all(monkeypatch)
        with caplog.at_level(logging.INFO, logger="real-trade-entry"):
            tally = run(entry.evaluate_mode(db, "DEMO", gate_armed=True))
        assert tally["entered"] <= 3
        assert "shortlist" in caplog.text
