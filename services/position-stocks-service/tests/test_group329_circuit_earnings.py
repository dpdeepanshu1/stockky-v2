"""tests/test_group329_circuit_earnings.py - orders/circuit_gate.py and the results-day check in screening/quality_gate.py (group 329).

Circuit band: no scalp BUY next to the upper circuit / close to the lower circuit; unknown or nonsense limits never block.
Results day: a stock whose results are dated today fails the quality gate; an unknown date passes; everything fails open.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import circuit_gate, depth_gate
from screening import quality_gate as qg
from tz_utils import ist_today_str


@pytest.fixture(autouse=True)
def defaults(monkeypatch):
    monkeypatch.setattr(config, "CIRCUIT_GATE_ENABLED", True)
    monkeypatch.setattr(config, "CIRCUIT_UPPER_BUFFER_PCT", 1.0)
    monkeypatch.setattr(config, "CIRCUIT_LOWER_BUFFER_PCT", 2.0)
    monkeypatch.setattr(config, "EARNINGS_DAY_BLOCK", True)
    qg._earnings_seen.clear()
    yield
    qg._earnings_seen.clear()


def run(coro):
    return asyncio.run(coro)


class TestDecide:
    def test_middle_of_band_passes(self):
        assert circuit_gate.decide(100.0, 110.0, 90.0) is None

    def test_upper_and_lower_buffers(self):
        assert circuit_gate.decide(110.0, 110.0, 90.0).startswith("CIRCUIT_UPPER")
        assert circuit_gate.decide(108.95, 110.0, 90.0).startswith("CIRCUIT_UPPER")
        assert circuit_gate.decide(108.8, 110.0, 90.0) is None
        assert circuit_gate.decide(91.5, 110.0, 90.0).startswith("CIRCUIT_LOWER")
        assert circuit_gate.decide(92.0, 110.0, 90.0) is None

    @pytest.mark.parametrize("price,upper,lower", [
        (100.0, None, None), (100.0, 0, 0), (100.0, "x", None), (100.0, float("nan"), float("nan")),
        (None, 110.0, 90.0), (0, 110.0, 90.0), (100.0, 90.0, 110.0), (130.0, 110.0, 90.0), (70.0, 110.0, 90.0),
    ])
    def test_unknown_or_nonsense_never_blocks(self, price, upper, lower):
        assert circuit_gate.decide(price, upper, lower) is None

    def test_zero_buffer_turns_a_side_off(self, monkeypatch):
        monkeypatch.setattr(config, "CIRCUIT_UPPER_BUFFER_PCT", 0.0)
        assert circuit_gate.decide(110.0, 110.0, 90.0) is None
        assert circuit_gate.decide(90.5, 110.0, 90.0) is not None


class TestRejectReason:
    def test_reads_the_cached_quote_row(self, monkeypatch):
        monkeypatch.setattr(depth_gate, "_fresh_quote", lambda s: {"upper_circuit": 110.0, "lower_circuit": 90.0})
        assert circuit_gate.reject_reason("ABC", 110.0).startswith("CIRCUIT_UPPER")
        assert circuit_gate.reject_reason("ABC", 100.0) is None

    def test_no_quote_or_a_quote_without_limits_allows(self, monkeypatch):
        monkeypatch.setattr(depth_gate, "_fresh_quote", lambda s: None)
        assert circuit_gate.reject_reason("ABC", 110.0) is None
        monkeypatch.setattr(depth_gate, "_fresh_quote", lambda s: {"spread_pct": 0.1})
        assert circuit_gate.reject_reason("ABC", 110.0) is None

    def test_an_exception_while_reading_the_quote_allows(self, monkeypatch):
        def boom(s):
            raise RuntimeError("market-data down")
        monkeypatch.setattr(depth_gate, "_fresh_quote", boom)
        assert circuit_gate.reject_reason("ABC", 110.0) is None

    def test_disabled_gate_never_reads_the_quote(self, monkeypatch):
        monkeypatch.setattr(config, "CIRCUIT_GATE_ENABLED", False)
        monkeypatch.setattr(depth_gate, "_fresh_quote", lambda s: (_ for _ in ()).throw(AssertionError("must not be read")))
        assert circuit_gate.reject_reason("ABC", 110.0) is None

    def test_both_buffers_zero_never_reads_the_quote(self, monkeypatch):
        monkeypatch.setattr(config, "CIRCUIT_UPPER_BUFFER_PCT", 0.0)
        monkeypatch.setattr(config, "CIRCUIT_LOWER_BUFFER_PCT", 0.0)
        monkeypatch.setattr(depth_gate, "_fresh_quote", lambda s: (_ for _ in ()).throw(AssertionError("must not be read")))
        assert circuit_gate.reject_reason("ABC", 110.0) is None


class TestEarningsDate:
    def test_reads_upcoming_and_recent(self):
        assert qg._earnings_date_from({"upcoming": [{"type": "earnings_date", "date": "2026-10-20"}]}) == "2026-10-20"
        assert qg._earnings_date_from({"recent": [{"type": "dividend", "date": "2026-10-12"},
                                                  {"type": "earnings_date", "date": "2026-10-12"}]}) == "2026-10-12"

    @pytest.mark.parametrize("body", [None, {}, [], "x", {"upcoming": None}, {"upcoming": [{"type": "earnings_date", "date": "soon"}]},
                                      {"upcoming": [{"type": "earnings_date"}]}])
    def test_junk_is_none(self, body):
        assert qg._earnings_date_from(body) is None

    def test_passes_rejects_only_a_date_that_is_today(self):
        today = ist_today_str()
        ok, why = qg.QualitySignal(symbol="A", earnings_date=today).passes()
        assert not ok and why.startswith("EARNINGS_TODAY")
        assert qg.QualitySignal(symbol="A", earnings_date="2099-01-01").passes()[0]
        assert qg.QualitySignal(symbol="A", earnings_date="2000-01-01").passes()[0]
        assert qg.QualitySignal(symbol="A", earnings_date=None).passes()[0]

    def test_switch_off(self, monkeypatch):
        monkeypatch.setattr(config, "EARNINGS_DAY_BLOCK", False)
        assert qg.QualitySignal(symbol="A", earnings_date=ist_today_str()).passes()[0]


class _Resp:
    def __init__(self, code, body):
        self.status_code, self._b = code, body

    def json(self):
        return self._b


class _Client:
    def __init__(self, resp):
        self.resp = resp

    async def get(self, url, timeout=None, **kw):
        if isinstance(self.resp, Exception):
            raise self.resp
        return self.resp


class TestEventFetchStashesTheDate:
    def test_return_shape_is_unchanged_and_date_is_stashed(self):
        body = {"has_positive_catalyst": True, "recent_event_score": 3.5, "bulk_deals": [1],
                "recent": [{"type": "earnings_date", "date": ist_today_str()}]}
        out = run(qg._fetch_event_signal(_Client(_Resp(200, body)), "ABC"))
        assert out == (True, 3.5, True)
        assert qg._earnings_seen["ABC"] == ist_today_str()

    def test_failure_stashes_nothing(self):
        assert run(qg._fetch_event_signal(_Client(TimeoutError()), "ABC")) == (None, None, False)
        assert run(qg._fetch_event_signal(_Client(_Resp(503, {})), "ABC")) == (None, None, False)
        assert "ABC" not in qg._earnings_seen

    def test_check_puts_the_date_on_the_signal_and_fails_the_gate(self, monkeypatch):
        monkeypatch.setattr(config, "QUALITY_GATE_ENABLED", True)

        async def fund_tech(client, sym):
            return (60.0, 60.0, 5000.0)

        async def event(client, sym):
            qg._earnings_seen[sym] = ist_today_str()
            return (None, None, False)
        monkeypatch.setattr(qg, "_fetch_fund_tech", fund_tech)
        monkeypatch.setattr(qg, "_fetch_event_signal", event)
        sig = run(qg.check("ABC"))
        assert sig.earnings_date == ist_today_str()
        ok, why = sig.passes()
        assert not ok and why.startswith("EARNINGS_TODAY")
        assert "ABC" not in qg._earnings_seen          # consumed once

    def test_a_stale_stashed_date_is_not_reused_by_the_next_check(self, monkeypatch):
        monkeypatch.setattr(config, "QUALITY_GATE_ENABLED", True)
        qg._earnings_seen["ABC"] = ist_today_str()      # left behind by an earlier, failed check

        async def fund_tech(client, sym):
            return (60.0, 60.0, 5000.0)

        async def event(client, sym):
            return (None, None, False)
        monkeypatch.setattr(qg, "_fetch_fund_tech", fund_tech)
        monkeypatch.setattr(qg, "_fetch_event_signal", event)
        assert run(qg.check("ABC")).earnings_date is None
