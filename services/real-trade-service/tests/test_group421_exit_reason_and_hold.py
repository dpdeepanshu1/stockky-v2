"""group 421: exit-reason attribution from position events, sessions held, hold bucket and return % on the per-trade record
(trade_records.py). Pure functions plus the real loader against in-memory SQLite."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import trade_records as tr


def utc(h, m=0, day=9):
    return datetime(2026, 10, day, h, m, tzinfo=timezone.utc)


def pos(**kw):
    d = dict(id=1, mode="REAL", symbol="AAA", status="CLOSED", avg_entry_price=100.0, realized_pnl=-20.0, opened_at=utc(4, 0), closed_at=utc(9, 30),
             broker_imported=False, watchlist_entry_id=None, source_tab="watchlist", entry_decision_label="BUY NOW", entry_conviction_score=70.0,
             is_regime_override=False, net_realized_pnl=None, realized_cost_estimate=None)
    d.update(kw)
    return NS(**d)


def order(id, side, reason=None, source="AUTO", at=None, qty=None):
    return NS(id=id, side=side, symbol="AAA", mode="REAL", status="FILLED", exit_reason=reason, decision_id=None, watchlist_entry_id=None,
              execution_source=source, created_at=at or utc(4, 0), updated_at=at or utc(4, 0), qty=qty)


def fill(order_id, qty, price, at):
    return NS(order_id=order_id, qty=qty, price=price, filled_at=at)


def ev(kind, detail, at=None, position_id=1):
    return NS(position_id=position_id, event_type=kind, detail=detail, occurred_at=at or utc(9, 30))


def rec(positions=None, orders=(), fills=(), events=()):
    return tr.build_records(positions or [pos()], orders, fills, [], [], [], [], events)[0]


class TestEventReason:
    def test_ghost_closed_sold_at_the_broker(self):
        r = rec(events=[ev("GHOST_CLOSED", "holdings_sync_reconcile: AAA still listed in Dhan holdings but live positions show net -5 (sold at the broker, no exit fill booked here)")])
        assert (r["exit_reason"], r["exit_reason_source"]) == ("sold_outside_app", "event_ghost_closed")

    def test_ghost_closed_not_held(self):
        r = rec(events=[ev("GHOST_CLOSED", "holdings_sync_reconcile: AAA not found in Dhan holdings or live positions")])
        assert (r["exit_reason"], r["exit_reason_source"]) == ("ghost_closed", "event_ghost_closed")

    def test_demo_closed_event_carries_the_reason(self):
        r = rec(events=[ev("CLOSED", "eod_squareoff: 10 @ 101.5 (pnl +15.00)")])
        assert (r["exit_reason"], r["exit_reason_source"]) == ("eod_squareoff", "event_closed")

    def test_exit_sent_is_used_when_there_is_no_closed_event(self):
        r = rec(events=[ev("EXIT_SENT", "stop_hit: SELL 10 sent to Dhan, broker order_id=1")])
        assert (r["exit_reason"], r["exit_reason_source"]) == ("stop_hit", "event_exit_sent")

    def test_newest_closing_event_wins(self):
        r = rec(events=[ev("EXIT_SENT", "stop_hit: SELL 10 sent", at=utc(9, 0)), ev("CLOSED", "emergency_gap_down: 10 @ 90", at=utc(9, 30))])
        assert r["exit_reason"] == "emergency_gap_down"

    @pytest.mark.parametrize("detail", ["exit: 10 @ 100 (pnl +0.00, broker-confirmed)", "no colon here", ": 10 @ 1", "a_reason_text_that_is_far_too_long_to_be_a_code: x"])
    def test_generic_or_malformed_detail_gives_no_reason(self, detail):
        r = rec(events=[ev("CLOSED", detail)])
        assert r["exit_reason"] is None and r["exit_reason_source"] == "none"

    def test_other_event_types_are_ignored(self):
        r = rec(events=[ev("STOP_TRAILED", "stop_hit: moved")])
        assert r["exit_reason"] is None

    def test_events_of_another_position_are_ignored(self):
        r = rec(events=[ev("CLOSED", "stop_hit: 1 @ 1", position_id=99)])
        assert r["exit_reason"] is None


class TestPrecedence:
    def test_order_reason_beats_events(self):
        r = rec(orders=[order(1, "BUY", at=utc(4, 0)), order(2, "SELL", reason="target_hit_partial", at=utc(9, 30))],
                events=[ev("CLOSED", "stop_hit: 1 @ 1")])
        assert (r["exit_reason"], r["exit_reason_source"]) == ("target_hit_partial", "order")

    def test_manual_sell_without_a_reason(self):
        r = rec(orders=[order(1, "BUY", at=utc(4, 0)), order(2, "SELL", source="MANUAL", at=utc(9, 30))])
        assert (r["exit_reason"], r["exit_reason_source"]) == ("manual", "order_manual")

    def test_sell_without_reason_falls_to_events_then_to_unspecified(self):
        o = [order(1, "BUY", at=utc(4, 0)), order(2, "SELL", at=utc(9, 30))]
        assert rec(orders=o, events=[ev("CLOSED", "time_stop: 3 @ 99")])["exit_reason"] == "time_stop"
        r = rec(orders=o)
        assert (r["exit_reason"], r["exit_reason_source"]) == ("unspecified_sell", "order_without_reason")

    def test_nothing_known_is_none_with_source_none(self):
        r = rec()
        assert r["exit_reason"] is None and r["exit_reason_source"] == "none"

    def test_no_events_argument_keeps_the_old_behaviour(self):
        r = tr.build_records([pos()], [], [], [], [], [], [])[0]
        assert r["exit_reason"] is None


class TestTradingDaysHeld:
    # 2026-10-09 is a Friday. UTC 04:00 = 09:30 IST.
    @pytest.mark.parametrize("o,c,want", [
        (utc(4, 0, 9), utc(9, 30, 9), 0),           # same IST day
        (utc(4, 0, 9), utc(4, 0, 12), 1),           # Fri -> Mon is one session (weekend skipped)
        (utc(4, 0, 12), utc(4, 0, 13), 1),
        (utc(4, 0, 12), utc(4, 0, 16), 4),
        (utc(4, 0, 12), utc(4, 0, 19), 5),          # Mon -> next Mon
        (utc(4, 0, 12), utc(4, 0, 21), 6),           # 20 Oct 2026 is Dussehra (holiday), so Mon -> Wed counts 6
    ])
    def test_counts_sessions(self, o, c, want):
        assert tr.trading_days_held(o, c) == want

    def test_late_utc_evening_is_the_next_ist_day(self):
        assert tr.trading_days_held(utc(4, 0, 12), utc(19, 0, 12)) == 1      # 00:30 IST on the 13th

    def test_holiday_is_skipped(self):
        # 2026-10-20 (Tue) is not necessarily a listed holiday; use whatever tz_utils lists, falling back to a skip when none matches
        from tz_utils import _NSE_HOLIDAYS_2026
        wk = sorted(d for d in _NSE_HOLIDAYS_2026 if datetime.strptime(d, "%Y-%m-%d").weekday() < 4)
        if not wk:
            pytest.skip("no weekday holiday in the list")
        h = datetime.strptime(wk[0], "%Y-%m-%d").replace(tzinfo=timezone.utc)
        o = h - timedelta(days=1) + timedelta(hours=4)
        c = h + timedelta(days=1) + timedelta(hours=4)
        assert tr.trading_days_held(o, c) == 1                               # the holiday does not count

    @pytest.mark.parametrize("o,c", [(None, utc(5)), (utc(5), None), (utc(5, 0, 12), utc(5, 0, 9))])
    def test_missing_or_reversed_is_none(self, o, c):
        assert tr.trading_days_held(o, c) is None


class TestHoldBucketAndReturn:
    @pytest.mark.parametrize("d,b", [(None, None), (0, "same_day"), (1, "1_day"), (2, "2_5_days"), (5, "2_5_days"), (6, "6_plus_days"), (30, "6_plus_days")])
    def test_bucket(self, d, b):
        assert tr.hold_bucket(d) == b

    def test_record_carries_days_bucket_and_net_return(self):
        o = [order(1, "BUY", at=utc(4, 0), qty=10), order(2, "SELL", reason="stop_hit", at=utc(9, 30))]
        f = [fill(1, 10, 100.0, utc(4, 0)), fill(2, 10, 98.0, utc(9, 30))]
        r = rec([pos(realized_pnl=-20.0, net_realized_pnl=-25.0)], o, f)
        assert r["held_days"] == 0 and r["hold_bucket"] == "same_day"
        assert r["return_basis"] == "net" and r["return_pct"] == pytest.approx(-2.5)

    def test_gross_price_move_when_the_size_is_unknown(self):
        o = [order(1, "BUY", at=utc(4, 0)), order(2, "SELL", reason="stop_hit", at=utc(9, 30))]
        f = [fill(2, 10, 98.0, utc(9, 30))]
        r = rec([pos()], o, f)
        assert r["return_basis"] == "gross_price" and r["return_pct"] == pytest.approx(-2.0)

    def test_no_data_gives_none(self):
        r = rec([pos(avg_entry_price=None)])
        assert r["return_pct"] is None or r["return_basis"] in ("net", "gross_price")


class TestLoaderUsesEvents:
    def test_closed_position_without_a_sell_order_gets_its_ghost_reason(self):
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from sqlalchemy.pool import StaticPool
        import models
        eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        models.Base.metadata.create_all(eng)
        db = sessionmaker(bind=eng)()
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        p = models.TradePosition(mode="REAL", symbol="ZZZ", status="CLOSED", qty_open=0, avg_entry_price=50.0, realized_pnl=-10.0,
                                 opened_at=now - timedelta(hours=3), closed_at=now - timedelta(hours=1))
        db.add(p)
        db.flush()
        db.add(models.TradePositionEvent(position_id=p.id, event_type="GHOST_CLOSED",
                                         detail="holdings_sync_reconcile: ZZZ ... (sold at the broker, no exit fill booked here)"))
        db.commit()
        recs = tr.load_records(db, "REAL", 7)
        assert len(recs) == 1 and recs[0]["exit_reason"] == "sold_outside_app" and recs[0]["exit_reason_source"] == "event_ghost_closed"
        assert recs[0]["hold_bucket"] in ("same_day", "1_day")
