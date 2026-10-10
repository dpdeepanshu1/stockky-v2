"""Group 287: reconcile cross-checks a dead (REJECTED) parent against the order-update WebSocket events.
Default: only a WS_CROSSCHECK warning, outcome unchanged. RECONCILE_USE_ORDER_EVENTS=1: the row stays OPEN, not ERROR."""
from __future__ import annotations

import logging
import time

import pytest

import config
from execution import order_ws
from orders import reconcile
from tests.test_reconcile import (env, mkpos, super_row, available, lock_held,  # noqa: F401
                            LEDGER_AVAILABLE)


def ev(symbol="ABC", txn="B", status="TRADED", traded=10, avg=100.0, oid="W1", at=None):
    return {"order_id": oid, "status": status, "symbol": symbol, "txn": txn, "traded_qty": traded,
            "avg_traded_price": avg, "traded_price": avg, "received_at": time.time() if at is None else at}


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    monkeypatch.setattr(reconcile, "_order_list_cache", (0.0, []))
    monkeypatch.setattr(reconcile, "_pending_kept_logged", set())
    monkeypatch.setattr(reconcile, "_ws_xcheck_logged", set())
    s = order_ws.OrderEventStore()
    monkeypatch.setattr(order_ws, "store", s)
    return s


class TestStoreLookup:
    def test_finds_traded_buy_with_exact_qty(self, fresh):
        fresh.add(ev())
        assert fresh.find_entry_fill("abc", 10, 0)["avg_traded_price"] == 100.0

    def test_eq_suffix_is_ignored(self, fresh):
        fresh.add(ev(symbol="ABC-EQ"))
        assert fresh.find_entry_fill("ABC", 10, 0) is not None

    @pytest.mark.parametrize("kw", [{"txn": "S"}, {"status": "PENDING"}, {"status": "PART_TRADED"}, {"traded": 9},
                                    {"symbol": "OTHER"}])
    def test_other_events_do_not_match(self, fresh, kw):
        fresh.add(ev(**kw))
        assert fresh.find_entry_fill("ABC", 10, 0) is None

    def test_older_than_since_does_not_match(self, fresh):
        fresh.add(ev(at=100.0))
        assert fresh.find_entry_fill("ABC", 10, 200.0) is None

    def test_latest_wins(self, fresh):
        fresh.add(ev(oid="A", avg=100.0, at=1.0))
        fresh.add(ev(oid="B", avg=101.0, at=2.0))
        assert fresh.find_entry_fill("ABC", 10, 0)["avg_traded_price"] == 101.0


class TestHelper:
    def test_no_opened_at_returns_none(self):
        class P: symbol = "ABC"; quantity = 10; opened_at = None
        assert reconcile._entry_fill_from_order_events(P()) is None

    def test_exception_returns_none(self, monkeypatch):
        monkeypatch.setattr(order_ws.store, "find_entry_fill", lambda *a: 1 / 0)
        class P: symbol = "ABC"; quantity = 10; opened_at = __import__("datetime").datetime(2026, 1, 1)
        assert reconcile._entry_fill_from_order_events(P()) is None


class TestReconcile:
    def _dead(self, db, b, sym_events, store):
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="REJECTED")]
        b.plain_orders = []                      # the order book proves nothing
        for e in sym_events:
            store.add({**e, "symbol": p.symbol})
        return p

    def test_flag_off_outcome_unchanged_but_disagreement_logged(self, env, fresh, monkeypatch, caplog):
        db, b, _ = env
        monkeypatch.setattr(config, "RECONCILE_USE_ORDER_EVENTS", False)
        p = self._dead(db, b, [ev()], fresh)
        with caplog.at_level(logging.WARNING):
            reconcile.run_exit_reconciliation(db)
        assert p.status == "ERROR"
        assert any("WS_CROSSCHECK" in r.message for r in caplog.records)

    def test_flag_on_keeps_the_row_open(self, env, fresh, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "RECONCILE_USE_ORDER_EVENTS", True)
        p = self._dead(db, b, [ev()], fresh)
        assert reconcile.run_exit_reconciliation(db) == 0
        assert p.status == "OPEN" and p.closed_at is None
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_flag_on_without_matching_event_still_errors(self, env, fresh, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "RECONCILE_USE_ORDER_EVENTS", True)
        p = self._dead(db, b, [ev(status="REJECTED")], fresh)
        reconcile.run_exit_reconciliation(db)
        assert p.status == "ERROR"

    def test_flag_on_wrong_quantity_still_errors(self, env, fresh, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "RECONCILE_USE_ORDER_EVENTS", True)
        p = self._dead(db, b, [ev(traded=3)], fresh)
        reconcile.run_exit_reconciliation(db)
        assert p.status == "ERROR"

    def test_order_book_proof_still_wins_over_events(self, env, fresh, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(config, "RECONCILE_USE_ORDER_EVENTS", False)
        from tests.test_reconcile_dead_parent_fill import _buy
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="REJECTED")]
        b.plain_orders = [_buy(p, 100.0)]
        reconcile.run_exit_reconciliation(db)
        assert p.status == "OPEN"
