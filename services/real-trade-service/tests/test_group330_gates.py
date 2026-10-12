"""tests/test_group330_gates.py - India VIX, VWAP, pre-open snapshot, negative news and carry context (group 330)."""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from entry_engine import carry_context, event_risk, preopen, vix_gate, vwap_gate
from tz_utils import IST


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    for n in ("VIX_GATE_ENABLED", "NEWS_NEGATIVE_BLOCK_ENABLED", "CARRY_CONTEXT_GATE_ENABLED", "PREOPEN_SNAPSHOT_ENABLED"):
        monkeypatch.setattr(config, n, True)
    for m in (vix_gate, event_risk, preopen, carry_context):
        m.reset()
    yield
    for m in (vix_gate, event_risk, preopen, carry_context):
        m.reset()


class TestVix:
    def test_parse_row(self):
        assert vix_gate.parse_row({"price": 15.2, "day_change_pct": 3.0}) == {"level": 15.2, "change_pct": 3.0}
        r = vix_gate.parse_row({"price": 22.0, "previous_close": 20.0})
        assert round(r["change_pct"], 6) == 10.0
        for bad in (None, {}, {"price": 0}, {"price": 500}, {"price": "x"}, {"price": float("nan")}, "str"):
            assert vix_gate.parse_row(bad) is None
        assert vix_gate.parse_row({"price": 15.0, "day_change_pct": 500})["change_pct"] is None

    def test_entry_block(self):
        assert vix_gate.entry_block_reason({"level": 24.0, "change_pct": 0}).startswith("VIX_HIGH")
        assert vix_gate.entry_block_reason({"level": 23.9, "change_pct": 0}) is None
        assert vix_gate.entry_block_reason(None) is None

    def test_block_off_when_zero_or_switched_off(self, monkeypatch):
        monkeypatch.setattr(config, "VIX_BLOCK_ABOVE", 0.0)
        assert vix_gate.entry_block_reason({"level": 40.0, "change_pct": 0}) is None
        monkeypatch.setattr(config, "VIX_BLOCK_ABOVE", 24.0)
        monkeypatch.setattr(config, "VIX_GATE_ENABLED", False)
        assert vix_gate.entry_block_reason({"level": 40.0, "change_pct": 0}) is None

    def test_size_mult(self):
        assert vix_gate.size_mult({"level": 15.0, "change_pct": 2.0}) == 1.0
        assert vix_gate.size_mult({"level": 18.0, "change_pct": 0.0}) == 0.7
        assert vix_gate.size_mult({"level": 14.0, "change_pct": 10.0}) == 0.7
        assert vix_gate.size_mult({"level": 14.0, "change_pct": None}) == 1.0
        assert vix_gate.size_mult(None) == 1.0

    def test_size_mult_is_clamped(self, monkeypatch):
        monkeypatch.setattr(config, "VIX_CAUTION_SIZE_MULT", 5.0)
        assert vix_gate.size_mult({"level": 30.0, "change_pct": 0}) == 1.0
        monkeypatch.setattr(config, "VIX_CAUTION_SIZE_MULT", -3.0)
        assert vix_gate.size_mult({"level": 30.0, "change_pct": 0}) == 0.1

    def test_carry_reason(self):
        assert vix_gate.carry_reason({"level": 20.0, "change_pct": 0}).startswith("VIX_HIGH_CARRY")
        assert vix_gate.carry_reason({"level": 14.0, "change_pct": 6.0}).startswith("VIX_SPIKE_CARRY")
        assert vix_gate.carry_reason({"level": 14.0, "change_pct": 5.9}) is None
        assert vix_gate.carry_reason(None) is None

    def test_snapshot_caches_and_fails_open(self, monkeypatch):
        calls = []
        monkeypatch.setattr(vix_gate, "_fetch", lambda: calls.append(1) or {"level": 16.0, "change_pct": 1.0})
        assert vix_gate.snapshot()["level"] == 16.0 and vix_gate.snapshot()["level"] == 16.0
        assert len(calls) == 1

    def test_snapshot_failure_is_none_and_not_retried_at_once(self, monkeypatch):
        calls = []

        def boom():
            calls.append(1)
            raise TimeoutError("down")
        monkeypatch.setattr(vix_gate, "_fetch", boom)
        assert vix_gate.snapshot() is None and vix_gate.snapshot() is None
        assert len(calls) == 1

    def test_snapshot_off(self, monkeypatch):
        monkeypatch.setattr(config, "VIX_GATE_ENABLED", False)
        monkeypatch.setattr(vix_gate, "_fetch", lambda: pytest.fail("must not fetch"))
        assert vix_gate.snapshot() is None


class TestVwap:
    def test_below_vwap_rejects_beyond_tolerance(self):
        assert vwap_gate.reject_reason(99.0, 100.0).startswith("VWAP_BELOW")
        assert vwap_gate.reject_reason(99.9, 100.0) is None
        assert vwap_gate.reject_reason(101.0, 100.0) is None

    def test_unknown_or_insane_vwap_never_blocks(self):
        for v in (None, 0, -5, "x", float("nan")):
            assert vwap_gate.reject_reason(90.0, v) is None
        assert vwap_gate.reject_reason(80.0, 100.0) is None
        assert vwap_gate.reject_reason(None, 100.0) is None

    def test_off_switch_and_tick(self, monkeypatch):
        class T:
            price, vwap = 99.0, 100.0
        assert vwap_gate.reject_reason_tick(T()) is not None
        assert vwap_gate.reject_reason_tick(object()) is None
        monkeypatch.setattr(config, "ENTRY_VWAP_GATE_ENABLED", False)
        assert vwap_gate.reject_reason_tick(T()) is None


class TestNews:
    def test_low_score_blocks(self):
        assert event_risk.negative_news_reason({"event_score": 30}).startswith("NEWS_NEGATIVE")
        assert event_risk.negative_news_reason({"event_score": 35}) is not None
        assert event_risk.negative_news_reason({"event_score": 36}) is None
        assert event_risk.negative_news_reason({"event_score": 80}) is None

    def test_hard_negatives(self):
        body = {"event_score": 50, "event_score_breakdown": [{"type": "regulatory_action", "decayed_impact": -20.0}]}
        assert "regulatory_action" in event_risk.negative_news_reason(body)
        body = {"event_score": 50, "event_score_breakdown": [{"type": "delisting_risk", "decayed_impact": -9.0}]}
        assert event_risk.negative_news_reason(body) is not None
        body = {"event_score": 50, "event_score_breakdown": [{"type": "earnings_miss", "decayed_impact": -16.0}]}
        assert event_risk.negative_news_reason(body) is not None

    def test_faded_or_mild_events_do_not_block(self):
        body = {"event_score": 50, "event_score_breakdown": [{"type": "regulatory_action", "decayed_impact": -3.5},
                                                              {"type": "earnings_miss", "decayed_impact": -4.0},
                                                              {"type": "analyst_downgrade", "decayed_impact": -10.0}]}
        assert event_risk.negative_news_reason(body) is None

    def test_missing_or_garbage_is_none(self, monkeypatch):
        for b in (None, {}, [], "x", {"event_score": None}, {"event_score": "bad"}, {"event_score_breakdown": [None, 3]}):
            assert event_risk.negative_news_reason(b) is None
        monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_SCORE", 0.0)
        assert event_risk.negative_news_reason({"event_score": 1}) is None
        monkeypatch.setattr(config, "NEWS_NEGATIVE_BLOCK_ENABLED", False)
        assert event_risk.negative_news_reason({"event_score": 1}) is None

    def test_news_reason_uses_one_cached_lookup(self, monkeypatch):
        calls = []
        monkeypatch.setattr(event_risk, "_fetch", lambda s: calls.append(s) or {"event_score": 20})
        assert event_risk.news_reason("ABC").startswith("NEWS_NEGATIVE")
        assert event_risk.news_reason("ABC").startswith("NEWS_NEGATIVE")
        event_risk.lookup("ABC")
        assert calls == ["ABC"]

    def test_news_reason_fails_open(self, monkeypatch):
        def down(s):
            raise TimeoutError("x")
        monkeypatch.setattr(event_risk, "_fetch", down)
        assert event_risk.news_reason("ABC") is None


class _Tk:
    def __init__(self, price, prev, age_s=10, age_known=True, base=None):
        self.price, self.prev_close, self.age_known = price, prev, age_known
        self.as_of = (base or datetime.now(timezone.utc)) - timedelta(seconds=age_s)


MON_0910 = datetime(2026, 10, 12, 9, 10, tzinfo=IST)
MON_0905 = datetime(2026, 10, 12, 9, 5, tzinfo=IST)
MON_0916 = datetime(2026, 10, 12, 9, 16, tzinfo=IST)
SAT_0910 = datetime(2026, 10, 10, 9, 10, tzinfo=IST)
MON_1030 = datetime(2026, 10, 12, 10, 30, tzinfo=IST)
MON_1130 = datetime(2026, 10, 12, 11, 30, tzinfo=IST)


class TestPreopen:
    def test_gap_pct(self):
        assert round(preopen.gap_pct(106, 100), 6) == 6.0
        for a, b in ((0, 100), (100, 0), (None, 1), ("x", 1), (200, 100)):
            assert preopen.gap_pct(a, b) is None

    def test_due_window(self):
        assert preopen.due(MON_0910) is True
        assert preopen.due(MON_0905) is False and preopen.due(MON_0916) is False
        assert preopen.due(SAT_0910) is False

    def test_due_once_per_day_and_switch(self, monkeypatch):
        preopen.record({"A": _Tk(106, 100, base=MON_0910)}, now=MON_0910)
        assert preopen.due(MON_0910) is False
        preopen.reset()
        monkeypatch.setattr(config, "PREOPEN_SNAPSHOT_ENABLED", False)
        assert preopen.due(MON_0910) is False

    def test_record_keeps_only_fresh_prices(self):
        n = preopen.record({"A": _Tk(106, 100), "B": _Tk(106, 100, age_s=900), "C": _Tk(106, 100, age_known=False),
                            "D": _Tk(0, 100), "E": object()}, now=None)
        assert n == 1 and preopen.status()["symbols_with_gap"] == 1

    def test_gap_reasons_inside_window(self):
        preopen.record({"UP": _Tk(106.5, 100, base=MON_0910), "DN": _Tk(96.5, 100, base=MON_0910),
                        "OK": _Tk(101, 100, base=MON_0910)}, now=MON_0910)
        assert preopen.gap_reason("UP", MON_1030).startswith("PREOPEN_GAP_UP")
        assert preopen.gap_reason("DN", MON_1030).startswith("PREOPEN_GAP_DOWN")
        assert preopen.gap_reason("OK", MON_1030) is None
        assert preopen.gap_reason("UNKNOWN", MON_1030) is None

    def test_gate_expires_and_is_per_day(self):
        preopen.record({"UP": _Tk(106.5, 100, base=MON_0910)}, now=MON_0910)
        assert preopen.gap_reason("UP", MON_1130) is None
        assert preopen.gap_reason("UP", datetime(2026, 10, 13, 9, 30, tzinfo=IST)) is None

    def test_thresholds_can_be_disabled(self, monkeypatch):
        preopen.record({"UP": _Tk(106.5, 100, base=MON_0910), "DN": _Tk(96.5, 100, base=MON_0910)}, now=MON_0910)
        monkeypatch.setattr(config, "PREOPEN_GAP_UP_BLOCK_PCT", 0.0)
        monkeypatch.setattr(config, "PREOPEN_GAP_DOWN_BLOCK_PCT", 0.0)
        assert preopen.gap_reason("UP", MON_1030) is None and preopen.gap_reason("DN", MON_1030) is None

    def test_run_records_and_warms_and_never_raises(self, monkeypatch):
        import market_feed.feed as feed

        async def fake_quotes(syms, **kw):
            return {s: _Tk(106.5, 100) for s in syms}
        monkeypatch.setattr(feed, "get_quotes", fake_quotes)
        warmed = []
        monkeypatch.setattr(preopen, "_warm_blocking", lambda syms: warmed.append(list(syms)) or len(syms))
        st = asyncio.run(preopen.run(["abc", "XYZ", "abc"]))
        assert st["symbols_with_gap"] == 2 and warmed == [["ABC", "XYZ"]] and st["warmed"] == 2

    def test_run_survives_feed_failure_and_marks_the_day(self, monkeypatch):
        import market_feed.feed as feed

        async def boom(syms, **kw):
            raise RuntimeError("feed down")
        monkeypatch.setattr(feed, "get_quotes", boom)
        st = asyncio.run(preopen.run(["A"]))
        assert st["symbols_with_gap"] == 0 and preopen.status()["day"] is not None

    def test_warm_blocking_calls_each_cache(self, monkeypatch):
        from entry_engine import prev_day
        seen = {"pd": None, "vix": 0, "ev": []}
        monkeypatch.setattr(prev_day, "prefetch", lambda s: seen.__setitem__("pd", list(s)))
        monkeypatch.setattr(vix_gate, "snapshot", lambda: seen.__setitem__("vix", seen["vix"] + 1))
        monkeypatch.setattr(event_risk, "lookup", lambda s: seen["ev"].append(s))
        monkeypatch.setattr(config, "PREOPEN_WARM_MAX_SYMBOLS", 2)
        assert preopen._warm_blocking(["A", "B", "C"]) == 2
        assert seen["pd"] == ["A", "B", "C"] and seen["vix"] == 1 and seen["ev"] == ["A", "B"]


class TestCarryContext:
    def test_parse_indices(self):
        assert carry_context.parse_indices({"nifty_vs_prev_close": {"change_pct": -1.2}}) == -1.2
        for bad in (None, {}, {"stale": True, "nifty_vs_prev_close": {"change_pct": -3}},
                    {"fallback": True, "nifty_vs_prev_close": {"change_pct": -3}}, {"nifty_vs_prev_close": {"change_pct": "x"}},
                    {"nifty_vs_prev_close": None}, "str"):
            assert carry_context.parse_indices(bad) is None

    def test_market_reason(self):
        assert carry_context.market_reason(-0.8).startswith("CARRY_MARKET_WEAK")
        assert carry_context.market_reason(-0.79) is None
        assert carry_context.market_reason(None) is None
        assert carry_context.market_reason(-2.0).startswith("CARRY_MARKET_WEAK")

    def test_reason_order_and_inputs(self, monkeypatch):
        monkeypatch.setattr(event_risk, "news_reason", lambda s: "NEWS_NEGATIVE: x")
        assert carry_context.reason("A", -1.0, {"level": 25.0, "change_pct": 0}).startswith("CARRY_MARKET_WEAK")
        assert carry_context.reason("A", 0.2, {"level": 25.0, "change_pct": 0}).startswith("VIX_HIGH_CARRY")
        assert carry_context.reason("A", 0.2, {"level": 12.0, "change_pct": 0}) == "NEWS_NEGATIVE: x"
        assert carry_context.reason("A", 0.2, {"level": 12.0, "change_pct": 0}, check_news=False) is None
        monkeypatch.setattr(config, "CARRY_BLOCK_NEGATIVE_NEWS", False)
        assert carry_context.reason("A", 0.2, None) is None

    def test_reason_all_unknown_is_none_and_switch(self, monkeypatch):
        monkeypatch.setattr(event_risk, "news_reason", lambda s: None)
        assert carry_context.reason("A", None, None) is None
        monkeypatch.setattr(config, "CARRY_CONTEXT_GATE_ENABLED", False)
        assert carry_context.reason("A", -5.0, {"level": 90.0, "change_pct": 50}) is None

    def test_nifty_fetch_caches_and_fails_open(self, monkeypatch):
        calls = []
        monkeypatch.setattr(carry_context, "_fetch_nifty", lambda: calls.append(1) or -0.4)
        assert carry_context.nifty_change_pct() == -0.4 and carry_context.nifty_change_pct() == -0.4
        assert len(calls) == 1
        carry_context.reset()

        def down():
            raise TimeoutError("x")
        monkeypatch.setattr(carry_context, "_fetch_nifty", down)
        assert carry_context.nifty_change_pct() is None


class TestTickVwap:
    def test_tick_has_vwap_slot_default_none(self):
        from market_feed.feed import Tick
        t = Tick("A", 100.0, datetime.now(timezone.utc), None, "x")
        assert t.vwap is None
        assert Tick("A", 100.0, datetime.now(timezone.utc), None, "x", vwap=99.5).vwap == 99.5

    def test_bulk_row_and_quote_row_parse_avg_price(self):
        import market_feed.feed as feed
        src = open(feed.__file__, encoding="utf-8").read()
        assert src.count('vwap=_safe_circuit(') == 2 and src.count('vwap=getattr(t, "vwap", None)') == 2
