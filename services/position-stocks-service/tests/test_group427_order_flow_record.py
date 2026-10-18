"""group 427: order-flow record on a new position (orders/order_flow.py). Record only - nothing here decides anything."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from orders import depth_gate, order_flow as of

Q = {"price": 101.0, "spread_pct": 0.12, "book_value_5": 900000.0, "bid_qty_5": 1000.0, "ask_qty_5": 3000.0, "avg_price": 100.0,
     "open": 99.0, "day_high": 102.0, "day_low": 98.0, "previous_close": 100.0, "volume": 5000}


def test_imbalance():
    assert of.imbalance(1000, 3000) == -0.5 and of.imbalance(3000, 1000) == 0.5
    assert of.imbalance(0, 0) is None and of.imbalance(None, 1) is None and of.imbalance(True, 1) is None


def test_snapshot_dict():
    d = of.snapshot_dict(Q, 101.0)
    assert d["imbalance_5"] == -0.5 and d["vs_vwap_pct"] == 1.0 and d["day_pos"] == 0.75 and d["spread_pct"] == 0.12
    assert of.snapshot_dict({"price": 5.0}, 5.0) is None and of.snapshot_dict(None) is None and of.snapshot_dict("x") is None


def test_snapshot_json_uses_the_cached_quote_and_never_raises(monkeypatch):
    monkeypatch.setattr(config, "ORDER_FLOW_DEPTH20", False)
    monkeypatch.setattr(depth_gate, "_fresh_quote", lambda s: Q)
    j = json.loads(of.snapshot_json("AAA", 101.0, 10))
    assert j["imbalance_5"] == -0.5
    monkeypatch.setattr(depth_gate, "_fresh_quote", lambda s: (_ for _ in ()).throw(RuntimeError("x")))
    assert of.snapshot_json("AAA", 101.0, 10) is None


def test_snapshot_json_merges_depth20(monkeypatch):
    monkeypatch.setattr(config, "ORDER_FLOW_DEPTH20", True)
    monkeypatch.setattr(depth_gate, "_fresh_quote", lambda s: Q)
    monkeypatch.setattr(of, "_fetch_d20", lambda s, q: {"imbalance_20": 0.4})
    j = json.loads(of.snapshot_json("AAA", 101.0, 10))
    assert j["d20"] == {"imbalance_20": 0.4}
    assert of.parse(json.dumps(j))["imbalance_20"] == 0.4


def test_depth20_alone_still_records_when_the_quote_has_no_depth(monkeypatch):
    monkeypatch.setattr(config, "ORDER_FLOW_DEPTH20", True)
    monkeypatch.setattr(depth_gate, "_fresh_quote", lambda s: None)
    monkeypatch.setattr(of, "_fetch_d20", lambda s, q: {"imbalance_20": -0.2})
    assert json.loads(of.snapshot_json("AAA", 101.0, 10))["d20"]["imbalance_20"] == -0.2


def test_off_switch(monkeypatch):
    monkeypatch.setattr(config, "ORDER_FLOW_RECORD", False)
    assert of.snapshot_json("AAA", 101.0, 10) is None


def test_fetch_d20_unreachable_is_none(monkeypatch):
    monkeypatch.setattr(config, "MARKET_DATA_URL", "http://127.0.0.1:9")
    monkeypatch.setattr(config, "ORDER_FLOW_DEPTH20_TIMEOUT_S", 0.2)
    assert of._fetch_d20("AAA", 5) is None


def test_d20_subset_and_parse_garbage():
    assert of.d20_subset({"available": False}) is None and of.d20_subset({"available": True, "imbalance_20": 0.1, "x": 1}) == {"imbalance_20": 0.1}
    assert of.parse(None) == {} and of.parse("{bad") == {} and of.parse("[1]") == {}


def test_model_and_migration_know_the_column():
    import models
    import db
    assert hasattr(models.ScalpPosition, "flow_json")
    assert any(r[0] == "scalp_positions" and r[1] == "flow_json" for r in db._COLUMN_MIGRATIONS)
