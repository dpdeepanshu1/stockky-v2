"""tests/test_group330_gates.py - India VIX, negative news and the 09:08 pre-open snapshot for the scalper (group 330)."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from screening import preopen, quality_gate as qg, vix_gate
from tz_utils import IST


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    for n in ("VIX_GATE_ENABLED", "NEWS_NEGATIVE_BLOCK_ENABLED", "PREOPEN_SNAPSHOT_ENABLED"):
        monkeypatch.setattr(config, n, True)
    preopen.reset()
    vix_gate.reset()
    yield


def run(c):
    return asyncio.run(c)


class TestVix:
    def test_parse_row(self):
        assert vix_gate.parse_row({"price": 15.2, "day_change_pct": 3.0}) == {"level": 15.2, "change_pct": 3.0}
        assert round(vix_gate.parse_row({"price": 22.0, "previous_close": 20.0})["change_pct"], 6) == 10.0
        for bad in (None, {}, {"price": 0}, {"price": 500}, {"price": "x"}, {"price": float("nan")}, "s"):
            assert vix_gate.parse_row(bad) is None

    def test_block_reason(self):
        assert vix_gate.block_reason({"level": 24.0, "change_pct": 0}).startswith("VIX_HIGH")
        assert vix_gate.block_reason({"level": 23.9, "change_pct": 0}) is None
        assert vix_gate.block_reason(None) is None

    def test_size_mult(self):
        assert vix_gate.size_mult({"level": 15.0, "change_pct": 1.0}) == 1.0
        assert vix_gate.size_mult({"level": 18.0, "change_pct": 0.0}) == 0.7
        assert vix_gate.size_mult({"level": 14.0, "change_pct": 10.0}) == 0.7
        assert vix_gate.size_mult(None) == 1.0        # nothing cached -> normal size

    def test_size_mult_clamped(self, monkeypatch):
        monkeypatch.setattr(config, "VIX_CAUTION_SIZE_MULT", 9.0)
        assert vix_gate.size_mult({"level": 30.0, "change_pct": 0}) == 1.0
        monkeypatch.setattr(config, "VIX_CAUTION_SIZE_MULT", -1.0)
        assert vix_gate.size_mult({"level": 30.0, "change_pct": 0}) == 0.1

    def test_refresh_caches_then_cached_reads_without_network(self, monkeypatch):
        calls = []

        async def f():
            calls.append(1)
            return {"level": 19.0, "change_pct": 0.0}
        monkeypatch.setattr(vix_gate, "_fetch", f)
        assert run(vix_gate.refresh())["level"] == 19.0 and run(vix_gate.refresh())["level"] == 19.0
        assert len(calls) == 1
        assert vix_gate.cached()["level"] == 19.0 and vix_gate.size_mult() == 0.7

    def test_refresh_failure_is_unknown(self, monkeypatch):
        async def boom():
            raise TimeoutError("down")
        monkeypatch.setattr(vix_gate, "_fetch", boom)
        assert run(vix_gate.refresh()) is None and run(vix_gate.refresh()) is None
        assert vix_gate.cached() is None and vix_gate.size_mult() == 1.0

    def test_switch_off(self, monkeypatch):
        monkeypatch.setattr(config, "VIX_GATE_ENABLED", False)
        assert run(vix_gate.refresh()) is None and vix_gate.block_reason({"level": 90.0, "change_pct": 0}) is None
        assert vix_gate.size_mult({"level": 90.0, "change_pct": 0}) == 1.0

    def test_market_gate_blocks_on_high_vix(self, monkeypatch):
        from screening import trade_gates

        async def f():
            return {"level": 26.0, "change_pct": 0.0}
        monkeypatch.setattr(vix_gate, "_fetch", f)
        monkeypatch.setattr(config, "MARKET_GATE_ENABLED", True)

        async def nifty():
            return 0.5
        monkeypatch.setattr(trade_gates, "_fetch_nifty_change_pct", nifty)
        trade_gates._market_cache.update({"pct": None, "ts": 0.0, "prev_pct": None})
        assert run(trade_gates.market_gate_reject()).startswith("VIX_HIGH")

    def test_market_gate_passes_when_vix_unknown(self, monkeypatch):
        from screening import trade_gates

        async def boom():
            raise TimeoutError("x")
        monkeypatch.setattr(vix_gate, "_fetch", boom)
        monkeypatch.setattr(config, "MARKET_GATE_ENABLED", True)

        async def nifty():
            return 0.5
        monkeypatch.setattr(trade_gates, "_fetch_nifty_change_pct", nifty)
        trade_gates._market_cache.update({"pct": None, "ts": 0.0, "prev_pct": None})
        assert run(trade_gates.market_gate_reject()) is None


class TestNegativeNews:
    def test_score_and_hard_negatives(self):
        f = qg._negative_news_from
        assert f({"event_score": 30}).startswith("NEWS_NEGATIVE")
        assert f({"event_score": 35}) is not None and f({"event_score": 36}) is None
        assert "regulatory_action" in f({"event_score": 50, "event_score_breakdown": [{"type": "regulatory_action", "decayed_impact": -20.0}]})
        assert f({"event_score": 50, "event_score_breakdown": [{"type": "earnings_miss", "decayed_impact": -16.0}]}) is not None

    def test_faded_events_do_not_block(self):
        body = {"event_score": 50, "event_score_breakdown": [{"type": "regulatory_action", "decayed_impact": -3.5},
                                                              {"type": "analyst_downgrade", "decayed_impact": -10.0}]}
        assert qg._negative_news_from(body) is None

    def test_garbage_is_none(self, monkeypatch):
        for b in (None, {}, [], "x", {"event_score": None}, {"event_score": "bad"}, {"event_score_breakdown": [None, 3]}):
            assert qg._negative_news_from(b) is None
        monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_SCORE", 0.0)
        assert qg._negative_news_from({"event_score": 1}) is None

    def test_passes_rejects_and_switch(self, monkeypatch):
        sig = qg.QualitySignal(symbol="A", negative_news="NEWS_NEGATIVE:event score 20")
        ok, why = sig.passes()
        assert ok is False and why.startswith("NEWS_NEGATIVE")
        monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_ENABLED", False)
        assert sig.passes()[0] is True
        assert qg.QualitySignal(symbol="A").passes()[0] is True

    def test_check_carries_the_reason_from_the_event_body(self, monkeypatch):
        class R:
            status_code = 200

            def __init__(self, body):
                self._b = body

            def json(self):
                return self._b

        class C:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

            async def get(self, url, timeout=None):
                if "/event" in url or "events" in url:
                    return R({"event_score": 22, "recent_event_score": -0.5})
                raise TimeoutError("down")
        monkeypatch.setattr(qg.httpx, "AsyncClient", lambda *a, **k: C())
        monkeypatch.setattr(config, "QUALITY_GATE_ENABLED", True)
        sig = run(qg.check("TESTCO"))
        assert sig.negative_news and sig.negative_news.startswith("NEWS_NEGATIVE") and sig.passes()[0] is False
        assert "TESTCO" not in qg._news_seen


MON_0910 = datetime(2026, 10, 12, 9, 10, tzinfo=IST)
MON_0905 = datetime(2026, 10, 12, 9, 5, tzinfo=IST)
MON_0916 = datetime(2026, 10, 12, 9, 16, tzinfo=IST)
SAT_0910 = datetime(2026, 10, 10, 9, 10, tzinfo=IST)
MON_0930 = datetime(2026, 10, 12, 9, 30, tzinfo=IST)
MON_1030 = datetime(2026, 10, 12, 10, 30, tzinfo=IST)


def row(sym, price, prev, age=10):
    return {"symbol": sym, "price": price, "previous_close": prev, "age_s": age}


class TestPreopen:
    def test_gap_pct(self):
        assert round(preopen.gap_pct(106, 100), 6) == 6.0
        for a, b in ((0, 100), (100, 0), (None, 1), ("x", 1), (200, 100)):
            assert preopen.gap_pct(a, b) is None

    def test_due_window_once_per_day_and_switch(self, monkeypatch):
        assert preopen.due(MON_0910) is True
        assert preopen.due(MON_0905) is False and preopen.due(MON_0916) is False and preopen.due(SAT_0910) is False
        preopen.record([row("A", 106, 100)], now=MON_0910)
        assert preopen.due(MON_0910) is False
        preopen.reset()
        monkeypatch.setattr(config, "PREOPEN_SNAPSHOT_ENABLED", False)
        assert preopen.due(MON_0910) is False

    def test_record_keeps_only_fresh_rows(self):
        n = preopen.record([row("A", 106, 100), row("B", 106, 100, age=900), {"symbol": "C", "price": 106, "previous_close": 100},
                            row("D", 0, 100), row("E", 106, 100, age=-1), "junk", None], now=MON_0910)
        assert n == 1 and preopen.status()["symbols_with_gap"] == 1

    def test_gap_reasons_inside_window_only(self):
        preopen.record([row("UP", 106.5, 100), row("DN", 96.5, 100), row("OK", 101, 100)], now=MON_0910)
        assert preopen.gap_reason("UP", MON_0930).startswith("PREOPEN_GAP_UP")
        assert preopen.gap_reason("DN", MON_0930).startswith("PREOPEN_GAP_DOWN")
        assert preopen.gap_reason("OK", MON_0930) is None and preopen.gap_reason("NOPE", MON_0930) is None
        assert preopen.gap_reason("UP", MON_1030) is None                              # after PREOPEN_GAP_GATE_UNTIL_IST (10:00)
        assert preopen.gap_reason("UP", datetime(2026, 10, 13, 9, 30, tzinfo=IST)) is None   # next day

    def test_thresholds_can_be_disabled(self, monkeypatch):
        preopen.record([row("UP", 106.5, 100), row("DN", 96.5, 100)], now=MON_0910)
        monkeypatch.setattr(config, "PREOPEN_GAP_UP_BLOCK_PCT", 0.0)
        monkeypatch.setattr(config, "PREOPEN_GAP_DOWN_BLOCK_PCT", 0.0)
        assert preopen.gap_reason("UP", MON_0930) is None and preopen.gap_reason("DN", MON_0930) is None

    def test_run_records_warms_and_never_raises(self, monkeypatch):
        async def rows(syms):
            return [row(s, 106.5, 100) for s in syms]
        monkeypatch.setattr(preopen, "_fetch_rows", rows)

        async def vx():
            return None
        monkeypatch.setattr(vix_gate, "refresh", vx)
        from screening import prev_day, trade_gates

        async def mg():
            return None
        monkeypatch.setattr(trade_gates, "market_gate_reject", mg)
        seen = []
        monkeypatch.setattr(prev_day, "get", lambda s: seen.append(s))
        st = run(preopen.run(["ABC", "XYZ"]))
        assert st["symbols_with_gap"] == 2 and st["warmed"] == 2 and seen == ["ABC", "XYZ"]

    def test_run_survives_fetch_failure_and_marks_the_day(self, monkeypatch):
        async def boom(syms):
            raise RuntimeError("md down")
        monkeypatch.setattr(preopen, "_fetch_rows", boom)
        st = run(preopen.run(["A"]))
        assert st["symbols_with_gap"] == 0 and st["day"] is not None


class TestEntryWiring:
    def test_entry_imports_the_new_gates(self):
        from orders import entry
        assert entry.preopen is preopen and entry.vix_gate is vix_gate
