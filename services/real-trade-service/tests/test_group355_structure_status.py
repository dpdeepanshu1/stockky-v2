"""group 355: structure gate observability in real-trade - counters, recent ring, stored-decision tally, entry feed and GET /structure/status/{mode}."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from entry_engine import structure_gate as sg


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sg.reset()
    monkeypatch.setattr(config, "STRUCTURE_FETCH_ENABLED", True, raising=False)
    yield
    sg.reset()


def _verdict(**kw):
    v = {"opinion": "ok", "would_reject": False, "reasons": [], "no_data": None, "metrics": {}}
    v.update(kw)
    return v


@pytest.mark.parametrize("text,key", [
    ("chase 1.8 ATR above the nearest demand/imbalance zone (max 1.5)", "chase"),
    ("price stretched 3.4 ATR above EMA20 (max 3)", "stretch"),
    ("only 0.2 ATR of room to the next resistance (min 0.5)", "room"),
    ("structure R:R 0.90 below 1.2", "rr"),
    ("structural stop is too wide", "stop_wide"),
    ("fresh bearish break of structure 2 bar(s) ago", "bos"),
    ("daily structure is bearish", "bearish"),
    ("something new", "other"), (None, "other"),
])
def test_reason_key(text, key):
    assert sg.reason_key(text) == key


@pytest.mark.parametrize("text,key", [
    ("no structure summary", "no_summary"), ("structure quality 'insufficient'", "quality"), ("structure last bar is 9d old", "stale"),
    ("live price 10.00 is 40% from the structure close 7.00 (split / bad data)", "price_dev"), ("no price to compare", "no_price"),
    ("no ATR in the structure summary", "no_atr"), ("evaluation error", "error"), ("weird", "other"), (None, "other"), ("None", "other"),
])
def test_nodata_key(text, key):
    assert sg.nodata_key(text) == key


def test_reason_keys_match_real_evaluate_output():
    """The buckets must cover every reason string evaluate() really emits (guards against wording drift)."""
    from datetime import datetime, timezone
    s = {"ms_quality": "ok", "ms_asof": datetime.now(timezone.utc).isoformat(), "ms_trend": "bearish", "ms_price": 100.0, "ms_atr": 2.0,
         "ms_chase_atr": 3.0, "ms_stretch_atr": 5.0, "ms_room_atr": 0.0, "ms_stop": 99.5, "ms_target": 100.4, "ms_rr_flags": ["stop_too_wide"]}
    v = sg.evaluate(s, 100.0)
    assert v["would_reject"]
    keys = {sg.reason_key(r) for r in v["reasons"]}
    assert "other" not in keys and {"chase", "stretch", "room", "bearish"} <= keys


def test_record_counts_ok_reject_nodata_and_ring():
    sg.record("aaa", "demo", _verdict(), "enforce", False, 100.0, "payload")
    sg.record("BBB", "REAL", _verdict(would_reject=True, reasons=["chase 2.0 ATR above x", "daily structure is bearish"]), "enforce", True, 50.0, "gateway")
    sg.record("CCC", "REAL", _verdict(opinion="no_opinion", no_data="no structure summary"), "enforce", True, None, None)
    st = sg.stats()
    assert (st["evaluated"], st["ok"], st["no_opinion"], st["would_reject"], st["enforced"], st["nodata_blocked"]) == (3, 2, 1, 1, 2, 1)
    assert st["by_reason"] == {"chase": 1, "bearish": 1} and st["by_nodata"] == {"no_summary": 1}
    assert st["coverage_pct"] == pytest.approx(66.7, abs=0.1)
    assert [r["symbol"] for r in st["recent"]] == ["CCC", "BBB", "AAA"]
    assert st["recent"][1]["price"] == 50.0 and st["recent"][2]["mode"] == "DEMO"
    assert "since" not in st and st["config"]["STRUCTURE_GATE_MODE"] == config.STRUCTURE_GATE_MODE


def test_stats_empty_has_no_coverage():
    assert sg.stats()["coverage_pct"] is None


def test_record_never_raises():
    class Boom(dict):
        def get(self, *a, **k):
            raise RuntimeError("x")
    sg.record("A", "DEMO", Boom(), "enforce", False)
    assert sg.stats()["errors"] == 1


def test_ring_is_bounded():
    for i in range(130):
        sg.record(f"S{i}", "DEMO", _verdict(), "enforce", False)
    st = sg.stats()
    assert st["evaluated"] == 130 and len(st["recent"]) == 30 and len(sg._recent) == 100


def test_reset_clears_counters():
    sg.record("A", "DEMO", _verdict(), "enforce", False)
    sg.reset()
    assert sg.stats()["evaluated"] == 0 and sg.stats()["recent"] == []


# ── fetch counters ──────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_fetch_disabled_is_counted(monkeypatch):
    import asyncio
    monkeypatch.setattr(config, "STRUCTURE_FETCH_ENABLED", False, raising=False)
    assert asyncio.run(sg.fetch_summary("ABC")) is None
    assert sg.stats()["fetch_disabled"] == 1


def test_fetch_failure_then_cache_hit(monkeypatch):
    import asyncio
    monkeypatch.setattr(config, "API_GATEWAY_URL", "http://127.0.0.1:1", raising=False)
    monkeypatch.setattr(config, "STRUCTURE_FETCH_TIMEOUT_S", 0.2, raising=False)
    assert asyncio.run(sg.fetch_summary("ABC")) is None
    assert asyncio.run(sg.fetch_summary("ABC")) is None          # failure is cached (STRUCTURE_FAIL_TTL_S)
    st = sg.stats()
    assert st["fetch_fail"] == 1 and st["fetch_cache_hit"] == 1


# ── stored-decision tally ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def _row(sym, price, **doc):
    d = {"opinion": "ok", "would_reject": False, "enforced": False, "reasons": [], "no_data": None}
    d.update(doc)
    return {"symbol": sym, "created_at": "2026-10-14T04:00:00Z", "price": price, "structure_json": json.dumps(d)}


def test_tally_basic_and_moves():
    rows = [
        _row("UP", 100.0, would_reject=True, enforced=True, reasons=["chase 2.0 ATR above z"]),
        _row("UP", 90.0, would_reject=True, enforced=True, reasons=["chase 2.0 ATR above z"]),     # older verdict for same symbol: ignored for the move
        _row("DOWN", 200.0, would_reject=True, enforced=False, reasons=["structure R:R 0.5 below 1.2", "daily structure is bearish"]),
        _row("FINE", 50.0),
        _row("NODATA", 10.0, opinion="no_opinion", no_data="structure quality 'insufficient'"),
        {"symbol": "RAW", "created_at": None, "price": 1.0, "structure_json": None},
        {"symbol": "BAD", "created_at": None, "price": 1.0, "structure_json": "{not json"},
    ]
    t = sg.tally_decisions(rows, {"UP": 110.0, "DOWN": 180.0})
    assert t["decisions"] == 7 and t["with_snapshot"] == 5 and t["ok"] == 4 and t["no_opinion"] == 1
    assert (t["would_reject"], t["enforced"]) == (3, 2) and "shadow_only" not in t
    assert t["by_reason"] == {"chase": 2, "rr": 1, "bearish": 1} and t["by_nodata"] == {"quality": 1}
    assert t["coverage_pct"] == 80.0
    assert [r["symbol"] for r in t["rejects"]] == ["UP", "DOWN"]
    up = t["rejects"][0]
    assert up["price"] == 100.0 and up["now"] == 110.0 and up["since_pct"] == 10.0 and up["enforced"] is True
    assert t["rejects_up"] == 1 and t["rejects_down"] == 1 and t["avg_since_pct"] == pytest.approx(0.0)


def test_tally_empty_and_no_price():
    t = sg.tally_decisions([])
    assert t["decisions"] == 0 and t["coverage_pct"] is None and t["avg_since_pct"] is None and t["rejects"] == []
    t = sg.tally_decisions([_row("X", 10.0, would_reject=True, reasons=["x"])], {})
    assert t["rejects"][0]["since_pct"] is None and t["avg_since_pct"] is None and t["rejects_up"] == 0


def test_tally_caps_reject_list():
    rows = [_row(f"S{i}", 10.0, would_reject=True, reasons=["x"]) for i in range(30)]
    assert len(sg.tally_decisions(rows, max_rejects=5)["rejects"]) == 5


def test_tally_accepts_dict_structure_json():
    r = {"symbol": "A", "price": 5.0, "structure_json": {"opinion": "ok", "would_reject": False}}
    assert sg.tally_decisions([r])["ok"] == 1
