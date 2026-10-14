"""group 355: structure gate observability in position-stocks - counters, recent ring (+move since), stored tally from the candidate log
and the position snapshots, and the check() feed."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import structure_gate as sg


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in (("STRUCTURE_GATE_MODE", "enforce"), ("STRUCTURE_REQUIRE_DATA", False), ("STRUCTURE_BEARISH_BLOCK", True),
                 ("STRUCTURE_BEARISH_BOS_BARS", 5), ("STRUCTURE_MAX_CHASE_ATR", 4.0), ("STRUCTURE_MAX_STRETCH_ATR", 5.0),
                 ("STRUCTURE_MIN_ROOM_ATR", 0.3), ("STRUCTURE_MAX_AGE_DAYS", 7.0), ("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0)):
        monkeypatch.setattr(config, k, v, raising=False)
    sg.reset()
    yield
    sg.reset()


def _sum(**kw):
    d = {"ms_quality": "ok", "ms_asof": datetime.now(timezone.utc).isoformat(), "ms_price": 100.0, "ms_atr": 2.0, "ms_trend": "bullish",
         "ms_chase_atr": 0.5, "ms_stretch_atr": 1.0, "ms_room_atr": 3.0}
    d.update(kw)
    return d


def _v(**kw):
    v = {"opinion": "ok", "would_reject": False, "reasons": [], "no_data": None, "metrics": {}}
    v.update(kw)
    return v


@pytest.mark.parametrize("text,key", [
    ("chase 5.0 daily ATR above the nearest demand zone (max 4)", "chase"), ("stretched 6.0 daily ATR above EMA20 (max 5)", "stretch"),
    ("only 0.1 daily ATR of room to the next resistance (min 0.3)", "room"), ("fresh bearish break of structure 2 bar(s) ago", "bos"),
    ("daily structure is bearish", "bearish"), ("zzz", "other"), (None, "other")])
def test_reason_key(text, key):
    assert sg.reason_key(text) == key


def test_reason_keys_cover_real_evaluate_output():
    v = sg.evaluate(_sum(ms_trend="bearish", ms_chase_atr=9.0, ms_stretch_atr=9.0, ms_room_atr=0.0), 100.0)
    keys = {sg.reason_key(r) for r in v["reasons"]}
    assert "other" not in keys and {"chase", "stretch", "room", "bearish"} <= keys


@pytest.mark.parametrize("text,key", [("no structure summary", "no_summary"), ("structure quality 'insufficient'", "quality"),
                                      ("structure last bar is 9d old", "stale"), ("live price 1 is 40% from the structure close 7 (split / bad data)", "price_dev"),
                                      ("no price to compare", "no_price"), ("no ATR in the structure summary", "no_atr"),
                                      ("evaluation error", "error"), (None, "other")])
def test_nodata_key(text, key):
    assert sg.nodata_key(text) == key


def test_record_counts_and_ring():
    sg.record("aaa", _v(), "shadow", False, 10.0)
    sg.record("BBB", _v(would_reject=True, reasons=["daily structure is bearish", "chase 5.0 daily ATR above x"]), "enforce", True, 20.0)
    sg.record("CCC", _v(opinion="no_opinion", no_data="no structure summary"), "enforce", True)
    st = sg.stats()
    assert (st["evaluated"], st["ok"], st["no_opinion"], st["would_reject"], st["enforced"], st["nodata_blocked"]) == (3, 2, 1, 1, 2, 1)
    assert st["by_reason"] == {"bearish": 1, "chase": 1} and st["by_nodata"] == {"no_summary": 1}
    assert st["coverage_pct"] == pytest.approx(66.7, abs=0.1)
    assert [r["symbol"] for r in st["recent"]] == ["CCC", "BBB", "AAA"] and st["recent"][2]["price"] == 10.0
    assert "since" not in st and st["config"]["STRUCTURE_GATE_MODE"] == "enforce"


def test_stats_empty_and_reset():
    assert sg.stats()["coverage_pct"] is None
    sg.record("A", _v(), "shadow", False)
    sg.reset()
    assert sg.stats()["evaluated"] == 0


def test_record_never_raises():
    class Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("x")
    sg.record("A", Boom(), "shadow", False)
    assert sg.stats()["errors"] == 1


def test_ring_bounded():
    for i in range(130):
        sg.record(f"S{i}", _v(), "shadow", False)
    assert sg.stats()["evaluated"] == 130 and len(sg.stats()["recent"]) == 30 and len(sg._recent) == 100


def test_with_moves():
    rec = [{"symbol": "A", "price": 100.0}, {"symbol": "B", "price": 50.0}, {"symbol": "C", "price": None}]
    out = sg.with_moves(rec, {"A": 103.0, "B": None, "C": 5.0})
    assert out[0]["since_pct"] == 3.0 and out[0]["now"] == 103.0
    assert out[1]["since_pct"] is None and out[2]["since_pct"] is None


# ── check() feeds the counters ────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_check_records_enforced_refusal(monkeypatch):
    monkeypatch.setattr(sg, "fetch_summary", lambda s: _sum(ms_trend="bearish"))
    reason, snap = sg.check("xyz", 100.0)
    assert reason and reason.startswith("STRUCTURE_BAD_LOCATION") and snap
    st = sg.stats()
    assert st["evaluated"] == 1 and st["would_reject"] == 1 and st["enforced"] == 1
    assert st["recent"][0]["symbol"] == "XYZ" and st["recent"][0]["price"] == 100.0


def test_check_shadow_records_but_allows(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "shadow", raising=False)
    monkeypatch.setattr(sg, "fetch_summary", lambda s: _sum(ms_trend="bearish"))
    reason, snap = sg.check("XYZ", 100.0)
    assert reason is None and snap
    st = sg.stats()
    assert st["would_reject"] == 1 and st["enforced"] == 0


def test_check_off_records_nothing(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "off", raising=False)
    assert sg.check("XYZ", 100.0) == (None, None)
    assert sg.stats()["evaluated"] == 0


def test_check_nodata_require_data(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_REQUIRE_DATA", True, raising=False)
    monkeypatch.setattr(sg, "fetch_summary", lambda s: None)
    reason, _ = sg.check("XYZ", 100.0)
    assert reason and reason.startswith("STRUCTURE_NODATA")
    st = sg.stats()
    assert st["nodata_blocked"] == 1 and st["by_nodata"] == {"no_summary": 1}


def test_fetch_failure_then_cache_hit(monkeypatch):
    monkeypatch.setattr(config, "_API_GATEWAY_URL", "http://127.0.0.1:1", raising=False)
    monkeypatch.setattr(config, "STRUCTURE_FETCH_TIMEOUT_S", 0.2, raising=False)
    assert sg.fetch_summary("ABC") is None and sg.fetch_summary("ABC") is None
    st = sg.stats()
    assert st["fetch_fail"] == 1 and st["fetch_cache_hit"] == 1


# ── stored tally ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_tally_stored():
    logs = [
        {"symbol": "A", "reason": "STRUCTURE_BAD_LOCATION:daily structure is bearish; chase 5.0 daily ATR above the nearest demand zone (max 4)",
         "decision": "SKIPPED", "created_at": "t1"},
        {"symbol": "A", "reason": "STRUCTURE_BAD_LOCATION:daily structure is bearish", "decision": "SKIPPED", "created_at": "t0"},
        {"symbol": "B", "reason": "STRUCTURE_NODATA:no structure summary", "decision": "SKIPPED", "created_at": "t2"},
        {"symbol": "C", "reason": "QUALITY_GATE:x", "decision": "SKIPPED", "created_at": "t3"},
        {"symbol": "D", "reason": None, "decision": "ENTERED", "created_at": "t4"},
    ]
    pos = [
        {"symbol": "E", "structure_json": json.dumps({"opinion": "ok", "would_reject": False})},
        {"symbol": "F", "structure_json": json.dumps({"opinion": "ok", "would_reject": True, "reasons": ["daily structure is bearish"]})},
        {"symbol": "G", "structure_json": json.dumps({"opinion": "no_opinion", "no_data": "no structure summary"})},
        {"symbol": "H", "structure_json": None}, {"symbol": "I", "structure_json": "{bad"},
    ]
    t = sg.tally_stored(logs, pos)
    assert (t["refused"], t["refused_bad_location"], t["refused_nodata"]) == (3, 2, 1)
    assert t["by_reason"] == {"bearish": 2, "chase": 1} and t["by_nodata"] == {"no_summary": 1}
    assert [r["symbol"] for r in t["refused_symbols"]] == ["A", "B"]
    assert (t["entered_with_snapshot"], t["entered_ok"], t["entered_no_opinion"], t["entered_would_reject"]) == (3, 2, 1, 1)
    assert t["entered_by_reason"] == {"bearish": 1} and t["coverage_pct"] == pytest.approx(66.7, abs=0.1)


def test_tally_stored_empty():
    t = sg.tally_stored([], [])
    assert t["refused"] == 0 and t["coverage_pct"] is None and t["refused_symbols"] == []
