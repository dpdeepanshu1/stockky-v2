"""group332 - best-3-to-5 shortlist (screening/shortlist.py) and the removal of the market-weak gates.
Run from services/position-stocks-service:  python3 -m pytest tests/test_group332_shortlist.py -q"""
from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from screening import shortlist, trade_gates


def C(sym, score):
    return SimpleNamespace(symbol=sym, composite_score=score)


def Q(f=None, t=None):
    return SimpleNamespace(fundamental_score=f, technical_score=t)


def B(sym, conv, rank=1, side="bull", zone=None, extended=False):
    return {"symbol": sym, "conviction": conv, "rank": rank, "side": side, "zone_score": zone, "extended": extended}


def _payload(*rows):
    return {"bullish": [r for r in rows if r["side"] == "bull"], "bearish": [r for r in rows if r["side"] == "bear"]}


def test_board_conviction_reorders_scan_rank():
    passers = [(C("A", 10), Q(60, 60)), (C("B", 9), Q(80, 80)), (C("C", 8), Q(50, 50))]
    out = shortlist.build(passers, _payload(B("B", 95, 1, zone=90), B("A", 40, 9, zone=20)))
    assert out[0]["symbol"] == "B" and [r["pick"] for r in out] == [1, 2, 3]


def test_max_five_and_never_padded():
    passers = [(C(s, 10 - i), Q(60, 60)) for i, s in enumerate("ABCDEFG")]
    assert len(shortlist.build(passers, None)) == 5
    assert len(shortlist.build(passers[:2], None)) == 2


def test_bearish_and_extended_are_penalised():
    passers = [(C("A", 10), Q(70, 70)), (C("B", 10), Q(70, 70)), (C("C", 10), Q(70, 70))]
    out = shortlist.build(passers, _payload(B("A", 80, 1, side="bear"), B("B", 80, 2, extended=True), B("C", 80, 3)))
    assert [r["symbol"] for r in out] == ["C", "B", "A"]


def test_missing_board_scores_on_remaining_parts():
    out = shortlist.build([(C("A", 10), Q(50, 50)), (C("B", 5), Q(90, 90))], None)
    assert {r["symbol"] for r in out} == {"A", "B"} and all(r["score"] is not None for r in out)


def test_ties_keep_scan_order():
    out = shortlist.build([(C("A", 10), Q(60, 60)), (C("B", 10), Q(60, 60))], None)
    assert [r["symbol"] for r in out] == ["A", "B"]


def test_error_fails_open_to_scan_order(monkeypatch):
    monkeypatch.setattr(shortlist, "score_one", lambda *a, **k: 1 / 0)
    out = shortlist.build([(C("A", 10), Q()), (C("B", 9), Q())], None)
    assert [r["symbol"] for r in out] == ["A", "B"] and out[0]["score"] is None


def test_public_strips_objects():
    out = shortlist.public(shortlist.build([(C("A", 10), Q(60, 60))], None))
    assert "cand" not in out[0] and "quality" not in out[0]


def test_bad_board_payload_is_ignored():
    assert shortlist.build([(C("A", 10), Q(60, 60))], "junk")[0]["symbol"] == "A"


# ── market-weak restriction removed ───────────────────────────────────────────────────────────────
def test_market_gate_off_by_default():
    assert config.MARKET_GATE_ENABLED is False and config.MARKET_GATE_PREV_CLOSE_ENABLED is False
    assert config.VIX_BLOCK_ABOVE == 0.0


def test_weak_nifty_does_not_block(monkeypatch):
    async def weak():
        return -3.0
    monkeypatch.setattr(trade_gates, "_fetch_nifty_change_pct", weak)
    trade_gates._market_cache.update(pct=None, ts=0.0, prev_pct=None)
    assert asyncio.run(trade_gates.market_gate_reject()) is None


def test_gate_can_still_be_switched_back_on(monkeypatch):
    async def weak():
        return -3.0
    monkeypatch.setattr(config, "MARKET_GATE_ENABLED", True)
    monkeypatch.setattr(trade_gates, "_fetch_nifty_change_pct", weak)
    trade_gates._market_cache.update(pct=None, ts=0.0, prev_pct=None)
    assert asyncio.run(trade_gates.market_gate_reject()).startswith("MARKET_WEAK")
