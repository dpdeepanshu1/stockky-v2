"""group366 (position-stocks): the room rejection shows the RAW (possibly negative) room and a counter separates breakout-like rejections.
Verdicts are unchanged; only the reason text and one counter are new."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

import config
from orders import structure_gate as sg


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_MIN_ROOM_ATR", 0.3, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_MAX_CHASE_ATR", 4.0, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_MAX_STRETCH_ATR", 5.0, raising=False)
    monkeypatch.setattr(config, "STRUCTURE_BEARISH_BLOCK", True, raising=False)
    sg.reset()
    yield
    sg.reset()


def _eval(room, live):
    s = {"ms_quality": "ok", "ms_asof": datetime.now(timezone.utc).isoformat(), "ms_price": 100.0, "ms_atr": 2.0, "ms_trend": "bullish",
         "ms_chase_atr": 0.5, "ms_stretch_atr": 1.0, "ms_room_atr": room}
    return sg.evaluate(s, live)


def test_true_no_room_keeps_old_text():
    v = _eval(0.1, 100.0)
    assert v["would_reject"] and "only 0.1 daily ATR of room to the next resistance (min 0.3)" in v["reason"]
    assert "raw" not in v["reason"]


def test_price_past_resistance_shows_raw_value():
    v = _eval(0.2, 102.0)          # d = 1.0 -> raw room = -0.8
    assert v["would_reject"] and v["metrics"]["room_atr"] == pytest.approx(-0.8)
    txt = [r for r in v["reasons"] if "of room" in r][0]
    assert txt.startswith("only 0.0 daily ATR of room to the next resistance (min 0.3)")
    assert "[raw -0.8: live price is 0.8 ATR beyond the cached resistance - possible breakout]" in txt
    assert sg.reason_key(txt) == "room"


def test_counter_counts_only_breakout_like_rejections():
    sg.reset_stats()
    sg.record("A", _eval(0.2, 102.0), "enforce", True)
    sg.record("B", _eval(0.1, 100.0), "enforce", True)
    sg.record("C", _eval(3.0, 100.0), "enforce", False)
    assert sg._stats["room_past_resistance"] == 1 and sg._stats["by_reason"]["room"] == 2
