"""
tests/test_rt_reconcile.py — offline tests for execution/reconcile.py (real-trade-service).

This is "the only place a REAL fill becomes real in this service's own DB": it asks
Dhan whether each PLACED/PARTIAL order filled, then books the position, cash and
P&L. The tests run the REAL portfolio accounting (record_real_fill /
record_real_exit_fill) on in-memory SQLite, with a scripted broker order book, so
a wrong price, a double-booked fill or a stuck PENDING_EXIT shows up as a wrong
number in the DB, not as a mock assertion.

Sections:
  TestGet                    defensive key lookup
  TestBookFillDelta          one confirmed increment: BUY / SELL, stop+target fallbacks
  TestExitFailureAlerts      streak counter + operator alert cadence
  TestOrphanRepair           PENDING_EXIT positions with no live SELL
  TestReconcileFlow          the full pass: statuses, partials, dead orders, mismatch alert
  TestAuditRegressions       bugs found while writing these (fixed in the same audit)
  TestKnownGaps              strict xfail — pinned, needs a design decision

Run from services/real-trade-service:
    python3 -m pytest tests/test_rt_reconcile.py -q --cov=execution --cov-report=term-missing
"""
from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from execution import dhan_client
from execution import reconcile as R
from portfolio import portfolio as P

START_CASH = 100_000.0
RESTRICTED_MSG = "Order rejected as this stock is not allowed to be traded in Intraday."


class Rig:
    def __init__(self):
        self.book: list[dict] = []
        self.book_error: BaseException | None = None
        self.book_calls = 0
        self.imported = 0
        self.import_error: BaseException | None = None
        self.ghost = {"closed": 0, "symbols": []}
        self.ghost_error: BaseException | None = None
        self.sent: list[str] = []


@pytest.fixture()
def env(monkeypatch):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    rig = Rig()

    async def notify(text):
        rig.sent.append(text)

    async def import_holdings(db_):
        if rig.import_error:
            raise rig.import_error
        return rig.imported

    def ghost_sync(db_):
        if rig.ghost_error:
            raise rig.ghost_error
        return rig.ghost

    def order_list(db_):
        rig.book_calls += 1
        if rig.book_error:
            raise rig.book_error
        return list(rig.book)

    monkeypatch.setattr(R, "notify_async", notify)
    monkeypatch.setattr(R, "import_broker_holdings", import_holdings)
    monkeypatch.setattr(R, "holdings_sync_reconcile", ghost_sync)
    monkeypatch.setattr(dhan_client, "get_order_list", order_list)
    for k, v in dict(EXIT_RETRY_BASE_COOLDOWN_SECONDS=60, EXIT_RETRY_MAX_COOLDOWN_SECONDS=900,
                     EXIT_RETRY_ALERT_THRESHOLD=5).items():
        monkeypatch.setattr(config, k, v)

    db.add(models.TradeAccount(mode="REAL", starting_capital=START_CASH, current_equity=START_CASH,
                               cash_available=START_CASH, broker_cash_available=START_CASH,
                               realized_pnl_today=0.0, realized_pnl_total=0.0))
    db.commit()
    yield db, rig
    db.close()


def run(coro):
    return asyncio.run(coro)


# ── builders / readers ───────────────────────────────────────────────────────
def mk_decision(db, symbol="ABC", stop=98.0, target=104.0):
    d = models.TradeDecision(mode="REAL", symbol=symbol, decision_type="ENTRY", action="BUY",
                             proposed_stop=stop, proposed_target=target)
    db.add(d)
    db.commit()
    return d


def mk_order(db, side="BUY", symbol="ABC", qty=10, status="PLACED", dhan_id="O1", order_type="MARKET",
             decision=True, exit_reason=None, mode="REAL", filled_so_far=0, **kw):
    dec_id = None
    if side == "BUY" and decision is True:
        dec_id = mk_decision(db, symbol).id
    elif isinstance(decision, models.TradeDecision):
        dec_id = decision.id
    o = models.TradeOrder(mode=mode, decision_id=dec_id, symbol=symbol, side=side, qty=qty,
                          order_type=order_type, status=status, dhan_order_id=dhan_id,
                          exit_reason=exit_reason, filled_qty_so_far=filled_so_far, **kw)
    db.add(o)
    db.commit()
    return o


def mk_pos(db, symbol="ABC", status="OPEN", qty=10, avg=100.0, realized=0.0, **kw):
    p = models.TradePosition(mode="REAL", symbol=symbol, status=status, qty_open=qty, avg_entry_price=avg,
                             realized_pnl=realized, current_stop=avg * 0.98, current_target=avg * 1.04,
                             opened_at=datetime.now(timezone.utc), **kw)
    db.add(p)
    db.commit()
    return p


def row(oid="O1", status="TRADED", avg=100.0, filled=10, otype="MARKET", **kw):
    r = {"orderId": oid, "orderStatus": status, "orderType": otype}
    if avg is not None:
        r["averageTradedPrice"] = avg
    if filled is not None:
        r["filledQty"] = filled
    r.update(kw)
    return r


def cash(db):
    return P.get_account(db, "REAL").cash_available


def positions(db, symbol=None):
    q = db.query(models.TradePosition)
    return q.filter_by(symbol=symbol).all() if symbol else q.all()


def events(db, model, **flt):
    return db.query(model).filter_by(**flt).all()


# ── _get ─────────────────────────────────────────────────────────────────────
class TestGet:
    def test_first_present_key_wins(self):
        assert R._get({"b": 2, "a": 1}, "a", "b") == 1

    def test_none_and_empty_string_are_skipped(self):
        assert R._get({"a": None, "b": "", "c": 3}, "a", "b", "c") == 3

    def test_zero_is_a_real_value(self):
        assert R._get({"a": 0, "b": 5}, "a", "b") == 0

    def test_default_when_nothing_matches(self):
        assert R._get({}, "a", default="x") == "x" and R._get({}, "a") is None


# ── _book_fill_delta ─────────────────────────────────────────────────────────
class TestBookFillDelta:
    def test_buy_uses_the_decisions_stop_and_target(self, env):
        db, rig = env
        o = mk_order(db)
        run(R._book_fill_delta(db, o, 100.0, 10, is_partial=False))
        (p,) = positions(db)
        assert (p.status, p.qty_open, p.avg_entry_price, p.current_stop, p.current_target) == ("OPEN", 10, 100.0, 98.0, 104.0)
        assert o.filled_qty_so_far == 10 and o.status == "FILLED"
        assert cash(db) == pytest.approx(START_CASH - 1_000.0)
        assert rig.sent == ["✅ *BUY filled* — ABC\n10 shares @ ₹100.00 (₹1,000.00)\nStop ₹98.00 · Target ₹104.00"]

    def test_partial_buy_stays_partial_and_says_so(self, env):
        db, rig = env
        o = mk_order(db)
        run(R._book_fill_delta(db, o, 100.0, 4, is_partial=True))
        assert o.status == "PARTIAL" and o.filled_qty_so_far == 4 and positions(db)[0].qty_open == 4
        assert "BUY partial fill" in rig.sent[0] and "Order 4/10 filled" in rig.sent[0]

    def test_buy_without_a_decision_falls_back_to_the_flat_percentages(self, env):
        from entry_engine.entry import FLAT_STOP_PCT, FLAT_TARGET_PCT
        db, _ = env
        o = mk_order(db, decision=False)
        run(R._book_fill_delta(db, o, 200.0, 5, is_partial=False))
        p = positions(db)[0]
        assert p.current_stop == round(200.0 * (1 - FLAT_STOP_PCT / 100), 2)
        assert p.current_target == round(200.0 * (1 + FLAT_TARGET_PCT / 100), 2)

    def test_missing_stop_or_target_on_the_decision_is_filled_in_independently(self, env):
        from entry_engine.entry import FLAT_STOP_PCT
        db, _ = env
        d = mk_decision(db, stop=None, target=110.0)
        o = mk_order(db, decision=d)
        run(R._book_fill_delta(db, o, 100.0, 5, is_partial=False))
        p = positions(db)[0]
        assert p.current_stop == round(100.0 * (1 - FLAT_STOP_PCT / 100), 2) and p.current_target == 110.0

    def test_a_second_buy_fill_averages_into_the_same_position(self, env):
        db, _ = env
        o = mk_order(db)
        run(R._book_fill_delta(db, o, 100.0, 4, is_partial=True))
        run(R._book_fill_delta(db, o, 105.0, 6, is_partial=False))
        (p,) = positions(db)
        assert p.qty_open == 10 and p.avg_entry_price == pytest.approx(103.0)
        assert cash(db) == pytest.approx(START_CASH - 400.0 - 630.0)

    # SELL side
    def test_sell_books_the_exit_against_the_pending_exit_position(self, env):
        db, rig = env
        pos = mk_pos(db, status="PENDING_EXIT", qty=10, avg=100.0)
        o = mk_order(db, side="SELL", exit_reason="target_hit", dhan_id="S1")
        run(R._book_fill_delta(db, o, 105.0, 10, is_partial=False))
        assert pos.status == "CLOSED" and pos.qty_open == 0 and pos.realized_pnl == pytest.approx(50.0)
        assert o.status == "FILLED" and o.filled_qty_so_far == 10
        assert cash(db) == pytest.approx(START_CASH + 1_050.0)
        assert P.get_account(db, "REAL").realized_pnl_today == pytest.approx(50.0)
        assert rig.sent == ["🟢 *SELL filled* — ABC\n10 shares @ ₹105.00\nP&L: ₹+50.00"]

    def test_losing_exit_gets_the_red_marker(self, env):
        db, rig = env
        mk_pos(db, status="PENDING_EXIT")
        run(R._book_fill_delta(db, mk_order(db, side="SELL", dhan_id="S1"), 97.0, 10, is_partial=False))
        assert rig.sent[0].startswith("🔴 *SELL filled*") and "P&L: ₹-30.00" in rig.sent[0]

    def test_partial_sell_leaves_a_partially_closed_position(self, env):
        db, rig = env
        pos = mk_pos(db, status="PENDING_EXIT", qty=10)
        o = mk_order(db, side="SELL", dhan_id="S1")
        run(R._book_fill_delta(db, o, 102.0, 4, is_partial=True))
        assert pos.status == "PARTIALLY_CLOSED" and pos.qty_open == 6 and o.status == "PARTIAL"
        assert "SELL partial fill" in rig.sent[0] and "order 4/10 filled" in rig.sent[0]

    def test_sell_prefers_the_pending_exit_row_over_an_open_one(self, env):
        db, _ = env
        open_pos = mk_pos(db, status="OPEN", qty=5)
        pending = mk_pos(db, status="PENDING_EXIT", qty=10)
        run(R._book_fill_delta(db, mk_order(db, side="SELL", dhan_id="S1"), 101.0, 10, is_partial=False))
        assert pending.status == "CLOSED" and open_pos.status == "OPEN"

    @pytest.mark.parametrize("status", ["OPEN", "PARTIALLY_CLOSED"])
    def test_sell_falls_back_to_a_live_position(self, env, status):
        db, _ = env
        pos = mk_pos(db, status=status, qty=10)
        run(R._book_fill_delta(db, mk_order(db, side="SELL", dhan_id="S1"), 101.0, 10, is_partial=False))
        assert pos.status == "CLOSED"

    def test_sell_with_no_matching_position_advances_the_order_but_books_nothing(self, env):
        db, rig = env
        mk_pos(db, symbol="OTHER", status="OPEN")
        o = mk_order(db, side="SELL", dhan_id="S1")
        run(R._book_fill_delta(db, o, 101.0, 10, is_partial=False))
        assert o.status == "FILLED" and rig.sent == []
        assert cash(db) == START_CASH and positions(db)[0].qty_open == 10

    def test_partial_sell_without_a_position_is_marked_partial(self, env):
        db, _ = env
        o = mk_order(db, side="SELL", dhan_id="S1")
        run(R._book_fill_delta(db, o, 101.0, 3, is_partial=True))
        assert o.status == "PARTIAL"

    def test_a_confirmed_exit_fill_clears_the_failure_streak(self, env):
        db, _ = env
        pos = mk_pos(db, status="PENDING_EXIT", consecutive_exit_failures=4,
                     last_exit_failure_at=datetime.now(timezone.utc))
        run(R._book_fill_delta(db, mk_order(db, side="SELL", dhan_id="S1"), 101.0, 10, is_partial=False))
        assert pos.consecutive_exit_failures == 0 and pos.last_exit_failure_at is None

    def test_missing_exit_reason_is_logged_as_plain_exit(self, env):
        db, _ = env
        mk_pos(db, status="PENDING_EXIT")
        run(R._book_fill_delta(db, mk_order(db, side="SELL", dhan_id="S1", exit_reason=None), 101.0, 10, is_partial=False))
        ev = events(db, models.TradePositionEvent, event_type="CLOSED")
        assert ev and ev[0].detail.startswith("exit:")


# ── exit-failure streak / alerts ─────────────────────────────────────────────
class TestExitFailureAlerts:
    def track(self, db, pos, order, reason=None, code=None):
        run(R._track_exit_failure_and_maybe_alert(db, pos, order, "REJECTED", rejection_reason=reason, rejection_code=code))

    def test_streak_counts_and_stamps_the_time(self, env):
        db, rig = env
        pos = mk_pos(db, status="OPEN")
        o = mk_order(db, side="SELL", dhan_id="S1")
        self.track(db, pos, o)
        self.track(db, pos, o)
        assert pos.consecutive_exit_failures == 2 and pos.last_exit_failure_at is not None
        assert rig.sent == []

    def test_no_alert_until_the_threshold(self, env):
        db, rig = env
        pos = mk_pos(db, consecutive_exit_failures=3)
        self.track(db, pos, mk_order(db, side="SELL", dhan_id="S1"))
        assert pos.consecutive_exit_failures == 4 and rig.sent == []

    def test_alert_at_the_threshold_with_broker_reason_and_cooldown(self, env):
        db, rig = env
        pos = mk_pos(db, consecutive_exit_failures=4)
        self.track(db, pos, mk_order(db, side="SELL", dhan_id="S9"), reason="Circuit limit hit", code="RMS-12")
        (msg,) = rig.sent
        assert "Exit SELL stuck" in msg and "5 consecutive broker rejections" in msg and "order S9" in msg
        assert "Broker reason: Circuit limit hit [RMS-12]" in msg
        assert "960s" not in msg and "900s cooldown" in msg          # 60 * 2^4 = 960, capped at 900

    def test_alert_without_a_broker_reason_has_no_reason_line(self, env):
        db, rig = env
        pos = mk_pos(db, consecutive_exit_failures=4)
        self.track(db, pos, mk_order(db, side="SELL", dhan_id="S1"))
        assert "Broker reason" not in rig.sent[0]

    def test_alert_repeats_on_every_multiple_but_not_in_between(self, env):
        db, rig = env
        pos = mk_pos(db, consecutive_exit_failures=8)
        o = mk_order(db, side="SELL", dhan_id="S1")
        self.track(db, pos, o)                                        # 9th: quiet
        assert rig.sent == []
        self.track(db, pos, o)                                        # 10th: alert
        assert len(rig.sent) == 1

    def test_cooldown_shown_is_the_exponential_value_below_the_cap(self, env, monkeypatch):
        monkeypatch.setattr(config, "EXIT_RETRY_ALERT_THRESHOLD", 2)
        db, rig = env
        pos = mk_pos(db, consecutive_exit_failures=1)
        self.track(db, pos, mk_order(db, side="SELL", dhan_id="S1"))     # n=2 -> 60*2^1
        assert "120s cooldown" in rig.sent[0]

    def test_threshold_zero_disables_alerts(self, env, monkeypatch):
        monkeypatch.setattr(config, "EXIT_RETRY_ALERT_THRESHOLD", 0)
        db, rig = env
        pos = mk_pos(db, consecutive_exit_failures=99)
        self.track(db, pos, mk_order(db, side="SELL", dhan_id="S1"))
        assert rig.sent == []


# ── orphan repair ────────────────────────────────────────────────────────────
class TestOrphanRepair:
    def repair(self, db):
        tally = {}
        R._repair_orphaned_pending_exits(db, tally)
        return tally

    def test_pending_exit_with_no_live_sell_goes_back_to_open(self, env):
        db, _ = env
        pos = mk_pos(db, status="PENDING_EXIT")
        assert self.repair(db)["positions_unstuck"] == 1 and pos.status == "OPEN"
        ev = events(db, models.TradePositionEvent, event_type="EXIT_ORDER_DEAD")
        assert len(ev) == 1 and "restored to OPEN" in ev[0].detail

    def test_a_prior_partial_exit_restores_to_partially_closed(self, env):
        db, _ = env
        pos = mk_pos(db, status="PENDING_EXIT", realized=25.0)
        self.repair(db)
        assert pos.status == "PARTIALLY_CLOSED"

    @pytest.mark.parametrize("live", ["PLACED", "PARTIAL"])
    def test_a_sell_still_in_flight_is_left_alone(self, env, live):
        db, _ = env
        pos = mk_pos(db, status="PENDING_EXIT")
        mk_order(db, side="SELL", status=live, dhan_id="S1")
        assert self.repair(db)["positions_unstuck"] == 0 and pos.status == "PENDING_EXIT"

    @pytest.mark.parametrize("dead", ["REJECTED", "CANCELLED", "FILLED"])
    def test_a_finished_sell_does_not_count_as_in_flight(self, env, dead):
        db, _ = env
        pos = mk_pos(db, status="PENDING_EXIT")
        mk_order(db, side="SELL", status=dead, dhan_id="S1")
        self.repair(db)
        assert pos.status == "OPEN"

    def test_only_a_sell_for_the_same_symbol_counts(self, env):
        db, _ = env
        pos = mk_pos(db, symbol="ABC", status="PENDING_EXIT")
        mk_order(db, side="SELL", symbol="XYZ", status="PLACED", dhan_id="S1")
        mk_order(db, side="BUY", symbol="ABC", status="PLACED", dhan_id="B1")
        self.repair(db)
        assert pos.status == "OPEN"

    def test_other_positions_are_untouched_and_tally_is_zero_when_clean(self, env):
        db, _ = env
        pos = mk_pos(db, status="OPEN")
        assert self.repair(db)["positions_unstuck"] == 0 and pos.status == "OPEN"


# ── the full pass ────────────────────────────────────────────────────────────
class TestReconcileFlow:
    def go(self, db):
        return run(R.reconcile_real_orders(db))

    # housekeeping steps
    def test_no_pending_orders_still_runs_the_repair_and_holdings_steps(self, env):
        db, rig = env
        rig.imported = 3
        rig.ghost = {"closed": 0, "symbols": []}
        pos = mk_pos(db, status="PENDING_EXIT")
        t = self.go(db)
        assert t["positions_unstuck"] == 1 and t["holdings_imported"] == 3 and t["checked"] == 0
        assert rig.book_calls == 0                                   # nothing to look up at the broker
        assert pos.status == "OPEN"

    def test_ghost_positions_are_reported(self, env):
        db, rig = env
        rig.ghost = {"closed": 2, "symbols": ["AAA", "BBB"]}
        t = self.go(db)
        assert t["ghost_positions_closed"] == 2
        assert len(rig.sent) == 1 and "force-closed 2 ghost" in rig.sent[0] and "AAA, BBB" in rig.sent[0]

    def test_holdings_import_and_ghost_sync_failures_never_block_the_pass(self, env):
        db, rig = env
        rig.import_error = RuntimeError("holdings api down")
        rig.ghost_error = RuntimeError("ghost sync down")
        mk_order(db)
        rig.book = [row()]
        t = self.go(db)
        assert t["entries_filled"] == 1 and t["holdings_imported"] == 0 and t["ghost_positions_closed"] == 0

    def test_order_list_failure_is_an_error_not_a_crash(self, env):
        db, rig = env
        rig.book_error = RuntimeError("dhan 503")
        mk_order(db)
        pos = mk_pos(db, symbol="ZZZ", status="PENDING_EXIT")
        t = self.go(db)
        assert t["errors"] == 1 and t["checked"] == 0 and t["positions_unstuck"] == 1
        assert positions(db, "ABC") == [] and pos.status == "OPEN"

    # which orders are looked at
    def test_only_real_placed_or_partial_orders_are_checked(self, env):
        db, rig = env
        mk_order(db, dhan_id="O1", status="PLACED")
        mk_order(db, dhan_id="O2", status="PARTIAL", symbol="DEF", filled_so_far=3)
        mk_order(db, dhan_id="O3", status="FILLED", symbol="GHI")
        mk_order(db, dhan_id="O4", status="PLACED", symbol="JKL", mode="DEMO")
        rig.book = [row("O1"), row("O2", status="PART_TRADED", filled=3), row("O3"), row("O4")]
        assert self.go(db)["checked"] == 2

    def test_order_without_a_broker_id_is_skipped(self, env):
        db, rig = env
        mk_order(db, dhan_id=None)
        rig.book = [row()]
        t = self.go(db)
        assert t["checked"] == 1 and t["entries_filled"] == 0 and positions(db) == []

    def test_order_not_yet_in_the_book_is_left_for_next_cycle(self, env):
        db, rig = env
        o = mk_order(db)
        rig.book = [row("OTHER")]
        self.go(db)
        assert o.status == "PLACED" and positions(db) == []

    # fills
    @pytest.mark.parametrize("status", ["TRADED", "COMPLETE", "FILLED", "EXECUTED", "traded"])
    def test_every_filled_status_word_books_an_entry(self, env, status):
        db, rig = env
        mk_order(db)
        rig.book = [row(status=status)]
        t = self.go(db)
        assert t["entries_filled"] == 1 and positions(db)[0].qty_open == 10

    def test_entry_fill_end_to_end(self, env):
        db, rig = env
        o = mk_order(db)
        rig.book = [row(avg=101.5, filled=10)]
        t = self.go(db)
        assert t["checked"] == 1 and t["entries_filled"] == 1 and t["errors"] == 0
        (p,) = positions(db)
        assert (p.qty_open, p.avg_entry_price, p.current_stop, p.current_target) == (10, 101.5, 98.0, 104.0)
        assert o.status == "FILLED" and o.filled_qty_so_far == 10
        assert cash(db) == pytest.approx(START_CASH - 1_015.0)

    def test_exit_fill_end_to_end(self, env):
        db, rig = env
        pos = mk_pos(db, status="PENDING_EXIT", qty=10, avg=100.0)
        mk_order(db, side="SELL", dhan_id="S1", exit_reason="stop_hit")
        rig.book = [row("S1", avg=97.0, filled=10)]
        t = self.go(db)
        assert t["exits_confirmed"] == 1 and pos.status == "CLOSED" and pos.realized_pnl == pytest.approx(-30.0)

    def test_second_pass_never_double_books(self, env):
        db, rig = env
        mk_order(db)
        rig.book = [row()]
        self.go(db)
        t2 = self.go(db)
        assert t2["checked"] == 0                                   # order is FILLED now, not re-queried
        assert positions(db)[0].qty_open == 10 and cash(db) == pytest.approx(START_CASH - 1_000.0)

    def test_partial_then_complete_books_only_the_increment(self, env):
        db, rig = env
        o = mk_order(db)
        rig.book = [row(status="PART_TRADED", filled=4)]
        t1 = self.go(db)
        assert t1["partial_fills"] == 1 and o.status == "PARTIAL" and positions(db)[0].qty_open == 4
        rig.book = [row(status="TRADED", filled=10)]
        t2 = self.go(db)
        assert t2["entries_filled"] == 1 and o.status == "FILLED" and positions(db)[0].qty_open == 10

    def test_same_partial_seen_twice_is_not_booked_twice(self, env):
        db, rig = env
        o = mk_order(db)
        rig.book = [row(status="PART_TRADED", filled=4)]
        self.go(db)
        t2 = self.go(db)
        assert t2["partial_fills"] == 0 and positions(db)[0].qty_open == 4 and o.filled_qty_so_far == 4

    def test_partially_filled_status_word_is_understood(self, env):
        db, rig = env
        mk_order(db)
        rig.book = [row(status="PARTIALLY_FILLED", filled=3)]
        assert self.go(db)["partial_fills"] == 1

    def test_partial_sell_counts_as_a_partial(self, env):
        db, rig = env
        pos = mk_pos(db, status="PENDING_EXIT", qty=10)
        mk_order(db, side="SELL", dhan_id="S1")
        rig.book = [row("S1", status="PART_TRADED", avg=102.0, filled=4)]
        t = self.go(db)
        assert t["partial_fills"] == 1 and pos.qty_open == 6 and pos.status == "PARTIALLY_CLOSED"

    def test_complete_status_with_nothing_new_just_closes_the_order(self, env):
        db, rig = env
        o = mk_order(db, status="PARTIAL", filled_so_far=10)
        rig.book = [row(status="TRADED", filled=10)]
        self.go(db)
        assert o.status == "FILLED" and positions(db) == []            # already booked earlier: no second position

    def test_fill_details_can_come_from_alternate_keys(self, env):
        db, rig = env
        mk_order(db)
        rig.book = [{"order_id": "O1", "order_status": "TRADED", "order_type": "MARKET",
                     "average_traded_price": 99.0, "traded_quantity": 10}]
        assert self.go(db)["entries_filled"] == 1 and positions(db)[0].avg_entry_price == 99.0

    def test_traded_price_is_the_last_resort_price_key(self, env):
        db, rig = env
        mk_order(db)
        rig.book = [{"orderId": "O1", "orderStatus": "TRADED", "orderType": "MARKET", "tradedPrice": 98.5, "filledQty": 10}]
        self.go(db)
        assert positions(db)[0].avg_entry_price == 98.5

    def test_quantity_is_derived_from_total_minus_remaining(self, env):
        db, rig = env
        mk_order(db)
        rig.book = [{"orderId": "O1", "orderStatus": "PART_TRADED", "orderType": "MARKET",
                     "averageTradedPrice": 100.0, "quantity": 10, "remainingQuantity": 6}]
        self.go(db)
        assert positions(db)[0].qty_open == 4

    def test_unusable_remaining_quantity_means_nothing_is_booked(self, env):
        db, rig = env
        mk_order(db)
        rig.book = [{"orderId": "O1", "orderStatus": "TRADED", "orderType": "MARKET",
                     "averageTradedPrice": 100.0, "quantity": 10, "remainingQuantity": "n/a"}]
        self.go(db)
        assert positions(db) == []

    @pytest.mark.parametrize("kw", [{"avg": None}, {"filled": None}])
    def test_filled_status_without_price_or_quantity_is_left_as_is(self, env, kw):
        db, rig = env
        o = mk_order(db)
        rig.book = [row(**kw)]
        t = self.go(db)
        assert o.status == "PLACED" and positions(db) == [] and t["entries_filled"] == 0

    @pytest.mark.parametrize("status", ["PENDING", "TRANSIT", "OPEN", "TRIGGER_PENDING"])
    def test_orders_still_working_at_the_broker_are_untouched(self, env, status):
        db, rig = env
        o = mk_order(db)
        rig.book = [row(status=status, avg=None, filled=0)]
        t = self.go(db)
        assert o.status == "PLACED" and t["entries_filled"] == 0 and positions(db) == []

    # dead orders
    @pytest.mark.parametrize("broker_status,expected", [("REJECTED", "REJECTED"), ("CANCELLED", "CANCELLED"), ("CANCELED", "CANCELLED")])
    def test_dead_buy_is_marked_and_logged(self, env, broker_status, expected):
        db, rig = env
        o = mk_order(db)
        rig.book = [row(status=broker_status, avg=None, filled=0, omsErrorDescription="RMS blocked", omsErrorCode="E1")]
        t = self.go(db)
        assert t["dead_orders"] == 1 and o.status == expected and positions(db) == []
        ev = events(db, models.TradeOrderEvent, order_id=o.id)
        assert len(ev) == 1 and ev[0].event_type == expected
        assert "Broker reported" in ev[0].detail and "RMS blocked" in ev[0].detail and "[E1]" in ev[0].detail

    def test_buy_that_partially_filled_then_died_books_the_filled_part(self, env):
        db, rig = env
        o = mk_order(db)
        rig.book = [row(status="CANCELLED", avg=100.0, filled=4)]
        t = self.go(db)
        assert t["partial_fills"] == 1 and t["dead_orders"] == 1
        assert o.status == "CANCELLED" and positions(db)[0].qty_open == 4
        assert "after 4 of 10 already filled" in events(db, models.TradeOrderEvent, order_id=o.id)[-1].detail

    def test_dead_zero_fill_sell_unsticks_the_position_and_counts_the_failure(self, env):
        db, rig = env
        pos = mk_pos(db, status="PENDING_EXIT")
        o = mk_order(db, side="SELL", dhan_id="S1")
        rig.book = [row("S1", status="REJECTED", avg=None, filled=0)]
        t = self.go(db)
        assert t["dead_orders"] == 1 and t["positions_unstuck"] == 1
        assert pos.status == "OPEN" and pos.consecutive_exit_failures == 1 and o.status == "REJECTED"

    def test_dead_sell_finds_a_still_open_position_too(self, env):
        db, rig = env
        pos = mk_pos(db, status="OPEN")
        mk_order(db, side="SELL", dhan_id="S1")
        rig.book = [row("S1", status="CANCELLED", avg=None, filled=0)]
        t = self.go(db)
        assert pos.consecutive_exit_failures == 1 and t["positions_unstuck"] == 1

    def test_dead_sell_with_no_position_just_marks_the_order(self, env):
        db, rig = env
        o = mk_order(db, side="SELL", dhan_id="S1")
        rig.book = [row("S1", status="REJECTED", avg=None, filled=0)]
        t = self.go(db)
        assert o.status == "REJECTED" and t["positions_unstuck"] == 0

    def test_dead_sell_after_a_partial_exit_books_it_instead_of_unsticking(self, env):
        db, rig = env
        pos = mk_pos(db, status="PENDING_EXIT", qty=10)
        mk_order(db, side="SELL", dhan_id="S1")
        rig.book = [row("S1", status="CANCELLED", avg=102.0, filled=4)]
        t = self.go(db)
        assert t["partial_fills"] == 1 and t["positions_unstuck"] == 0
        assert pos.status == "PARTIALLY_CLOSED" and pos.qty_open == 6

    def test_repeated_dead_sells_raise_the_alert_at_the_threshold(self, env):
        db, rig = env
        mk_pos(db, status="OPEN", consecutive_exit_failures=4)
        mk_order(db, side="SELL", dhan_id="S1")
        rig.book = [row("S1", status="REJECTED", avg=None, filled=0, omsErrorDescription="Blocked by RMS")]
        self.go(db)
        assert any("Exit SELL stuck" in m and "Blocked by RMS" in m for m in rig.sent)

    def test_intraday_restriction_rejection_is_remembered(self, env):
        import intraday_eligibility as ie
        db, rig = env
        mk_order(db, symbol="MEDICAPS")
        rig.book = [row(status="REJECTED", avg=None, filled=0, omsErrorDescription=RESTRICTED_MSG)]
        self.go(db)
        assert ie.is_restricted(db, "MEDICAPS") is True

    def test_other_rejections_are_not_recorded_as_restrictions(self, env):
        import intraday_eligibility as ie
        db, rig = env
        mk_order(db, symbol="MEDICAPS")
        rig.book = [row(status="REJECTED", avg=None, filled=0, omsErrorDescription="Insufficient funds")]
        self.go(db)
        assert ie.is_restricted(db, "MEDICAPS") is False

    def test_recording_failure_never_blocks_the_dead_order_bookkeeping(self, env, monkeypatch):
        import intraday_eligibility as ie
        db, rig = env
        monkeypatch.setattr(ie, "record_restriction", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
        o = mk_order(db)
        rig.book = [row(status="REJECTED", avg=None, filled=0, omsErrorDescription=RESTRICTED_MSG)]
        t = self.go(db)
        assert o.status == "REJECTED" and t["dead_orders"] == 1

    # order-type mismatch alert
    @pytest.mark.parametrize("ours,theirs,alerts", [
        ("MARKET", "MARKET", False),
        ("MARKET", "LIMIT", False),           # Dhan echoes MARKET as LIMIT-with-protection: expected
        ("LIMIT", "LIMIT", False),
        ("MARKET", "", False),
        ("LIMIT", "MARKET", True),
        ("MARKET", "STOP_LOSS", True),
    ])
    def test_order_type_mismatch_alert_rules(self, env, ours, theirs, alerts):
        db, rig = env
        mk_order(db, order_type=ours)
        rig.book = [row(otype=theirs, status="PENDING", avg=None, filled=0)]
        self.go(db)
        assert bool([m for m in rig.sent if "order-type mismatch" in m]) is alerts

    def test_mismatch_is_alerted_only_once_per_order(self, env):
        db, rig = env
        mk_order(db, order_type="LIMIT")
        rig.book = [row(otype="MARKET", status="PENDING", avg=None, filled=0)]
        self.go(db)
        self.go(db)
        assert len([m for m in rig.sent if "order-type mismatch" in m]) == 1

    def test_several_orders_are_all_processed_in_one_pass(self, env):
        db, rig = env
        mk_order(db, dhan_id="O1", symbol="AAA")
        mk_order(db, dhan_id="O2", symbol="BBB")
        rig.book = [row("O1"), row("O2")]
        t = self.go(db)
        assert t["checked"] == 2 and t["entries_filled"] == 2
        assert sorted(p.symbol for p in positions(db)) == ["AAA", "BBB"]


# ── regressions for bugs found by this audit ─────────────────────────────────
class TestAuditRegressions:
    """One order whose booking raises must not stop the rest of the queue. Before the audit fix the
    exception escaped reconcile_real_orders(), main.py logged "self-heal failed", the reconcile
    throttle was never marked done, and the SAME order failed again at the SAME point every cycle —
    so every order queued behind it (including SELL confirmations) was never reconciled."""

    def test_a_failing_order_does_not_block_the_orders_behind_it(self, env, monkeypatch):
        db, rig = env
        bad = mk_order(db, dhan_id="O1", symbol="BAD")
        good = mk_order(db, dhan_id="O2", symbol="GOOD")
        rig.book = [row("O1"), row("O2")]
        real = R.record_real_fill

        def flaky(db_, order, *a, **k):
            if order.symbol == "BAD":
                raise RuntimeError("constraint violation")
            return real(db_, order, *a, **k)
        monkeypatch.setattr(R, "record_real_fill", flaky)
        t = run(R.reconcile_real_orders(db))
        assert t["errors"] == 1 and t["entries_filled"] == 1
        assert good.status == "FILLED" and [p.symbol for p in positions(db)] == ["GOOD"]
        assert bad.status == "PLACED"                                # rolled back, retried next cycle

    def test_a_failed_order_is_rolled_back_cleanly(self, env, monkeypatch):
        db, rig = env
        mk_order(db, dhan_id="O1", symbol="BAD")
        rig.book = [row("O1")]
        monkeypatch.setattr(R, "record_real_fill", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        run(R.reconcile_real_orders(db))
        o = db.query(models.TradeOrder).one()
        assert o.filled_qty_so_far in (0, None) and cash(db) == START_CASH and positions(db) == []


# ── known gaps (strict xfail) ────────────────────────────────────────────────
class TestKnownGaps:
    @pytest.mark.xfail(strict=True, reason=(
        "Dhan's averageTradedPrice is the CUMULATIVE average of the whole order, but reconcile books each "
        "increment (delta_qty) at that cumulative average instead of at the increment's own price. "
        "5 @ ₹100 then 5 @ ₹102 (cumulative avg ₹101) is booked as 5 @ 100 + 5 @ 101 => position avg "
        "₹100.50 and cash debited ₹1,005 instead of ₹101.00 / ₹1,010. Exits have the same shape "
        "(realized P&L drifts). A fix needs each increment's own price — for BUYs derivable from TradeFill "
        "rows, for SELLs record_real_exit_fill writes no TradeFill row, so it needs a small schema/record change."))
    def test_partial_fills_should_be_booked_at_the_increments_own_price(self, env):
        db, rig = env
        mk_order(db)
        rig.book = [row(status="PART_TRADED", avg=100.0, filled=5)]
        run(R.reconcile_real_orders(db))
        rig.book = [row(status="TRADED", avg=101.0, filled=10)]     # 5 @100 then 5 @102 => cumulative avg 101
        run(R.reconcile_real_orders(db))
        assert positions(db)[0].avg_entry_price == pytest.approx(101.0)
        assert cash(db) == pytest.approx(START_CASH - 1_010.0)
