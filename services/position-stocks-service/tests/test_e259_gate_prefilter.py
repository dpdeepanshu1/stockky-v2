"""E259 - opening-gate pre-filter for the shortlist (screening/gate_prefilter.py) and the Top picks card fallback (screening/top_picks.py).
Run from services/position-stocks-service:  python3 -m pytest tests/test_e259_gate_prefilter.py -q"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from screening import gate_prefilter, opening_gate, top_picks


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(SHORTLIST_GATE_PREFILTER=True, TOP_PICKS_N=5, TOP_PICKS_MIN_SHOWN=3, TOP_PICKS_FRESH_S=900.0,
                     TOP_PICKS_MIN_CONVICTION=60.0, SHORTLIST_EXTENDED_PENALTY=10.0).items():
        monkeypatch.setattr(config, k, v, raising=False)


def _pair(sym, ltp=100.0):
    return SimpleNamespace(symbol=sym, current_ltp=ltp), SimpleNamespace(tag="q")


def _gate(monkeypatch, reasons, active=True):
    monkeypatch.setattr(opening_gate, "is_active", lambda now=None: active)
    monkeypatch.setattr(opening_gate, "reject_reason", lambda sym, ltp, now=None: reasons.get(sym))


def test_is_verdict_codes():
    assert gate_prefilter.is_verdict("OPENING_GATE:GAP_UP:+5.46% > +3.00%")
    assert gate_prefilter.is_verdict("OPENING_GATE:BELOW_OPEN:ltp 71 < open 75")
    assert gate_prefilter.is_verdict("OPENING_GATE:PREV_DAY_WEAK_CLOSE:close_pos=0.40")
    for code in ("NO_PREV_DAY_DATA", "NO_DAY_STATS", "NO_PRICE", "NO_OPEN_OR_PREV_CLOSE", "NO_OPENING_RANGE_DATA", "NO_ATR_DATA", "ERROR:ValueError", "TOO_EARLY:0.1m"):
        assert not gate_prefilter.is_verdict("OPENING_GATE:" + code), code
    assert not gate_prefilter.is_verdict(None)
    assert not gate_prefilter.is_verdict("SPIKE_CHASE:THRUST")


def test_gappers_are_held_and_others_keep_order(monkeypatch):
    _gate(monkeypatch, {"TRENT": "OPENING_GATE:GAP_UP:+5.46% > +3.00%", "ALIVUS": "OPENING_GATE:GAP_UP:+3.84% > +3.00%",
                        "PFOCUS": "OPENING_GATE:NO_PREV_DAY_DATA"})
    passers = [_pair(s) for s in ("TRENT", "ALIVUS", "PFOCUS", "ABC", "XYZ")]
    kept, held = gate_prefilter.partition(passers)
    assert [p[0].symbol for p in kept] == ["PFOCUS", "ABC", "XYZ"]          # data-not-ready stays, order unchanged
    assert [h["symbol"] for h in held] == ["TRENT", "ALIVUS"]
    assert held[0]["reason"].startswith("OPENING_GATE:GAP_UP")


def test_nothing_held_outside_window_or_when_off(monkeypatch):
    _gate(monkeypatch, {"TRENT": "OPENING_GATE:GAP_UP:+5.46% > +3.00%"}, active=False)
    kept, held = gate_prefilter.partition([_pair("TRENT")])
    assert len(kept) == 1 and held == []
    _gate(monkeypatch, {"TRENT": "OPENING_GATE:GAP_UP:+5.46% > +3.00%"}, active=True)
    monkeypatch.setattr(config, "SHORTLIST_GATE_PREFILTER", False)
    kept, held = gate_prefilter.partition([_pair("TRENT")])
    assert len(kept) == 1 and held == []


def test_errors_fail_open(monkeypatch):
    monkeypatch.setattr(opening_gate, "is_active", lambda now=None: True)

    def boom(sym, ltp, now=None):
        raise RuntimeError("x")
    monkeypatch.setattr(opening_gate, "reject_reason", boom)
    kept, held = gate_prefilter.partition([_pair("A"), _pair("B")])
    assert len(kept) == 2 and held == []
    assert gate_prefilter.partition([]) == ([], [])


def test_empty_when_all_held(monkeypatch):
    _gate(monkeypatch, {"A": "OPENING_GATE:GAP_DOWN:-3.77% < -2.00%"})
    kept, held = gate_prefilter.partition([_pair("A")])
    assert kept == [] and len(held) == 1


# ---------------- card fallback ----------------
VIEW = {"symbol": "TRENT", "sector": "Consumer", "ltp": 3021.6, "side": "bull", "day": {"ltp": 3021.6, "open": 3018.0, "prev_close": 2861.7},
        "candles": [[1, 1, 1, 1, 3000.0, 1], [2, 1, 1, 1, 3021.6, 1]], "zone_score": 55.0, "rr": {"rr": 2.0}, "extended": False,
        "invalidation_risk": {"invalidation": 2990.0, "risk_per_share": 31.6}, "position": None}


def test_row_from_symbol_view():
    r = top_picks.row_from_symbol_view(VIEW)
    assert r["ltp"] == 3021.6 and r["off_board"] is True and r["invalidation"] == 2990.0 and r["rr"] == 2.0
    assert r["change_pct"] == pytest.approx(5.59, abs=0.01)
    assert r["spark"] == [3000.0, 3021.6]
    assert top_picks.row_from_symbol_view(None) is None
    assert top_picks.row_from_symbol_view({"symbol": "X", "ltp": None}) is None
    assert top_picks.row_from_symbol_view("junk") is None


def test_card_gets_price_and_levels_from_fallback_and_shows_gate_text():
    rows = [{"symbol": "TRENT", "score": 81.9, "outcome": "SKIPPED:OPENING_GATE", "skip_detail": "OPENING_GATE:GAP_UP:+5.46% > +3.00%"}]
    seen = []

    def fb(sym):
        seen.append(sym)
        return top_picks.row_from_symbol_view(VIEW)
    built = top_picks.build(rows, 1000.0, {"bullish": [], "bearish": []}, 1010.0, fallback=fb)
    c = built["picks"][0]
    assert seen == ["TRENT"] and c["ltp"] == 3021.6 and c["entry"] == 3021.6 and c["stop"] == 2990.0
    assert c["target"] == pytest.approx(3021.6 + 2.0 * 31.6, abs=0.01)
    assert any(x.startswith("gate: OPENING_GATE:GAP_UP") for x in c["reasons"])
    assert any("not on the ranked board" in x for x in c["reasons"])


def test_fallback_not_used_when_board_has_row_and_errors_are_safe():
    rows = [{"symbol": "AAA", "score": 70.0, "outcome": "NOT_TRIED"}, {"symbol": "BBB", "score": 60.0, "outcome": "NOT_TRIED"}]
    payload = {"bullish": [{"symbol": "AAA", "ltp": 100.0, "conviction": 80, "invalidation": 98.0, "rr": 2.0}], "bearish": []}
    seen = []

    def fb(sym):
        seen.append(sym)
        raise RuntimeError("no bars")
    built = top_picks.build(rows, 1000.0, payload, 1010.0, fallback=fb)
    assert seen == ["BBB"]                                   # AAA already on the board
    assert built["picks"][0]["ltp"] == 100.0 and built["picks"][1]["ltp"] is None   # failing fallback = dashes, no crash
    assert top_picks.build(rows, 1000.0, payload, 1010.0)["picks"][1]["ltp"] is None  # no fallback = old behaviour
