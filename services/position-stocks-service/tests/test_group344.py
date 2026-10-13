"""group344 (13 Oct log review): quality gate blocks an unchecked entry when the event service fails, price floor runs before the quality gate,
exit-fill audit and one-time confirm in the closed-trade repair."""
import asyncio
import logging

import pytest

import config
from screening import quality_gate as qg
import os
import sys
sys.path.insert(0, os.path.dirname(__file__))
from test_group163_entry_precheck import TestInTheCycle as _Base163, _cand, m  # noqa: E402  (re-use its cycle scaffolding)
from test_group163_entry_precheck import db, _reset  # noqa: E402,F401  (fixtures)


# ------------------------------------------------------------------ quality gate: event service down
def _fakes(monkeypatch, *, event_ok, retry_ok=False, calls=None):
    async def _ft(client, symbol):
        return (60.0, 55.0, 5000.0)

    async def _ev(client, symbol, timeout=None):
        if calls is not None:
            calls.append(timeout)
        if event_ok or (timeout is not None and retry_ok):
            return True, 70.0, False
        qg._event_failed.add(symbol)
        return None, None, False
    monkeypatch.setattr(qg, "_fetch_fund_tech", _ft)
    monkeypatch.setattr(qg, "_fetch_event_signal", _ev)


@pytest.fixture()
def gate(monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_FAIL_BLOCK", True)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_RETRY_TIMEOUT_S", 6.0)
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_FAIL_OPEN_AFTER_S", 900.0)
    monkeypatch.setattr(config, "QUALITY_GATE_RESULT_TTL_S", 0.0)
    qg.reset_runtime_state() if hasattr(qg, "reset_runtime_state") else None
    qg._event_fail_since[0] = None
    qg._event_failed.clear()
    yield qg
    qg._event_fail_since[0] = None


def test_event_down_twice_blocks_the_entry(gate, monkeypatch):
    calls = []
    _fakes(monkeypatch, event_ok=False, calls=calls)
    sig = asyncio.run(gate.check("RAMRAT"))
    ok, reason = sig.passes()
    assert sig.event_unchecked and not ok and reason.startswith("EVENT_CHECK_UNAVAILABLE")
    assert 6.0 in calls                              # the retry used the longer timeout


def test_retry_that_works_clears_the_flag(gate, monkeypatch):
    _fakes(monkeypatch, event_ok=False, retry_ok=True)
    sig = asyncio.run(gate.check("RAMRAT"))
    assert not sig.event_unchecked and sig.passes()[0]


def test_event_up_is_unchanged(gate, monkeypatch):
    _fakes(monkeypatch, event_ok=True)
    sig = asyncio.run(gate.check("RAMRAT"))
    assert not sig.event_unchecked and sig.passes()[0]


def test_switch_off_restores_fail_open(gate, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_GATE_EVENT_FAIL_BLOCK", False)
    _fakes(monkeypatch, event_ok=False)
    sig = asyncio.run(gate.check("RAMRAT"))
    assert not sig.event_unchecked and sig.passes()[0]


def test_valve_fails_open_after_a_long_outage(gate, monkeypatch):
    _fakes(monkeypatch, event_ok=False)
    gate._event_fail_since[0] = gate._time.monotonic() - 1000.0
    sig = asyncio.run(gate.check("RAMRAT"))
    assert not sig.event_unchecked


def test_recovery_resets_the_outage_clock(gate, monkeypatch):
    _fakes(monkeypatch, event_ok=False)
    asyncio.run(gate.check("AAA"))
    assert gate._event_fail_since[0] is not None
    _fakes(monkeypatch, event_ok=True)
    asyncio.run(gate.check("BBB"))
    assert gate._event_fail_since[0] is None


# ------------------------------------------------------------------ price floor before the gate
class TestPriceFloorBeforeGate:
    _setup = _Base163._setup

    def test_cheap_candidate_never_reaches_the_quality_gate(self, db, monkeypatch):
        monkeypatch.setattr(config, "PRICE_FLOOR_BEFORE_GATE", True)
        monkeypatch.setattr(config, "MIN_STOCK_PRICE", 20.0)
        monkeypatch.setattr(config, "SHORTLIST_ENABLED", False)
        calls = self._setup(db, monkeypatch, [_cand("HATHWAY", 10.6), _cand("GOOD", 50.0)], avail=120.0)
        summary = asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert calls["check"] == ["GOOD"]
        assert summary["below_price_floor"] == ["HATHWAY"]

    def test_switch_off_keeps_the_old_behaviour(self, db, monkeypatch):
        monkeypatch.setattr(config, "PRICE_FLOOR_BEFORE_GATE", False)
        monkeypatch.setattr(config, "MIN_STOCK_PRICE", 20.0)
        monkeypatch.setattr(config, "SHORTLIST_ENABLED", False)
        calls = self._setup(db, monkeypatch, [_cand("HATHWAY", 10.6)], avail=120.0)
        asyncio.run(m._run_cycle(db, trigger="AUTO"))
        assert calls["check"] == ["HATHWAY"]


# ------------------------------------------------------------------ closed-trade exit audit
def _sell(avg, qty=100, sid="1"):
    return {"transactionType": "SELL", "securityId": sid, "orderStatus": "TRADED", "averageTradedPrice": avg, "filledQty": qty}


def _rec_env(monkeypatch):
    from orders import reconcile
    reconcile._order_list_cache = (0.0, [])
    reconcile._last_auto_repair_ts = 0.0
    reconcile._exit_fill_confirmed.clear()
    reconcile._exit_fill_logged.clear()
    return reconcile


def test_exit_audit_lists_confirmed_row_and_confirms_once(db, monkeypatch, caplog):
    from tests.test_repair_closed_entry_prices import _pos, _ledger
    from execution import dhan_client
    reconcile = _rec_env(monkeypatch)
    p = _pos(db, symbol="TECHM", entry=87.22, exit_=86.0, status="STOP_HIT", qty=100)
    _ledger(db)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [_sell(86.0)])
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: [{"orderId": "S1", "orderStatus": "PENDING"}])
    with caplog.at_level(logging.INFO):
        r1 = reconcile.repair_closed_entry_prices(db)
        r2 = reconcile.repair_closed_entry_prices(db)
    assert r1["exit_audit"] and r1["exit_audit"][0]["verdict"] == "booked exit is the order-book fill"
    assert r1["exit_audit"][0]["order_book_fill"] == 86.0
    assert r2["exit_audit"][0]["verdict"] == "confirmed earlier"
    assert sum("taken from today's order book" in x.message for x in caplog.records) <= 1


def test_exit_audit_flags_row_with_no_matching_sell(db, monkeypatch):
    from tests.test_repair_closed_entry_prices import _pos, _ledger
    from execution import dhan_client
    reconcile = _rec_env(monkeypatch)
    _pos(db, symbol="FSL", entry=87.22, exit_=86.0, status="TARGET_HIT", qty=100)
    _ledger(db)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [_sell(86.0, qty=7)])
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: [{"orderId": "S1", "orderStatus": "PENDING"}])
    r = reconcile.repair_closed_entry_prices(db)
    assert r["exit_audit"][0]["order_book_fill"] is None
    assert "could be the trigger price" in r["exit_audit"][0]["verdict"]


def test_confirm_once_can_be_switched_off(db, monkeypatch):
    from tests.test_repair_closed_entry_prices import _pos, _ledger
    from execution import dhan_client
    reconcile = _rec_env(monkeypatch)
    monkeypatch.setattr(config, "EXIT_FILL_CONFIRM_ONCE", False)
    _pos(db, symbol="NEWGEN", entry=87.22, exit_=86.0, status="STOP_HIT", qty=100)
    _ledger(db)
    monkeypatch.setattr(dhan_client, "get_order_list", lambda d: [_sell(86.0)])
    monkeypatch.setattr(dhan_client, "get_super_order_list", lambda d: [{"orderId": "S1", "orderStatus": "PENDING"}])
    reconcile.repair_closed_entry_prices(db)
    r2 = reconcile.repair_closed_entry_prices(db)
    assert r2["exit_audit"][0]["verdict"] == "booked exit is the order-book fill"
