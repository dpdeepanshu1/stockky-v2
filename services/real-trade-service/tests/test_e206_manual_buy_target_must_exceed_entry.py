"""E206: a manual BUY whose typed target is at or below the entry is rejected (same class as the stop-below-entry check)."""
from __future__ import annotations

import pytest

import manual_engine as me
from tests.test_manual_engine import Req, _approve, _quotes, db, run  # noqa: F401


@pytest.mark.parametrize("target", [100.0, 99.0, 50.0])
def test_typed_target_at_or_below_entry_is_rejected(db, monkeypatch, target):
    _quotes(monkeypatch, price=100.0)
    _approve(monkeypatch)
    r = run(me.evaluate_manual_order(db, "DEMO", True, Req(stop_price=95.0, target_price=target), confirm=False))
    assert r["ok"] is False and r["reason"] == "invalid_target"


def test_limit_price_is_the_reference_for_the_target_check(db, monkeypatch):
    _quotes(monkeypatch, price=100.0)
    _approve(monkeypatch)
    r = run(me.evaluate_manual_order(db, "DEMO", True,
                                     Req(limit_price=110.0, stop_price=105.0, target_price=108.0), confirm=False))
    assert r["ok"] is False and r["reason"] == "invalid_target"       # 108 is above the market but below the 110 entry


def test_target_above_entry_and_no_target_still_pass(db, monkeypatch):
    _quotes(monkeypatch, price=100.0)
    _approve(monkeypatch)
    a = run(me.evaluate_manual_order(db, "DEMO", True, Req(stop_price=95.0, target_price=106.0), confirm=False))
    b = run(me.evaluate_manual_order(db, "DEMO", True, Req(), confirm=False))        # fallback target is above entry
    assert a["ok"] is True and b["ok"] is True


def test_swing_caller_is_not_affected(db, monkeypatch):
    _quotes(monkeypatch, price=100.0)
    _approve(monkeypatch)
    r = run(me.evaluate_manual_order(db, "DEMO", True, Req(stop_price=95.0, target_price=90.0), confirm=False,
                                     swing={"max_trade_value": 50_000}))
    assert r.get("reason") != "invalid_target"
