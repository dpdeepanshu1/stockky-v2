"""Round 188: the conviction score shows how many inputs it was built from, and CONVICTION_MIN_KNOWN (default 2 = unchanged) can demand more.

Run from services/position-stocks-service:  python3 -m pytest tests/test_round188_conviction_known_inputs.py -q
"""
from __future__ import annotations

from types import SimpleNamespace

import config
from orders import conviction


def _long(board=90, rvol=None, tech=None, zone=None, catalyst=False):
    cand = SimpleNamespace(symbol="ABC", rvol=rvol)
    q = SimpleNamespace(technical_score=tech, has_positive_catalyst=catalyst, bulk_deal_flag=False)
    return cand, q


def _force(monkeypatch, board, zone=None):
    from screening import shortlist
    monkeypatch.setattr(config, "CONVICTION_SIZING_ENABLED", True, raising=False)   # conftest turns it off for older tests
    monkeypatch.setattr(shortlist, "recent", lambda s, side: {"board_conviction": board, "zone_score": zone})
    monkeypatch.setattr(conviction.rr_floor, "reward_risk", lambda levels: None)


def test_two_known_inputs_score_and_report_two_of_six(monkeypatch):
    _force(monkeypatch, 90)
    cand, q = _long(catalyst=True)
    d = conviction.long_decision(cand, q, None)
    assert d is not None and d["known"] == 2 and d["total"] == 6
    assert conviction.known_note(d) == "2 of 6 inputs known"
    assert d["mult"] > 1.0            # missing inputs are neutral (50 = the mid score), the two strong ones lift it


def test_unknown_inputs_never_lift_the_score(monkeypatch):
    _force(monkeypatch, 40)           # board known but weakest, catalyst known strongest
    cand, q = _long(catalyst=True)
    d = conviction.long_decision(cand, q, None)
    assert d["score"] == round((0 * 0.30 + 100 * 0.10 + 50 * 0.60) / 1.0, 1)


def test_default_min_known_is_two(monkeypatch):
    monkeypatch.setattr(config, "CONVICTION_MIN_KNOWN", 2, raising=False)
    _force(monkeypatch, 90)
    cand, q = _long(catalyst=True)
    assert conviction.long_decision(cand, q, None) is not None


def test_min_known_three_gives_no_opinion_on_two_inputs(monkeypatch):
    monkeypatch.setattr(config, "CONVICTION_MIN_KNOWN", 3, raising=False)
    _force(monkeypatch, 90)
    cand, q = _long(catalyst=True)
    assert conviction.long_decision(cand, q, None) is None       # None = the old fixed size


def test_bad_min_known_falls_back_to_two(monkeypatch):
    monkeypatch.setattr(config, "CONVICTION_MIN_KNOWN", "x", raising=False)
    assert conviction._min_known() == 2


def test_known_note_empty_without_decision():
    assert conviction.known_note(None) == ""
    assert conviction.note("ABC", "LONG", {"score": 100, "mult": 3.0}) == "conviction 100 -> x3.00 size"
