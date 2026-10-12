"""group338 - Top picks cards (entry_engine/top_picks.py, GET /top-picks/{mode}) and the long-only Gate-6 rule (ENTRY_SHORTLIST_DROP_BEARISH).
Run from services/real-trade-service:  python3 -m pytest tests/test_group338_top_picks.py -q"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from entry_engine import shortlist, top_picks


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in dict(TOP_PICKS_N=5, TOP_PICKS_MIN_SHOWN=3, TOP_PICKS_FRESH_S=900.0, TOP_PICKS_MIN_CONVICTION=60.0,
                     ENTRY_SHORTLIST_ENABLED=True, ENTRY_SHORTLIST_MAX=5, ENTRY_MAX_NEW_PER_CYCLE=3, ENTRY_MIN_COMPOSITE_SCORE=50.0,
                     ENTRY_SHORTLIST_DROP_BEARISH=True).items():
        monkeypatch.setattr(config, k, v, raising=False)


def _row(sym, conv=80, zone=70, ltp=100.0, inv=98.0, rr=2.0, side="bull", **kw):
    d = dict(symbol=sym, name=sym, sector="Auto", side=side, conviction=conv, zone_score=zone, ltp=ltp, invalidation=inv, rr=rr,
             change_pct=1.5, rank=1, spark=[1, 2, 3], in_position=False)
    d.update(kw)
    return d


def test_engine_picks_keep_their_own_levels_and_get_board_fields():
    rows = {"E1": _row("E1"), "B1": _row("B1", conv=85), "B2": _row("B2", conv=75), "B3": _row("B3", conv=65)}
    snap = {"as_of": 1000.0, "picks": [{"pick": 1, "symbol": "E1", "status": "SELECTED", "outcome": "ENTERED", "composite_score": 71.5,
                                        "entry": 101.0, "stop": 97.0, "target": 109.0, "rr": 2.0, "board_bonus": 3.2, "is_upper_circuit": False}]}
    out = top_picks.build(snap, rows, 1100.0)
    c = out["picks"][0]
    assert (c["symbol"], c["source"], c["status"], c["outcome"], c["score"]) == ("E1", "engine", "SELECTED", "ENTERED", 71.5)
    assert (c["entry"], c["stop"], c["target"]) == (101.0, 97.0, 109.0)               # the engine's levels win over the board's
    assert c["ltp"] == 100.0 and c["sector"] == "Auto" and "board +3.2" in c["reasons"]
    assert [p["symbol"] for p in out["picks"]] == ["E1", "B1", "B2", "B3"] and out["preview_picks"] == 3


def test_stale_or_missing_snapshot_falls_back_to_board_preview():
    rows = {"B1": _row("B1", conv=85), "LOW": _row("LOW", conv=30), "BEAR": _row("BEAR", side="bear"), "HELD": _row("HELD", in_position=True)}
    out = top_picks.build({"as_of": 1.0, "picks": [{"symbol": "OLD"}]}, rows, 99999.0)
    assert [p["symbol"] for p in out["picks"]] == ["B1"] and out["engine_fresh"] is False
    assert top_picks.build({}, {}, 5.0)["picks"] == []
    assert top_picks.build(None, None, 5.0)["picks"] == []                            # never raises


def _e(sym, score, side=None, uc=False):
    return {"composite_score": score, "is_upper_circuit": uc, "board_side": side, "cand": sym}


def test_gate6_drops_board_bearish_but_keeps_upper_circuit_and_lone_picks(monkeypatch):
    out = shortlist.build([_e("BEAR", 90, "bear"), _e("OK", 60, "bull"), _e("NONE", 55)])
    assert [e["cand"] for e in out["shortlist"]] == ["OK", "NONE"]
    assert "BEAR" in [e["cand"] for e in out["skipped"]]
    out = shortlist.build([_e("BEARUC", 90, "bear", uc=True), _e("OK", 60)])
    assert [e["cand"] for e in out["shortlist"]] == ["BEARUC", "OK"]
    out = shortlist.build([_e("BEAR", 90, "bear")])
    assert [e["cand"] for e in out["shortlist"]] == ["BEAR"]                          # nothing else to enter -> unchanged behaviour
    monkeypatch.setattr(config, "ENTRY_SHORTLIST_DROP_BEARISH", False)
    assert [e["cand"] for e in shortlist.build([_e("BEAR", 90, "bear"), _e("OK", 60)])["shortlist"]] == ["BEAR", "OK"]
