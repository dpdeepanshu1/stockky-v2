"""group366: the room rejection now shows the RAW (possibly negative) room, and a counter tells breakout-like rejections apart.
Behaviour is unchanged: the same inputs are rejected / accepted as before; only the reason text and one counter are new."""
import pytest

from entry_engine import structure_gate as sg

BASE = {"ms_quality": "ok", "ms_price": 100.0, "ms_atr": 2.0, "ms_trend": "bullish", "ms_chase_atr": 0.4, "ms_stretch_atr": 1.0,
        "ms_room_atr": 3.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": []}


def _eval(room, live):
    return sg.evaluate({**BASE, "ms_room_atr": room}, live)


def test_true_no_room_keeps_exact_old_text():
    v = _eval(0.2, 100.0)
    assert v["would_reject"]
    assert "only 0.2 ATR of room to the next resistance (min 0.5)" in v["reason"]
    assert "raw" not in v["reason"]


def test_price_past_resistance_shows_raw_negative_value():
    v = _eval(0.4, 102.0)          # live 102 vs snapshot 100, ATR 2 -> d = 1.0 -> raw room = -0.6
    assert v["would_reject"] and v["metrics"]["room_atr"] == pytest.approx(-0.6)
    txt = [r for r in v["reasons"] if "of room" in r][0]
    assert txt.startswith("only 0.0 ATR of room to the next resistance (min 0.5)")
    assert "[raw -0.6: live price is 0.6 ATR beyond the cached resistance - possible breakout]" in txt


def test_reason_still_classified_as_room():
    v = _eval(0.4, 102.0)
    assert sg.reason_key([r for r in v["reasons"] if "of room" in r][0]) == "room"


def test_verdict_unchanged_by_diagnostic():
    assert not _eval(3.0, 100.0)["would_reject"]
    assert _eval(0.4, 102.0)["would_reject"] and _eval(0.2, 100.0)["would_reject"]


def test_counter_counts_only_breakout_like_room_rejections():
    sg.reset_stats()
    sg.record("A", "REAL", _eval(0.4, 102.0), "enforce", True)    # raw -0.6 -> counted
    sg.record("B", "REAL", _eval(0.2, 100.0), "enforce", True)    # genuine 0.2 room -> not counted
    sg.record("C", "REAL", _eval(3.0, 100.0), "enforce", False)   # accepted -> not counted
    assert sg._stats["room_past_resistance"] == 1 and sg._stats["by_reason"]["room"] == 2
    sg.reset_stats()
    assert sg._stats["room_past_resistance"] == 0
