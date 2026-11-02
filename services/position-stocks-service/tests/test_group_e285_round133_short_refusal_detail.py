"""E285 round 133: the short gate counters also say how many DISTINCT symbols each code hit and why each shortlisted name was refused (orders/short_refusals.py). Pure."""
from pathlib import Path

from orders import short_refusals as sr

ROOT = Path(__file__).resolve().parents[1]


def test_distinct_symbols_not_attempts():
    sr.reset()
    for _ in range(50):
        sr.note("VWAP_ABOVE", "CLEAN", "price over VWAP")
    sr.note("VWAP_ABOVE", "TI", "price over VWAP")
    assert sr.snapshot()["symbols_by_code"] == {"VWAP_ABOVE": 2}


def test_book_full_and_short_cap_are_told_apart_by_reason():
    sr.reset()
    sr.note("SHORT_MAX_POSITIONS", "CLEAN", "MAX_POSITIONS:5")
    sr.note("SHORT_MAX_POSITIONS", "TI", "2 live shorts open (max 2)")
    snap = sr.snapshot()["last_refusal"]
    assert snap["CLEAN"]["reason"] == "MAX_POSITIONS:5" and snap["TI"]["reason"].startswith("2 live shorts")
    assert sr.snapshot()["symbols_by_code"] == {"SHORT_MAX_POSITIONS": 2}


def test_latest_reason_wins_and_order_is_newest_last():
    sr.reset()
    sr.note("A", "X", "1")
    sr.note("B", "Y", "2")
    sr.note("C", "X", "3")
    assert list(sr.snapshot()["last_refusal"]) == ["Y", "X"] and sr.why("X") == {"code": "C", "why": "3"}


def test_bounded():
    sr.reset()
    for i in range(sr.LAST_CAP + 25):
        sr.note("A", f"S{i}", "r")
    assert len(sr.snapshot()["last_refusal"]) == sr.LAST_CAP
    for i in range(sr.SYMS_CAP + 30):
        sr.note("B", f"T{i}", "r")
    assert sr.snapshot()["symbols_by_code"]["B"] == sr.SYMS_CAP


def test_long_reason_is_cut_and_bad_input_never_raises():
    sr.reset()
    sr.note("A", "X", "z" * 1000)
    assert len(sr.why("X")["why"]) == 200
    sr.note("A", None, "r")
    sr.note("A", "", "r")
    sr.note(None, "Q", None)
    assert sr.why("nope") == {}


def test_reset_clears():
    sr.reset()
    sr.note("A", "X", "r")
    sr.reset()
    assert sr.snapshot() == {"symbols_by_code": {}, "last_refusal": {}}


def test_short_entry_wiring():
    s = (ROOT / "orders" / "short_entry.py").read_text()
    assert "short_refusals.note(code, symbol, reason)" in s and "def _count(code: str, symbol" in s
    assert "_count(code, getattr(cand, \"symbol\", None), reason)" in s
    assert "**short_refusals.snapshot()" in s and "_r.update(short_refusals.why(c.symbol))" in s
    assert "short_refusals.reset()" in s
