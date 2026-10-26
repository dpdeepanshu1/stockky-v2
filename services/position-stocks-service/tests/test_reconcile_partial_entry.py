"""E184: an entry that fills only PART of the order must not keep the ordered size, and a cancelled remainder must not turn a held
position into ERROR (shares owned at Dhan, nothing tracking or selling them)."""
from __future__ import annotations

import pytest

from tests.test_reconcile import (env, mkpos, super_row, plain_row, available, lock_held, LEDGER_AVAILABLE)  # noqa: F401
from orders import reconcile


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(reconcile, "_order_list_cache", (0.0, []))
    monkeypatch.setattr(reconcile, "_pending_kept_logged", set())
    monkeypatch.setattr(reconcile, "_partial_entry_alerted", set())
    monkeypatch.setattr(reconcile, "_dead_parent_part_logged", set())


def _part_row(filled, ordered=10, status="PART_TRADED", avg=100.0, **kw):
    return super_row("SO1", status=status, avg=avg, filledQty=filled, quantity=ordered,
                     target=("PENDING", 102.0), stop=("PENDING", 99.0), **kw)


def _alerts(sent):
    return sum("Partial entry fill" in m for m in sent["info"])


class TestQuantityFollowsTheFill:
    def test_part_fill_shrinks_quantity_and_capital_and_returns_the_rest_to_the_pool(self, env):
        db, b, sent = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [_part_row(6)]
        reconcile.run_exit_reconciliation(db)
        assert p.quantity == 6 and p.capital_risked == pytest.approx(600.0) and p.status == "OPEN"
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + 400.0)
        assert _alerts(sent) == 1

    def test_second_pass_changes_nothing_and_does_not_alert_again(self, env):
        db, b, sent = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [_part_row(6)]
        reconcile.run_exit_reconciliation(db)
        reconcile.run_exit_reconciliation(db)
        assert p.quantity == 6 and available(db) == pytest.approx(LEDGER_AVAILABLE + 400.0)
        assert _alerts(sent) == 1

    def test_later_fill_grows_the_position_up_to_the_order_size(self, env):
        db, b, sent = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [_part_row(6)]
        reconcile.run_exit_reconciliation(db)
        b.super_orders = [_part_row(9)]
        reconcile.run_exit_reconciliation(db)
        assert p.quantity == 9 and p.capital_risked == pytest.approx(900.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + 100.0)

    def test_filled_quantity_above_the_order_size_is_capped_at_the_order_size(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [_part_row(25, ordered=10)]
        reconcile.run_exit_reconciliation(db)
        assert p.quantity == 10 and available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_without_an_ordered_size_on_the_row_it_only_ever_shrinks(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        row = _part_row(25)
        del row["quantity"]
        b.super_orders = [row]
        reconcile.run_exit_reconciliation(db)
        assert p.quantity == 10

    def test_full_fill_changes_nothing_and_does_not_alert(self, env):
        db, b, sent = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [_part_row(10, status="TRADED")]
        reconcile.run_exit_reconciliation(db)
        assert p.quantity == 10 and available(db) == pytest.approx(LEDGER_AVAILABLE) and _alerts(sent) == 0

    def test_row_with_no_filled_quantity_changes_nothing(self, env):
        db, b, sent = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        row = _part_row(0, status="PENDING")
        b.super_orders = [row]
        reconcile.run_exit_reconciliation(db)
        assert p.quantity == 10 and _alerts(sent) == 0

    def test_filled_quantity_read_from_the_entry_leg_detail(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        row = super_row("SO1", status="PENDING", quantity=10, target=("PENDING", 102.0), stop=("PENDING", 99.0))
        row["legDetails"].append({"legName": "ENTRY_LEG", "orderStatus": "PART_TRADED", "filledQty": 4, "averageTradedPrice": 100.0})
        b.super_orders = [row]
        reconcile.run_exit_reconciliation(db)
        assert p.quantity == 4

    def test_capital_is_corrected_off_the_real_quantity_and_the_real_price(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [_part_row(6, avg=101.0)]
        reconcile.run_exit_reconciliation(db)
        assert p.quantity == 6 and p.entry_price == 101.0 and p.capital_risked == pytest.approx(606.0)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + 400.0 - 6.0)

    def test_a_stop_hit_after_a_part_fill_books_pnl_on_the_filled_shares(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="PART_TRADED", avg=100.0, filledQty=6, quantity=10,
                                    target=("PENDING", 102.0), stop=("TRADED", 99.0, 99.0))]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "STOP_HIT" and p.realized_pnl == pytest.approx(-6.0)

    def test_a_closed_position_is_never_resized(self, env):
        db, _, _ = env
        p = mkpos(db, status="STOP_HIT", entry=100.0, qty=10, super_id="SO1")
        assert reconcile._sync_entry_quantity(db, p, _part_row(6)) is False
        assert p.quantity == 10


class TestCancelledRemainderOfAPartFill:
    def test_cancelled_parent_with_shares_filled_stays_open_at_the_filled_quantity(self, env):
        db, b, sent = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="CANCELLED", avg=100.0, filledQty=4, quantity=10)]
        assert reconcile.run_exit_reconciliation(db) == 0
        assert p.status == "OPEN" and p.quantity == 4 and p.closed_at is None and p.error_message is None
        assert lock_held(db, p.symbol)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + 600.0)       # only the unfilled share of the reservation came back
        assert len(sent["critical"]) == 1 and p.symbol in sent["critical"][0] and "PARTIAL ENTRY" in sent["critical"][0]

    def test_it_alerts_once_not_every_pass(self, env):
        db, b, sent = env
        mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="CANCELLED", avg=100.0, filledQty=4, quantity=10)]
        for _ in range(3):
            reconcile.run_exit_reconciliation(db)
        assert len(sent["critical"]) == 1

    def test_cancelled_parent_with_nothing_filled_is_still_error_and_frees_the_capital(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="CANCELLED")]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "ERROR" and not lock_held(db, p.symbol)

    def test_rejected_parent_whose_buy_and_sell_traded_still_books_the_real_trade(self, env):
        db, b, _ = env
        p = mkpos(db, entry=107.17, qty=21, super_id="SO1")
        b.super_orders = [super_row("SO1", status="REJECTED", filledQty=21, quantity=21)]
        b.plain_orders = [
            plain_row("B1", avg=107.17, transactionType="BUY", securityId=p.dhan_security_id, quantity=21, filledQty=21,
                      createTime=p.opened_at.strftime("%Y-%m-%d %H:%M:%S")),
            plain_row("S1", avg=107.99, transactionType="SELL", securityId=p.dhan_security_id, quantity=21, filledQty=21),
        ]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status in ("STOP_HIT", "TARGET_HIT") and p.exit_price == pytest.approx(107.99)
