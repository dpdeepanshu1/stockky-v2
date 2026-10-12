"""group338 - Top picks cards (screening/top_picks.py, GET /top-picks) and the long-only shortlist rule (SHORTLIST_DROP_BEARISH).
Run from services/position-stocks-service:  python3 -m pytest tests/test_group338_top_picks.py -q"""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from screening import shortlist, top_picks


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(TOP_PICKS_N=5, TOP_PICKS_MIN_SHOWN=3, TOP_PICKS_FRESH_S=900.0, TOP_PICKS_MIN_CONVICTION=60.0,
                     SHORTLIST_MAX=5, SHORTLIST_MIN_SCORE=0.0, SHORTLIST_DROP_BEARISH=True).items():
        monkeypatch.setattr(config, k, v, raising=False)


def _row(sym, conv=80, zone=70, ltp=100.0, inv=98.0, rr=2.0, side="bull", **kw):
    d = dict(symbol=sym, name=sym, sector="Auto", side=side, conviction=conv, zone_score=zone, ltp=ltp, invalidation=inv, rr=rr,
             change_pct=1.5, rank=1, spark=[1, 2, 3], in_position=False)
    d.update(kw)
    return d


def test_levels_from_row_derives_stop_and_target():
    lv = top_picks.levels_from_row(_row("A", ltp=100.0, inv=98.0, rr=2.0))
    assert lv == {"entry": 100.0, "stop": 98.0, "target": 104.0, "rr": 2.0}
    assert top_picks.levels_from_row(_row("A", inv=105.0))["stop"] is None        # a stop above price is not a long stop
    assert top_picks.levels_from_row(None)["entry"] is None


def test_preview_ranks_filters_and_caps():
    payload = {"bullish": [_row("LOW", conv=40), _row("MID", conv=70, zone=50), _row("TOP", conv=90, zone=90),
                           _row("EXT", conv=90, zone=90, extended=True), _row("HELD", conv=95, in_position=True),
                           dict(_row("NOPX", conv=99), ltp=None)]}
    out = top_picks.preview(payload, 3)
    assert [p["symbol"] for p in out] == ["TOP", "EXT", "MID"]                      # EXT 90-10 chase penalty = 80 beats MID 63; LOW below floor, HELD held, NOPX unpriced
    assert all(p["source"] == "board" and p["status"] == "PREVIEW" for p in out)
    assert top_picks.preview(payload, 3, exclude=["TOP"])[0]["symbol"] == "EXT"


def test_build_uses_fresh_engine_picks_then_tops_up_from_board():
    payload = {"bullish": [_row("E1"), _row("B1", conv=85), _row("B2", conv=75), _row("B3", conv=65)]}
    eng = [{"pick": 1, "symbol": "E1", "score": 77.0, "outcome": "ENTERED", "board_rank": 2, "board_conviction": 80}]
    out = top_picks.build(eng, 1000.0, payload, 1100.0)
    syms = [p["symbol"] for p in out["picks"]]
    assert syms[0] == "E1" and out["picks"][0]["source"] == "engine" and out["picks"][0]["outcome"] == "ENTERED"
    assert syms[1:] == ["B1", "B2", "B3"] and out["engine_picks"] == 1 and out["preview_picks"] == 3   # 1 < MIN_SHOWN -> filled up to N
    assert [p["pick"] for p in out["picks"]] == [1, 2, 3, 4]
    assert out["picks"][0]["entry"] == 100.0 and out["picks"][0]["target"] == 104.0


def test_build_ignores_a_stale_engine_shortlist():
    payload = {"bullish": [_row("B1")]}
    out = top_picks.build([{"pick": 1, "symbol": "OLD", "score": 90.0}], 1000.0, payload, 1000.0 + 5000)
    assert [p["symbol"] for p in out["picks"]] == ["B1"] and out["engine_fresh"] is False


def test_build_never_raises_on_garbage():
    assert top_picks.build(None, None, "nope", 0.0)["picks"] == []
    assert top_picks.build([{"symbol": None}], 1.0, {"bullish": [None, 3]}, 2.0)["picks"] == []


def _passer(sym, score):
    return SimpleNamespace(symbol=sym, composite_score=score), SimpleNamespace(fundamental_score=60, technical_score=60)


def test_shortlist_drops_board_bearish_when_a_long_remains(monkeypatch):
    passers = [_passer("BEAR", 100.0), _passer("OK", 50.0)]
    board = {"bearish": [{"symbol": "BEAR", "conviction": 90}], "bullish": []}
    assert [r["symbol"] for r in shortlist.build(passers, board)] == ["OK"]
    monkeypatch.setattr(config, "SHORTLIST_DROP_BEARISH", False)
    assert [r["symbol"] for r in shortlist.build(passers, board)][0] in ("BEAR", "OK")   # penalty only: both stay
    assert len(shortlist.build(passers, board)) == 2


def test_shortlist_keeps_a_lone_bearish_passer():
    out = shortlist.build([_passer("BEAR", 100.0)], {"bearish": [{"symbol": "BEAR", "conviction": 90}], "bullish": []})
    assert [r["symbol"] for r in out] == ["BEAR"]                                  # never an empty shortlist because of this rule
