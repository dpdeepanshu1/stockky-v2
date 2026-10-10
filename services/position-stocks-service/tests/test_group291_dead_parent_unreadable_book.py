"""group291 (AAATECH follow-up). group 276 stopped a REJECTED super-order PARENT from booking a filled trade as ERROR by
looking in today's order book for the BUY. That lookup had two holes, both of which still turned a real trade into
"entry never happened" (ERROR, Rs0, capital and symbol lock released, no exit ever placed):

  1. the order book could not be READ at that moment (Dhan 403 "exceeding access rate" is routine) - an unreadable book
     was treated like an empty one;
  2. the book was served from the 20 s share and could predate the fill.

Now the dead-parent check reads the book fresh, and an unreadable book keeps the position OPEN for later passes (up to
DEAD_PARENT_BOOK_WAIT_S, default 180, 0 = old behaviour) before the old ERROR path runs.
"""
from __future__ import annotations

import time

import pytest

from tests.test_reconcile import (env, mkpos, super_row, plain_row, available, state, lock_held,  # noqa: F401
                            LEDGER_AVAILABLE)
from orders import reconcile


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(reconcile, "_order_list_cache", (0.0, []))
    monkeypatch.setattr(reconcile, "_pending_kept_logged", set())
    monkeypatch.setattr(reconcile, "_dead_parent_book_unreadable_since", {})
    monkeypatch.setattr(reconcile, "_dead_parent_defer_logged", set())
    monkeypatch.delenv("DEAD_PARENT_BOOK_WAIT_S", raising=False)
    monkeypatch.delenv("DEAD_PARENT_FILL_CHECK", raising=False)


def _buy(p, avg, qty=None, status="TRADED"):
    return plain_row("B1", status=status, avg=avg, transactionType="BUY", securityId=p.dhan_security_id,
                     quantity=qty or p.quantity, filledQty=qty or p.quantity,
                     createTime=p.opened_at.strftime("%Y-%m-%d %H:%M:%S"))


def _sell(p, avg, oid="S1"):
    return plain_row(oid, avg=avg, transactionType="SELL", securityId=p.dhan_security_id,
                     quantity=p.quantity, filledQty=p.quantity)


def _dead(db, b, **kw):
    p = mkpos(db, entry=107.17, qty=21, super_id="SO1", **kw)
    b.super_orders = [super_row("SO1", status="REJECTED")]
    return p


class TestOrderBookRead:
    def test_readable_book_returns_rows_and_true(self, env):
        db, b, _ = env
        b.plain_orders = [plain_row("X", avg=1.0)]
        rows, ok = reconcile._order_book_read(db)
        assert ok is True and [r["orderId"] for r in rows] == ["X"]

    def test_empty_but_readable_book_is_ok(self, env):
        db, b, _ = env
        assert reconcile._order_book_read(db) == ([], True)

    def test_failed_call_is_not_readable_and_not_cached(self, env):
        db, b, _ = env
        b.plain_error = RuntimeError("403 exceeding access rate")
        assert reconcile._order_book_read(db) == ([], False)
        b.plain_error = None
        b.plain_orders = [plain_row("X", avg=1.0)]
        assert reconcile._order_book_read(db)[1] is True

    def test_share_window_serves_the_cached_book(self, env):
        db, b, _ = env
        b.plain_orders = [plain_row("X", avg=1.0)]
        reconcile._order_book_read(db)
        b.plain_orders = [plain_row("Y", avg=1.0)]
        assert reconcile._order_book_read(db)[0][0]["orderId"] == "X"
        assert len(b.of("get_order_list")) == 1

    def test_force_bypasses_the_share(self, env):
        db, b, _ = env
        b.plain_orders = [plain_row("X", avg=1.0)]
        reconcile._order_book_read(db)
        b.plain_orders = [plain_row("Y", avg=1.0)]
        assert reconcile._order_book_read(db, force=True)[0][0]["orderId"] == "Y"

    def test_cached_order_list_keeps_its_old_contract(self, env):
        db, b, _ = env
        b.plain_error = RuntimeError("down")
        assert reconcile._cached_order_list(db) == []
        b.plain_error = None
        b.plain_orders = [plain_row("X", avg=1.0)]
        assert [r["orderId"] for r in reconcile._cached_order_list(db)] == ["X"]


class TestWaitSetting:
    @pytest.mark.parametrize("raw,want", [(None, 180.0), ("", 180.0), ("  ", 180.0), ("abc", 180.0), ("nan", 180.0),
                                           ("60", 60.0), ("0", 0.0), ("-5", 0.0)])
    def test_parse(self, monkeypatch, raw, want):
        if raw is None:
            monkeypatch.delenv("DEAD_PARENT_BOOK_WAIT_S", raising=False)
        else:
            monkeypatch.setenv("DEAD_PARENT_BOOK_WAIT_S", raw)
        assert reconcile._dead_parent_book_wait_s() == want


class TestUnreadableBookKeepsPositionOpen:
    def test_unreadable_book_does_not_book_error_or_release_anything(self, env):
        db, b, _ = env
        p = _dead(db, b)
        b.plain_error = RuntimeError("403 exceeding access rate")
        assert reconcile.run_exit_reconciliation(db) == 0
        assert p.status == "OPEN" and p.closed_at is None and p.error_message is None
        assert lock_held(db, p.symbol)
        assert available(db) == pytest.approx(LEDGER_AVAILABLE)

    def test_it_is_logged_once_not_every_pass(self, env, caplog):
        db, b, _ = env
        _dead(db, b)
        b.plain_error = RuntimeError("403")
        for _ in range(4):
            reconcile.run_exit_reconciliation(db)
        assert sum("could not be read" in r.getMessage() and "keeping the" in r.getMessage()
                   for r in caplog.records) == 1

    def test_next_pass_with_a_readable_book_books_the_real_trade(self, env):
        db, b, _ = env
        p = _dead(db, b)
        b.plain_error = RuntimeError("403")
        assert reconcile.run_exit_reconciliation(db) == 0
        b.plain_error = None
        b.plain_orders = [_buy(p, 107.17), _sell(p, 107.99)]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status in ("STOP_HIT", "TARGET_HIT") and p.exit_price == pytest.approx(107.99)
        assert p.realized_pnl == pytest.approx((107.99 - 107.17) * 21)
        assert p.id not in reconcile._dead_parent_book_unreadable_since

    def test_next_pass_with_a_buy_but_no_exit_stays_open(self, env):
        db, b, _ = env
        p = _dead(db, b)
        b.plain_error = RuntimeError("403")
        reconcile.run_exit_reconciliation(db)
        b.plain_error = None
        b.plain_orders = [_buy(p, 107.17)]
        assert reconcile.run_exit_reconciliation(db) == 0
        assert p.status == "OPEN"

    def test_next_pass_with_a_readable_book_that_proves_nothing_is_error(self, env):
        db, b, _ = env
        p = _dead(db, b)
        b.plain_error = RuntimeError("403")
        reconcile.run_exit_reconciliation(db)
        b.plain_error = None
        b.plain_orders = []
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "ERROR" and p.error_message.startswith("Entry leg REJECTED on Dhan (reconciled)")
        assert not lock_held(db, p.symbol)

    def test_after_the_wait_the_old_error_path_runs(self, env):
        db, b, _ = env
        p = _dead(db, b)
        b.plain_error = RuntimeError("403")
        reconcile.run_exit_reconciliation(db)
        reconcile._dead_parent_book_unreadable_since[p.id] = time.monotonic() - 181
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "ERROR"
        assert p.id not in reconcile._dead_parent_book_unreadable_since

    def test_wait_setting_is_honoured(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setenv("DEAD_PARENT_BOOK_WAIT_S", "30")
        p = _dead(db, b)
        b.plain_error = RuntimeError("403")
        reconcile.run_exit_reconciliation(db)
        reconcile._dead_parent_book_unreadable_since[p.id] = time.monotonic() - 31
        assert reconcile.run_exit_reconciliation(db) == 1 and p.status == "ERROR"

    def test_wait_zero_restores_the_old_behaviour(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setenv("DEAD_PARENT_BOOK_WAIT_S", "0")
        p = _dead(db, b)
        b.plain_error = RuntimeError("403")
        assert reconcile.run_exit_reconciliation(db) == 1 and p.status == "ERROR"

    def test_a_good_read_resets_the_clock(self, env):
        db, b, _ = env
        p = _dead(db, b)
        b.plain_error = RuntimeError("403")
        reconcile.run_exit_reconciliation(db)
        assert p.id in reconcile._dead_parent_book_unreadable_since
        reconcile._dead_parent_book_unreadable_since[p.id] = time.monotonic() - 5000
        b.plain_error = None
        b.plain_orders = [_buy(p, 107.17)]
        reconcile.run_exit_reconciliation(db)
        assert p.id not in reconcile._dead_parent_book_unreadable_since and p.status == "OPEN"

    def test_check_switch_off_still_means_no_lookup_at_all(self, env, monkeypatch):
        db, b, _ = env
        monkeypatch.setenv("DEAD_PARENT_FILL_CHECK", "0")
        p = _dead(db, b)
        b.plain_orders = [_buy(p, 107.17), _sell(p, 107.99)]
        assert reconcile.run_exit_reconciliation(db) == 1 and p.status == "ERROR"


class TestFreshBookBeatsStaleShare:
    def test_a_cached_book_from_before_the_fill_does_not_hide_the_buy(self, env):
        db, b, _ = env
        p = _dead(db, b)
        reconcile._order_list_cache = (time.monotonic(), [plain_row("OLD", avg=1.0, transactionType="BUY",
                                                                     securityId="999999")])
        b.plain_orders = [_buy(p, 107.17), _sell(p, 107.99)]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status in ("STOP_HIT", "TARGET_HIT") and p.realized_pnl == pytest.approx((107.99 - 107.17) * 21)

    def test_really_dead_entry_with_a_readable_book_is_still_error_at_once(self, env):
        db, b, _ = env
        p = _dead(db, b)
        b.plain_orders = [_buy(p, 107.17, status="REJECTED")]
        assert reconcile.run_exit_reconciliation(db) == 1 and p.status == "ERROR"
        assert not lock_held(db, p.symbol)
