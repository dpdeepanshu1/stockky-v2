"""group 351: daily market-structure gate (entry_engine/structure_gate.py) - pure verdicts, no-data = no opinion, live adjustment,
structural stop sanity, snapshot, mode selection, and the entry.py wiring (shadow records / enforce WAITs / fail-open)."""
import asyncio
import json
from datetime import datetime, timezone

import pytest

import config
from entry_engine import structure_gate as sg


def _summary(**kw):
    s = {"ms_quality": "ok", "ms_ver": "1.0.0", "ms_asof": datetime.now(timezone.utc).isoformat(), "ms_trend": "bullish",
         "ms_bos_dir": "bullish", "ms_bos_bars": 2, "ms_bos_failed": False, "ms_price": 100.0, "ms_atr": 2.0,
         "ms_chase_atr": 0.2, "ms_stretch_atr": 1.0, "ms_room_atr": 3.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": []}
    s.update(kw)
    return s


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for k, v in (("STRUCTURE_MAX_CHASE_ATR", 1.5), ("STRUCTURE_MAX_STRETCH_ATR", 3.0), ("STRUCTURE_MIN_ROOM_ATR", 0.5),
                 ("STRUCTURE_MIN_RR", 1.2), ("STRUCTURE_REJECT_STOP_WIDE", True), ("STRUCTURE_BEARISH_BLOCK", True),
                 ("STRUCTURE_BEARISH_BOS_BARS", 5), ("STRUCTURE_MAX_AGE_DAYS", 7.0), ("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0),
                 ("STRUCTURE_REQUIRE_DATA", False), ("STRUCTURE_STOP_MODE", "shadow"), ("STRUCTURE_STOP_MAX_RISK_MULT", 1.6),
                 ("STRUCTURE_STOP_MIN_RISK_MULT", 0.5), ("STRUCTURE_GATE_MODE", "shadow"), ("STRUCTURE_GATE_DEMO_MODE", "shadow")):
        monkeypatch.setattr(config, k, v, raising=False)
    sg.reset()


def test_clean_setup_has_opinion_and_no_reject():
    v = sg.evaluate(_summary(), 100.0)
    assert v["opinion"] == "ok" and not v["would_reject"] and v["reasons"] == []
    assert v["metrics"]["rr"] == 2.0


def test_chase_is_shifted_by_live_move():
    # structure close 100, live 104 (+2 ATR) on top of 0.2 chase -> 2.2 ATR
    v = sg.evaluate(_summary(), 104.0)
    assert v["would_reject"] and v["tag"] == "structure"
    assert v["metrics"]["chase_atr"] == pytest.approx(2.2)
    assert any("chase" in r for r in v["reasons"])


def test_stretch_room_rr_each_reject():
    assert any("stretched" in r for r in sg.evaluate(_summary(ms_stretch_atr=3.5), 100.0)["reasons"])
    assert any("room" in r for r in sg.evaluate(_summary(ms_room_atr=0.2), 100.0)["reasons"])
    assert any("R:R" in r for r in sg.evaluate(_summary(ms_target=102.0), 100.0)["reasons"])
    assert any("too wide" in r for r in sg.evaluate(_summary(ms_rr_flags=["stop_too_wide"]), 100.0)["reasons"])


def test_bearish_trend_and_fresh_bos():
    assert sg.evaluate(_summary(ms_trend="bearish"), 100.0)["would_reject"]
    assert sg.evaluate(_summary(ms_bos_dir="bearish", ms_bos_bars=3), 100.0)["would_reject"]
    assert not sg.evaluate(_summary(ms_bos_dir="bearish", ms_bos_bars=3, ms_bos_failed=True), 100.0)["would_reject"]
    assert not sg.evaluate(_summary(ms_bos_dir="bearish", ms_bos_bars=9), 100.0)["would_reject"]


@pytest.mark.parametrize("summary", [None, {}, {"ms_quality": "unavailable"}, {"ms_quality": "insufficient"}, {"ms_quality": "stale"}])
def test_no_data_is_no_opinion(summary):
    v = sg.evaluate(summary, 100.0)
    assert v["opinion"] == "no_opinion" and not v["would_reject"] and v["no_data"]


def test_stale_bar_and_price_deviation_are_no_opinion():
    assert sg.evaluate(_summary(ms_asof="2020-01-01T00:00:00+00:00"), 100.0)["opinion"] == "no_opinion"
    assert sg.evaluate(_summary(), 140.0)["opinion"] == "no_opinion"      # split / bad data
    assert sg.evaluate(_summary(ms_atr=None), 100.0)["opinion"] == "no_opinion"


def test_never_raises_on_garbage():
    v = sg.evaluate({"ms_quality": "ok", "ms_price": "x", "ms_atr": object()}, "abc")
    assert v["would_reject"] is False


def test_zero_threshold_disables_check(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_MAX_CHASE_ATR", 0.0)
    assert not sg.evaluate(_summary(ms_chase_atr=9.0), 100.0)["would_reject"]


def test_mode_for(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "enforce")
    monkeypatch.setattr(config, "STRUCTURE_GATE_DEMO_MODE", "shadow")
    assert sg.mode_for("REAL") == "enforce" and sg.mode_for("DEMO") == "shadow"


def test_extract_summary_variants():
    s = _summary()
    assert sg.extract_summary({"structure_summary": s}) == s
    assert sg.extract_summary(json.dumps({"structure_summary": s})) == s
    assert sg.extract_summary({"ms_quality": "ok", "ms_atr": 1.0})["ms_atr"] == 1.0
    assert sg.extract_summary("not json") is None and sg.extract_summary(None) is None


def test_structural_levels_sanity(monkeypatch):
    # ATR stop 97 (risk 3 at entry 100); structural stop 96 -> 1.33x -> sane, but shadow does not use it
    lv = sg.structural_levels(_summary(), 100.0, 97.0, 106.0)
    assert lv["sane"] and not lv["use"]
    monkeypatch.setattr(config, "STRUCTURE_STOP_MODE", "on")
    assert sg.structural_levels(_summary(), 100.0, 97.0, 106.0)["use"]
    assert not sg.structural_levels(_summary(ms_stop=90.0), 100.0, 97.0, 106.0)["sane"]      # 3.3x -> too wide
    assert not sg.structural_levels(_summary(ms_stop=98.9), 100.0, 97.0, 106.0)["sane"]      # 0.37x -> hair-thin
    assert not sg.structural_levels(_summary(ms_stop=101.0), 100.0, 97.0, 106.0)["sane"]     # above entry
    monkeypatch.setattr(config, "STRUCTURE_STOP_MODE", "off")
    assert sg.structural_levels(_summary(), 100.0, 97.0, 106.0)["stop"] is None


def test_snapshot_is_json_and_bounded():
    v = sg.evaluate(_summary(ms_trend="bearish"), 100.0)
    doc = json.loads(sg.snapshot(_summary(), v, "shadow", False, {"mode": "shadow"}, "payload"))
    assert doc["would_reject"] is True and doc["gate_mode"] == "shadow" and doc["summary"]["ms_trend"] == "bullish"


def test_get_summary_prefers_payload_then_gateway_failopen(monkeypatch):
    s = _summary()
    assert asyncio.run(sg.get_summary("ABC", {"structure_summary": s})) == (s, "payload")
    monkeypatch.setattr(config, "STRUCTURE_FETCH_ENABLED", False)
    assert asyncio.run(sg.get_summary("ABC", {})) == (None, None)
    monkeypatch.setattr(config, "STRUCTURE_FETCH_ENABLED", True)
    monkeypatch.setattr(config, "API_GATEWAY_URL", "http://127.0.0.1:9")       # refused -> fail open, cached
    assert asyncio.run(sg.get_summary("ABC", {})) == (None, None)
    assert "ABC" in sg._cache
