"""E285 round 169 (RATNAVEER, 3 Nov): a Super Order leg fires, Dhan makes its own VTT order and fills it, but the Super Order list does not read the leg
TRADED (or carries no fill price). The row stayed OPEN for 12 minutes, the app sent a second exit, and the exit was booked at the leg's static price.
Now reconcile books the exit from the filled VTT order in the plain order book, at its real fill price."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import config
from orders import leg_child_guard as L
from orders import reconcile
from tests.test_reconcile import (env, mkpos, super_row, available, lock_held, LEDGER_AVAILABLE)  # noqa: F401

_IST = timezone(timedelta(hours=5, minutes=30))


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(reconcile, "_order_list_cache", (0.0, []))
    monkeypatch.setattr(reconcile, "_pending_kept_logged", set())
    monkeypatch.setattr(reconcile, "_exit_fill_logged", set())
    monkeypatch.setattr(config, "RECONCILE_CHILD_FILL", True, raising=False)


def _now_ist(minutes_ago=0):
    return (datetime.now(_IST) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")


def _sell(p, oid, avg, tag="VTT_SQROFF", status="TRADED", qty=1, created=None, filled=None, side="SELL"):
    return {"orderId": oid, "transactionType": side, "orderStatus": status, "correlationId": tag, "securityId": p.dhan_security_id,
            "quantity": qty, "filledQty": qty if filled is None else filled, "averageTradedPrice": avg,
            "createTime": created or _now_ist(30)}


def _setup(env, **kw):
    db, b, sent = env
    p = mkpos(db, entry=382.90, qty=1, super_id="SO1", **kw)
    p.target_price, p.stop_price = 398.90, 384.00     # stop already trailed up
    db.commit()
    return db, b, sent, p


class TestBookedFromTheDhanMadeOrder:
    def test_triggered_leg_not_yet_traded_is_booked_from_the_vtt_fill(self, env):
        db, b, sent, p = _setup(env)
        b.super_orders = [super_row("SO1", avg=382.90, filledQty=1, target=("PENDING", 398.90), stop=("TRIGGERED", 381.85))]
        b.plain_orders = [_sell(p, "VTT1", 384.10)]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "STOP_HIT" and p.exit_price == pytest.approx(384.10)
        assert p.realized_pnl == pytest.approx(1.20)
        assert p.dhan_exit_order_id == "VTT1" and p.closed_at is not None
        assert available(db) == pytest.approx(LEDGER_AVAILABLE + p.capital_risked + 1.20)
        assert not lock_held(db, p.symbol)

    def test_no_triggered_leg_shown_kind_comes_from_the_nearer_level(self, env):
        db, b, sent, p = _setup(env)
        b.super_orders = [super_row("SO1", avg=382.90, filledQty=1, target=("PENDING", 398.90), stop=("PENDING", 381.85))]
        b.plain_orders = [_sell(p, "VTT1", 398.80)]
        reconcile.run_exit_reconciliation(db)
        assert p.status == "TARGET_HIT" and p.exit_price == pytest.approx(398.80)

    def test_ratnaveer_final_shape_uses_the_vtt_fill_not_the_static_leg_price(self, env):
        db, b, sent, p = _setup(env)
        # leg TRADED but with no fill price; two SELLs in the book (Dhan's VTT order and the app's later stagnation exit)
        b.super_orders = [super_row("SO1", avg=0.0, filledQty=1, target=("CANCELLED", 398.90), stop=("TRADED", 381.85))]
        b.plain_orders = [_sell(p, "VTT1", 384.10, created=_now_ist(40)), _sell(p, "STAG1", 381.60, tag="STAGNATION_EXIT", created=_now_ist(28))]
        assert reconcile.run_exit_reconciliation(db) == 1
        assert p.status == "STOP_HIT" and p.exit_price == pytest.approx(384.10)

    def test_switch_off_keeps_the_row_open(self, env, monkeypatch):
        db, b, sent, p = _setup(env)
        monkeypatch.setattr(config, "RECONCILE_CHILD_FILL", False)
        b.super_orders = [super_row("SO1", avg=382.90, filledQty=1, target=("PENDING", 398.90), stop=("TRIGGERED", 381.85))]
        b.plain_orders = [_sell(p, "VTT1", 384.10)]
        assert reconcile.run_exit_reconciliation(db) == 0 and p.status == "OPEN"

    def test_a_live_or_part_filled_vtt_order_books_nothing(self, env):
        db, b, sent, p = _setup(env)
        b.super_orders = [super_row("SO1", avg=382.90, filledQty=1, target=("PENDING", 398.90), stop=("TRIGGERED", 381.85))]
        b.plain_orders = [_sell(p, "VTT1", 0.0, status="PENDING", filled=0)]
        assert reconcile.run_exit_reconciliation(db) == 0 and p.status == "OPEN"

    def test_two_filled_vtt_orders_are_ambiguous_and_book_nothing(self, env):
        db, b, sent, p = _setup(env)
        b.super_orders = [super_row("SO1", avg=382.90, filledQty=1, target=("PENDING", 398.90), stop=("TRIGGERED", 381.85))]
        b.plain_orders = [_sell(p, "VTT1", 384.10), _sell(p, "VTT2", 384.20)]
        assert reconcile.run_exit_reconciliation(db) == 0 and p.status == "OPEN"

    def test_a_vtt_order_from_before_the_position_opened_is_ignored(self, env):
        db, b, sent, p = _setup(env)
        b.super_orders = [super_row("SO1", avg=382.90, filledQty=1, target=("PENDING", 398.90), stop=("TRIGGERED", 381.85))]
        b.plain_orders = [_sell(p, "VTT0", 384.10, created=_now_ist(60 * 6))]
        assert reconcile.run_exit_reconciliation(db) == 0 and p.status == "OPEN"

    def test_only_this_securitys_exit_side_orders_count(self, env):
        db, b, sent, p = _setup(env)
        b.super_orders = [super_row("SO1", avg=382.90, filledQty=1, target=("PENDING", 398.90), stop=("TRIGGERED", 381.85))]
        other = _sell(p, "VTT9", 384.10)
        other["securityId"] = "99999"
        b.plain_orders = [other, _sell(p, "VTT8", 384.10, side="BUY")]
        assert reconcile.run_exit_reconciliation(db) == 0 and p.status == "OPEN"

    def test_entry_not_filled_books_no_exit_from_a_vtt_order(self, env):
        db, b, sent, p = _setup(env)
        b.super_orders = [super_row("SO1", status="PENDING", target=("PENDING", 398.90), stop=("PENDING", 381.85))]
        b.plain_orders = [_sell(p, "VTT1", 384.10)]
        assert reconcile.run_exit_reconciliation(db) == 0 and p.status == "OPEN"


class TestPureHelper:
    P = SimpleNamespace(dhan_security_id="21", quantity=2, opened_at=datetime(2026, 11, 3, 4, 0, 0))

    def _r(self, **kw):
        r = {"orderId": "V1", "transactionType": "SELL", "orderStatus": "TRADED", "correlationId": "VTT_SQROFF", "securityId": "21",
             "filledQty": 2, "quantity": 2, "averageTradedPrice": 10.5, "createTime": "2026-11-03 11:00:00"}
        r.update(kw)
        return r

    def test_one_full_fill(self):
        assert L.filled_child_exit([self._r()], self.P, "SELL") == {"order_id": "V1", "price": 10.5}

    def test_none_cases(self):
        assert L.filled_child_exit([], self.P, "SELL") is None
        assert L.filled_child_exit([self._r(filledQty=1)], self.P, "SELL") is None          # part fill
        assert L.filled_child_exit([self._r(averageTradedPrice=0)], self.P, "SELL") is None  # no price
        assert L.filled_child_exit([self._r(correlationId="SCALP")], self.P, "SELL") is None # not Dhan-made
        assert L.filled_child_exit([self._r(), self._r(orderId="V2")], self.P, "SELL") is None  # two
        assert L.filled_child_exit([self._r()], self.P, "BUY") is None                       # wrong side
