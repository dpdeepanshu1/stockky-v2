"""E369: cancel_for_exit reads the stop back from Dhan before the software SELL; an INTRADAY (MIS) position never sells anyway."""
from __future__ import annotations

from types import SimpleNamespace

import config
from execution import broker_stop as B


class FakeDb:
    def commit(self): pass
    def rollback(self): pass


def setup(monkeypatch, *, cancel_raises=False, status="CANCELLED", stop=True):
    B.reset_state()
    row = SimpleNamespace(dhan_order_id="S1", status="PLACED") if stop else None
    monkeypatch.setattr(B, "open_stop", lambda db, sym: row)
    def fake_cancel(db, **kw):
        if cancel_raises:
            raise RuntimeError("cancel failed")
        return {}
    monkeypatch.setattr(B.dhan_client, "cancel_order", fake_cancel)
    monkeypatch.setattr(B, "_broker_status", lambda db, oid: status)
    monkeypatch.setattr(B.notifier, "notify_fire_and_forget", lambda *a, **k: None)
    return row


def pos(pt="INTRADAY"):
    return SimpleNamespace(symbol="ABC", entry_product_type=pt)


def test_no_stop_goes_ahead(monkeypatch):
    setup(monkeypatch, stop=False)
    assert B.cancel_for_exit(FakeDb(), pos()) is True


def test_cancel_confirmed_goes_ahead_and_marks_cancelled(monkeypatch):
    row = setup(monkeypatch, status="CANCELLED")
    assert B.cancel_for_exit(FakeDb(), pos()) is True and row.status == "CANCELLED"


def test_cancel_accepted_but_stop_traded_skips_sell_for_both_products(monkeypatch):
    for pt in ("INTRADAY", "CNC"):
        row = setup(monkeypatch, status="TRADED")
        assert B.cancel_for_exit(FakeDb(), pos(pt)) is False and row.status == "PLACED"


def test_mis_stop_still_live_after_cancel_waits(monkeypatch):
    row = setup(monkeypatch, status="PENDING")
    assert B.cancel_for_exit(FakeDb(), pos("MIS")) is False and row.status == "PLACED"


def test_cnc_stop_still_pending_after_cancel_goes_ahead(monkeypatch):
    setup(monkeypatch, status="PENDING")
    assert B.cancel_for_exit(FakeDb(), pos("CNC")) is True


def test_mis_unreadable_book_trusts_the_accepted_cancel(monkeypatch):
    row = setup(monkeypatch, status=None)
    assert B.cancel_for_exit(FakeDb(), pos()) is True and row.status == "CANCELLED"


def test_mis_cancel_failing_never_sells_anyway(monkeypatch):
    monkeypatch.setattr(config, "BROKER_STOP_CANCEL_MAX_FAILS", 3, raising=False)
    setup(monkeypatch, cancel_raises=True, status=None)
    assert [B.cancel_for_exit(FakeDb(), pos()) for _ in range(6)] == [False] * 6


def test_cnc_cancel_failing_sells_after_max_fails(monkeypatch):
    monkeypatch.setattr(config, "BROKER_STOP_CANCEL_MAX_FAILS", 3, raising=False)
    setup(monkeypatch, cancel_raises=True, status=None)
    assert [B.cancel_for_exit(FakeDb(), pos("CNC")) for _ in range(3)] == [False, False, True]


def test_failed_cancel_then_dead_in_book_goes_ahead(monkeypatch):
    row = setup(monkeypatch, cancel_raises=True, status="CANCELLED")
    assert B.cancel_for_exit(FakeDb(), pos()) is True and row.status == "CANCELLED"


def test_error_with_stop_known_blocks_mis_but_not_cnc(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(B, "_broker_status", lambda db, oid: (_ for _ in ()).throw(RuntimeError("boom")))
    assert B.cancel_for_exit(FakeDb(), pos("INTRADAY")) is False
    assert B.cancel_for_exit(FakeDb(), pos("CNC")) is True


def test_error_reading_the_stop_itself_lets_the_sell_go(monkeypatch):
    setup(monkeypatch)
    monkeypatch.setattr(B, "open_stop", lambda db, sym: (_ for _ in ()).throw(RuntimeError("db down")))
    assert B.cancel_for_exit(FakeDb(), pos("INTRADAY")) is True
