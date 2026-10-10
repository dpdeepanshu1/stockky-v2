"""Group 276 (AAATECH 2026-10-09): a super-order PARENT row read REJECTED (the stop leg was rejected) while the
BUY and a SELL really traded. Reconcile must book the real trade instead of ERROR / Rs0."""
from __future__ import annotations

import pytest

from tests.test_reconcile import (env, mkpos, super_row, plain_row, available, state, lock_held,  # noqa: F401
                            LEDGER_AVAILABLE)
from orders import reconcile


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(reconcile, "_order_list_cache", (0.0, []))
    monkeypatch.setattr(reconcile, "_pending_kept_logged", set())


def _buy(p, avg, qty=None, status="TRADED"):
    return plain_row("B1", status=status, avg=avg, transactionType="BUY", securityId=p.dhan_security_id,
                     quantity=qty or p.quantity, filledQty=qty or p.quantity,
                     createTime=p.opened_at.strftime("%Y-%m-%d %H:%M:%S"))


def _sell(p, avg, oid="S1", qty=None):
    return plain_row(oid, avg=avg, transactionType="SELL", securityId=p.dhan_security_id,
                     quantity=qty or p.quantity, filledQty=qty or p.quantity)


class TestDeadParentButTraded:
    def test_buy_and_sell_traded_books_the_real_pnl(self, env):
        db, b, _ = env
        p = mkpos(db, entry=107.17, qty=21, super_id="SO1")
        b.super_orders = [super_row("SO1", status="REJECTED")]
        b.plain_orders = [_buy(p, 107.17), _sell(p, 107.99)]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status in ("STOP_HIT", "TARGET_HIT")
        assert p.exit_price == pytest.approx(107.99)
        assert p.realized_pnl == pytest.approx((107.99 - 107.17) * 21)
        assert p.error_message is None and not lock_held(db, p.symbol)
        assert state(db)["realized_pnl_today"] == pytest.approx((107.99 - 107.17) * 21)

    def test_buy_traded_but_no_exit_yet_stays_open_not_error(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="REJECTED")]
        b.plain_orders = [_buy(p, 100.0)]
        assert reconcile.run_exit_reconciliation(db) == 0
        assert p.status == "OPEN" and p.closed_at is None
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_really_dead_entry_is_still_error(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="REJECTED")]
        b.plain_orders = [_buy(p, 100.0, status="REJECTED")]
        assert reconcile.run_exit_reconciliation(db) == 1 and p.status == "ERROR"

    def test_wrong_quantity_buy_is_not_taken_as_proof(self, env):
        db, b, _ = env
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="REJECTED")]
        b.plain_orders = [_buy(p, 100.0, qty=5), _sell(p, 101.0, qty=5)]
        assert reconcile.run_exit_reconciliation(db) == 1 and p.status == "ERROR"

    def test_env_switch_restores_old_behaviour(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setenv("DEAD_PARENT_FILL_CHECK", "0")
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1")
        b.super_orders = [super_row("SO1", status="REJECTED")]
        b.plain_orders = [_buy(p, 100.0), _sell(p, 101.0)]
        assert reconcile.run_exit_reconciliation(db) == 1 and p.status == "ERROR"


class TestRepairDeadEntryErrors:
    def _error_row(self, db, p):
        p.status = "ERROR"
        p.error_message = "Entry leg REJECTED on Dhan (reconciled)"
        from datetime import datetime, timezone
        p.closed_at = datetime.now(timezone.utc)
        db.commit()

    def test_dry_run_lists_and_apply_books(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(reconcile, "_ist_date_str", lambda dt: "2026-09-21")
        p = mkpos(db, entry=107.17, qty=21, super_id="SO1", claim_lock=False)
        self._error_row(db, p)
        b.super_orders = [super_row("SO1", status="REJECTED")]
        b.plain_orders = [_buy(p, 107.17), _sell(p, 107.99)]
        dry = reconcile.repair_dead_entry_errors(db)
        assert len(dry["recovered"]) == 1 and p.status == "ERROR"
        out = reconcile.repair_dead_entry_errors(db, apply=True)
        assert out["recovered"][0]["pnl"] == pytest.approx(17.22, abs=0.01)
        assert p.status in ("STOP_HIT", "TARGET_HIT") and p.error_message is None

    def test_row_without_a_filled_pair_is_skipped(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setattr(reconcile, "_ist_date_str", lambda dt: "2026-09-21")
        p = mkpos(db, entry=100.0, qty=10, super_id="SO1", claim_lock=False)
        self._error_row(db, p)
        out = reconcile.repair_dead_entry_errors(db, apply=True)
        assert out["recovered"] == [] and len(out["skipped"]) == 1 and p.status == "ERROR"
