"""
Round 211: the NSE movers boards. 4 Nov log: gainers gave 20 rows but losers and volume-gainers gave 0 right after a weak-cookie bootstrap.
  * _nse_board_rows reads {"data": [...]} AND group-shaped boards ({"NIFTY": {"data": [...]}, "allSec": {...}}), one row per symbol.
  * _fetch_nse_board tries alternative paths ("losers" | "loosers", "volume-gainers" | "live-analysis-volume-gainers") and keeps the first with rows.

Run from services/api-gateway:
    python3 -m pytest tests/test_round211_nse_board_rows.py -q
"""
from __future__ import annotations
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as gw


def _r(sym, pct=3.0):
    return {"symbol": sym, "pChange": pct}


def test_flat_data_list():
    assert [r["symbol"] for r in gw._nse_board_rows({"data": [_r("A"), _r("B")]})] == ["A", "B"]


def test_group_shaped_board_merges_groups_one_row_per_symbol():
    board = {"NIFTY": {"data": [_r("A"), _r("B")], "timestamp": "t"}, "allSec": {"data": [_r("B"), _r("C")]}, "timestamp": "t", "legends": []}
    assert sorted(r["symbol"] for r in gw._nse_board_rows(board)) == ["A", "B", "C"]


def test_nifty_as_plain_list():
    assert [r["symbol"] for r in gw._nse_board_rows({"NIFTY": [_r("A")]})] == ["A"]


def test_junk_gives_empty_never_raises():
    for bad in (None, [], "x", 5, {}, {"data": None}, {"data": "x"}, {"a": 1, "b": {"c": 2}}, {"data": [1, "x", None]}):
        assert gw._nse_board_rows(bad) == []


def test_alternative_path_is_tried_when_the_first_has_no_rows(monkeypatch):
    calls = []

    def fake(ep, key, ttl=0):
        calls.append((ep, key))
        return {"data": []} if ep.endswith("losers") else {"data": [_r("X", -4.0)]}
    monkeypatch.setattr(gw, "_fetch_from_nse_api", fake)
    data, ep = gw._fetch_nse_board("live-analysis-variations?index=losers|live-analysis-variations?index=loosers", "nse:losers")
    assert ep.endswith("loosers") and [r["symbol"] for r in gw._nse_board_rows(data)] == ["X"]
    assert calls[0][1] == "nse:losers" and calls[1][1] == "nse:losers:alt1"


def test_first_path_with_rows_stops_the_search(monkeypatch):
    calls = []
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda ep, key, ttl=0: (calls.append(ep), {"data": [_r("A")]})[1])
    data, ep = gw._fetch_nse_board("a|b", "k")
    assert ep == "a" and calls == ["a"]


def test_nothing_with_rows_returns_the_first_answer_and_none_when_blocked(monkeypatch):
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda ep, key, ttl=0: {"data": [], "from": ep})
    data, ep = gw._fetch_nse_board("a|b", "k")
    assert ep == "a" and data["from"] == "a"
    monkeypatch.setattr(gw, "_fetch_from_nse_api", lambda ep, key, ttl=0: None)
    assert gw._fetch_nse_board("a|b", "k") == (None, "a")
