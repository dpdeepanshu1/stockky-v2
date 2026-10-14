"""group354: daily market-structure gate on the position-stocks scalp entry (orders/structure_gate.py)."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timedelta, timezone

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


def test_clean_summary_passes():
    v = sg.evaluate(_sum(), 100.0)
    assert v["opinion"] == "ok" and v["would_reject"] is False


@pytest.mark.parametrize("kw,needle", [({"ms_trend": "bearish"}, "bearish"),
                                       ({"ms_chase_atr": 4.5}, "chase"),
                                       ({"ms_stretch_atr": 5.5}, "stretched"),
                                       ({"ms_room_atr": 0.1}, "room"),
                                       ({"ms_bos_dir": "bearish", "ms_bos_bars": 2, "ms_bos_failed": False}, "break of structure")])
def test_rejects(kw, needle):
    v = sg.evaluate(_sum(**kw), 100.0)
    assert v["would_reject"] is True and needle in v["reason"]


def test_failed_or_old_bearish_bos_is_not_a_reason():
    assert not sg.evaluate(_sum(ms_bos_dir="bearish", ms_bos_bars=2, ms_bos_failed=True), 100.0)["would_reject"]
    assert not sg.evaluate(_sum(ms_bos_dir="bearish", ms_bos_bars=9, ms_bos_failed=False), 100.0)["would_reject"]


def test_live_price_shifts_the_metrics():
    # price 4 above the structure close with ATR 2 -> +2 ATR of chase, 2 ATR less room
    v = sg.evaluate(_sum(ms_chase_atr=2.5, ms_room_atr=1.0), 104.0)
    assert v["metrics"]["chase_atr"] == pytest.approx(4.5) and v["metrics"]["room_atr"] == pytest.approx(-1.0) and v["would_reject"]


@pytest.mark.parametrize("summary", [None, {}, _sum(ms_quality="unavailable"), _sum(ms_quality="insufficient"),
                                     _sum(ms_asof=(datetime.now(timezone.utc) - timedelta(days=30)).isoformat()),
                                     _sum(ms_atr=None), _sum(ms_price=50.0, ms_trend="bearish")])
def test_no_data_is_no_opinion(summary):
    v = sg.evaluate(summary, 100.0)
    assert v["opinion"] == "no_opinion" and v["would_reject"] is False and v["no_data"]


def test_zero_limit_switches_a_test_off(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_MAX_CHASE_ATR", 0.0)
    assert not sg.evaluate(_sum(ms_chase_atr=9.0), 100.0)["would_reject"]


def test_check_enforce_returns_reason_and_snapshot(monkeypatch):
    monkeypatch.setattr(sg, "fetch_summary", lambda s: _sum(ms_trend="bearish"))
    reason, snap = sg.check("ABC", 100.0)
    assert reason.startswith("STRUCTURE_BAD_LOCATION:")
    doc = json.loads(snap)
    assert doc["enforced"] is True and doc["would_reject"] is True and doc["summary"]["ms_trend"] == "bearish"


def test_check_shadow_never_refuses_but_records(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "shadow")
    monkeypatch.setattr(sg, "fetch_summary", lambda s: _sum(ms_trend="bearish"))
    reason, snap = sg.check("ABC", 100.0)
    assert reason is None and json.loads(snap)["would_reject"] is True and json.loads(snap)["enforced"] is False


def test_check_off_and_no_data_and_errors_fail_open(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "off")
    assert sg.check("ABC", 100.0) == (None, None)
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "enforce")
    monkeypatch.setattr(sg, "fetch_summary", lambda s: None)
    reason, snap = sg.check("ABC", 100.0)
    assert reason is None and json.loads(snap)["opinion"] == "no_opinion"
    monkeypatch.setattr(config, "STRUCTURE_REQUIRE_DATA", True)
    assert sg.check("ABC", 100.0)[0].startswith("STRUCTURE_NODATA:")
    monkeypatch.setattr(config, "STRUCTURE_REQUIRE_DATA", False)

    def boom(s):
        raise RuntimeError("x")
    monkeypatch.setattr(sg, "fetch_summary", boom)
    assert sg.check("ABC", 100.0) == (None, None)


def test_structure_skip_is_symbol_specific():
    from orders import entry
    assert entry.is_symbol_specific_skip("STRUCTURE_BAD_LOCATION:daily structure is bearish")


def test_live_defaults_are_pinned():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.py"), encoding="utf-8").read()
    assert '_get_str(_ps("STRUCTURE_GATE_MODE"), "enforce")' in src and '_get_str("BOARD_GATE_MODE", "enforce")' in src
    assert '_get_bool("BOARD_USE_MS_ENGINE", True)' in src
