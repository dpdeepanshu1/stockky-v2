"""group 427: order-flow numbers recorded on the entry decision (entry_engine/order_flow.py) and the by_imbalance / by_spread expectancy groups. Record only."""
from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
import trade_records as tr
from entry_engine import order_flow as of
from market_feed.feed import Tick


def setup_function(_f):
    of._reset_for_tests()


def tick(**kw):
    d = dict(symbol="AAA", price=101.0, as_of=datetime.now(timezone.utc), atr=2.0, source="t", volume=5000, day_high=102.0, day_low=98.0,
             prev_close=100.0, spread_pct=0.12, book_value_5=900000.0, vwap=100.0, bid_qty_5=3000.0, ask_qty_5=1000.0, open_price=99.0)
    d.update(kw)
    return Tick(**d)


def test_imbalance_math_and_edges():
    assert of.imbalance(3000, 1000) == 0.5
    assert of.imbalance(1000, 3000) == -0.5
    assert of.imbalance(0, 0) is None and of.imbalance(None, 5) is None and of.imbalance(-1, 5) is None
    assert of.imbalance(5, 0) == 1.0


def test_snapshot_dict_values():
    d = of.snapshot_dict(tick())
    assert d["imbalance_5"] == 0.5 and d["spread_pct"] == 0.12 and d["vs_vwap_pct"] == 1.0
    assert d["vs_open_pct"] == round(2 / 99 * 100, 3) and d["day_pos"] == 0.75 and d["day_change_pct"] == 1.0


def test_snapshot_is_none_when_the_tick_has_no_depth_or_reference():
    t = Tick(symbol="B", price=50.0, as_of=datetime.now(timezone.utc), atr=None, source="x")
    assert of.snapshot_dict(t) is None
    assert of.snapshot_dict(None) is None


def test_record_writes_compact_json_and_never_raises():
    dec = NS(flow_json=None)
    assert of.record(dec, tick()) is True
    assert json.loads(dec.flow_json)["imbalance_5"] == 0.5
    assert of.record(NS(flow_json=None), object()) is False        # nothing readable -> no record, no error


def test_record_off_switch(monkeypatch):
    monkeypatch.setattr(config, "ORDER_FLOW_RECORD", False)
    dec = NS(flow_json=None)
    assert of.record(dec, tick()) is False and dec.flow_json is None


def test_d20_subset_needs_an_available_book():
    assert of.d20_subset({"available": False, "reason": "off"}) is None
    assert of.d20_subset("x") is None
    assert of.d20_subset({"available": True, "imbalance_20": -0.3, "junk": 1}) == {"imbalance_20": -0.3}


def test_add_d20_merges_under_d20(monkeypatch):
    async def fake(sym, qty):
        return {"imbalance_20": 0.31, "buy_impact_pct": 0.05}
    monkeypatch.setattr(of, "fetch_d20", fake)
    dec = NS(flow_json=None)
    of.record(dec, tick())
    assert asyncio.run(of.add_d20(dec, "AAA", 10)) is True
    j = json.loads(dec.flow_json)
    assert j["imbalance_5"] == 0.5 and j["d20"]["imbalance_20"] == 0.31


def test_add_d20_failure_leaves_the_decision_alone(monkeypatch):
    async def none(sym, qty):
        return None
    monkeypatch.setattr(of, "fetch_d20", none)
    dec = NS(flow_json='{"v":1}')
    assert asyncio.run(of.add_d20(dec, "AAA", 10)) is False and dec.flow_json == '{"v":1}'


def test_add_d20_off_switch(monkeypatch):
    monkeypatch.setattr(config, "ORDER_FLOW_DEPTH20", False)
    assert asyncio.run(of.add_d20(NS(flow_json=None), "AAA", 1)) is False


def test_fetch_d20_unreachable_market_data_is_no_opinion_and_cached(monkeypatch):
    monkeypatch.setattr(config, "ORDER_FLOW_DEPTH20_TIMEOUT_S", 0.2)
    import market_feed.feed as feed
    monkeypatch.setattr(feed, "MARKET_DATA_URL", "http://127.0.0.1:9")
    assert asyncio.run(of.fetch_d20("ZZZ", 5)) is None
    assert asyncio.run(of.fetch_d20("ZZZ", 5)) is None
    assert of.stats()["d20_fail"] == 1 and of.stats()["d20_cache_hit"] == 1


def test_parse_flattens_d20_and_survives_garbage():
    p = of.parse('{"imbalance_5":0.1,"d20":{"imbalance_20":-0.4}}')
    assert p["imbalance_5"] == 0.1 and p["imbalance_20"] == -0.4
    assert of.parse(None) == {} and of.parse("{bad") == {} and of.parse("[1]") == {}


def test_buckets():
    assert [of.imbalance_bucket(v) for v in (-0.5, 0.0, 0.5, None)] == ["ask_heavy", "balanced", "bid_heavy", "unknown"]
    assert [of.spread_bucket(v) for v in (0.1, 0.2, 0.5, None)] == ["tight", "normal", "wide", "unknown"]


def test_trade_record_carries_flow_and_groups_by_bucket():
    def utc(h):
        return datetime(2026, 10, 9, h, 0, tzinfo=timezone.utc)
    p = NS(id=1, mode="REAL", symbol="AAA", status="CLOSED", avg_entry_price=100.0, realized_pnl=-20.0, opened_at=utc(4), closed_at=utc(5),
           broker_imported=False, watchlist_entry_id=None, source_tab="watchlist", entry_decision_label="BUY NOW", entry_conviction_score=70.0,
           is_regime_override=False, net_realized_pnl=None, realized_cost_estimate=None)
    o = lambda i, side, did=None: NS(id=i, side=side, symbol="AAA", mode="REAL", status="FILLED", exit_reason=None, decision_id=did,
                                      watchlist_entry_id=None, execution_source="AUTO", created_at=utc(4), updated_at=utc(4))
    d = NS(id=7, candidate_id=None, proposed_price=100.0, proposed_stop=97.0, stop_basis="atr",
           flow_json='{"imbalance_5":-0.6,"spread_pct":0.4,"d20":{"imbalance_20":0.3}}')
    rec = tr.build_records([p], [o(1, "BUY", 7), o(2, "SELL")], [], [d], [], [], [])[0]
    assert rec["imbalance_5"] == -0.6 and rec["imbalance_20"] == 0.3 and rec["flow_spread_pct"] == 0.4
    rep = tr.expectancy_report([rec])
    assert "ask_heavy" in rep["by_imbalance_5"] and "bid_heavy" in rep["by_imbalance_20"] and "wide" in rep["by_spread"]


def test_old_decision_without_flow_is_unknown():
    rep = tr.expectancy_report([{"net_pnl": 5.0, "gross_pnl": 5.0}])
    assert list(rep["by_imbalance_5"]) == ["unknown"]
