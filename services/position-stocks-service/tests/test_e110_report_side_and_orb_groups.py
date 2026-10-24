"""E110: /trades/report groups by side (LONG / SHORT) and by the E104 ORB bar fields (close_trigger, trigger-bar volume ratio). Report only; nothing is decided from it."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace as NS

from orders import trade_report as tr
from tz_utils import IST


def _t(pnl=10.0, **kw):
    opened = datetime(2026, 10, 9, 10, 5, tzinfo=IST).astimezone(timezone.utc)
    closed = datetime(2026, 10, 9, 10, 20, tzinfo=IST).astimezone(timezone.utc)
    d = dict(symbol="ABC", opened_at=opened, closed_at=closed, entry_price=100.0, exit_price=100.0 + pnl / 100, quantity=100, status="TARGET_HIT",
             window_source="5m", signal_price=None, realized_pnl=pnl, error_message=None)
    d.update(kw)
    return NS(**d)


def _orb(**o):
    return json.dumps({"orb": o})


def test_side_split_and_legacy_rows_are_long():
    rep = tr.report([_t(50.0), _t(40.0, side="LONG"), _t(30.0, side=None), _t(-20.0, side="short", entry_price=100.0, exit_price=100.2)])
    assert rep["by_side"]["LONG"]["trades"] == 3 and rep["by_side"]["SHORT"]["trades"] == 1
    assert tr.trade_record(_t(5.0, side="SHORT"))["side"] == "SHORT"


def test_close_trigger_buckets_never_count_a_missing_record_as_no_trigger():
    assert tr.orb_trigger_bucket({}) == "no_orb_record"
    assert tr.orb_trigger_bucket({"would_enter": True}) == "no_orb_record"
    assert tr.orb_trigger_bucket({"close_trigger": True}) == "close_trigger"
    assert tr.orb_trigger_bucket({"close_trigger": False}) == "no_close_trigger"
    rep = tr.report([_t(10.0, flow_json=_orb(close_trigger=True)), _t(-5.0, flow_json=_orb(close_trigger=False)), _t(7.0), _t(8.0, flow_json="not json")])
    g = rep["by_orb_close_trigger"]
    assert g["close_trigger"]["trades"] == 1 and g["no_close_trigger"]["trades"] == 1 and g["no_orb_record"]["trades"] == 2


def test_volume_ratio_buckets():
    cases = {0.4: "<1.0", 1.0: "1.0-1.5", 1.49: "1.0-1.5", 1.5: "1.5-2.5", 2.49: "1.5-2.5", 2.5: ">=2.5", 9.0: ">=2.5"}
    for v, want in cases.items():
        assert tr.orb_volume_bucket({"volume_ratio": v}) == want, v
    for bad in ({}, {"volume_ratio": None}, {"volume_ratio": "x"}, {"volume_ratio": float("nan")}):
        assert tr.orb_volume_bucket(bad) == "none"
    rep = tr.report([_t(10.0, flow_json=_orb(volume_ratio=3.0)), _t(5.0, flow_json=_orb(volume_ratio=0.5)), _t(1.0)])
    assert set(rep["by_orb_volume_ratio"]) == {">=2.5", "<1.0", "none"}


def test_empty_report_lists_the_new_groups_and_flow_json_dict_is_accepted():
    rep = tr.report([])
    assert rep["by_side"] == {} and rep["by_orb_close_trigger"] == {} and rep["by_orb_volume_ratio"] == {}
    assert tr.trade_record(_t(5.0, flow_json={"orb": {"close_trigger": True}}))["orb_trigger"] == "close_trigger"
