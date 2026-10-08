"""group 258: cumulative brokerage ledger (orders/charges_ledger.py). Pure maths + booking idempotence with a fake
session (no real DB needed)."""
from datetime import datetime, timezone
from types import SimpleNamespace

import config
from orders import charges_ledger as cl


def _row(i, entry=100.0, exit_=101.0, qty=10, status="TARGET_HIT", pnl=10.0, msg=None,
         closed=datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)):
    return SimpleNamespace(id=i, symbol=f"S{i}", entry_price=entry, exit_price=exit_, quantity=qty, status=status,
                           realized_pnl=pnl, error_message=msg, closed_at=closed, opened_at=closed)


class _Q:
    def __init__(self, ids): self.ids = ids
    def filter(self, *_a, **_k): return self
    def all(self): return [(i,) for i in self.ids]


class FakeDb:
    def __init__(self, existing=()):
        self.existing, self.added, self.commits, self.rollbacks = list(existing), [], 0, 0
    def query(self, *_a): return _Q(self.existing)
    def add(self, obj): self.added.append(obj)
    def commit(self): self.commits += 1
    def rollback(self): self.rollbacks += 1


def test_leg_brokerage_takes_lower_of_pct_and_cap():
    assert abs(cl.leg_brokerage(10_000) - 3.0) < 1e-9
    assert cl.leg_brokerage(1_000_000) == config.CHARGES_BROKERAGE_CAP_RS
    assert cl.leg_brokerage(0) == 0.0 and cl.leg_brokerage(-5) == 0.0


def test_charges_for_trade_brokerage_is_both_legs_and_total_includes_it():
    c = cl.charges_for_trade(100.0, 102.0, 100)
    assert abs(c["brokerage"] - (3.0 + 3.06)) < 1e-9
    assert abs(c["gst_on_brokerage"] - c["brokerage"] * 0.18) < 1e-9
    assert c["total"] > c["brokerage"]


def test_big_trade_hits_the_cap_on_each_leg():
    c = cl.charges_for_trade(1000.0, 1010.0, 1000)
    assert c["brokerage"] == 2 * config.CHARGES_BROKERAGE_CAP_RS


def test_book_positions_books_only_settled_and_skips_pending_error_open():
    db = FakeDb()
    rows = [
        _row(1),
        _row(2, status="ERROR", msg="Entry leg rejected", pnl=None),
        _row(3, status="STOP_HIT", msg="STOP_HIT_PENDING_RECONCILE"),
        _row(4, status="OPEN", exit_=None, pnl=None),
        _row(5, qty=0),
    ]
    assert cl.book_positions(db, rows) == 1
    assert [r.position_id for r in db.added] == [1] and db.commits == 1


def test_book_positions_is_idempotent_for_already_booked_ids():
    db = FakeDb(existing=[1])
    assert cl.book_positions(db, [_row(1), _row(6)]) == 1
    assert [r.position_id for r in db.added] == [6]


def test_book_positions_nothing_to_book_does_not_commit():
    db = FakeDb()
    assert cl.book_positions(db, [_row(2, status="ERROR", pnl=None)]) == 0
    assert db.commits == 0


def test_book_positions_failure_is_swallowed_and_rolled_back():
    class Boom(FakeDb):
        def commit(self): raise RuntimeError("db down")
    db = Boom()
    assert cl.book_positions(db, [_row(1)]) == 0
    assert db.rollbacks == 1


def test_ledger_row_day_is_the_ist_close_date():
    db = FakeDb()
    cl.book_positions(db, [_row(1, closed=datetime(2026, 10, 8, 20, 0, tzinfo=timezone.utc))])  # 01:30 IST next day
    assert db.added[0].day == "2026-10-09"


# -- group 259: range / today / total layout --
def _led(day, brokerage, gst_b, total, gross):
    return SimpleNamespace(day=day, brokerage=brokerage, gst_on_brokerage=gst_b, total_charges=total,
                           gross_pnl=gross, buy_value=1000.0, sell_value=1010.0)


class _CumDb:
    def __init__(self, rows): self.rows = rows
    def query(self, *_a): return SimpleNamespace(all=lambda: self.rows)


def test_cumulative_has_range_today_and_total_rows(monkeypatch):
    monkeypatch.setattr(cl, "sync_all", lambda db: 0)
    rows = [_led("2026-10-05", 4.0, 0.72, 8.0, 20.0), _led("2026-10-06", 2.0, 0.36, 5.0, -10.0),
            _led("2026-10-08", 1.0, 0.18, 2.0, 5.0)]
    s = cl.cumulative(_CumDb(rows), today="2026-10-08")
    h, t, tot = s["history"], s["today"], s["total"]
    assert (h["from"], h["to"], h["trades"]) == ("2026-10-05", "2026-10-07", 2)
    assert h["brokerage"] == 6.0 and h["all_charges"] == 13.0 and h["net_pnl"] == -3.0
    assert (t["from"], t["to"], t["trades"], t["all_charges"]) == ("2026-10-08", "2026-10-08", 1, 2.0)
    assert (tot["from"], tot["to"], tot["trades"]) == ("2026-10-05", "2026-10-08", 3)
    assert tot["all_charges"] == 15.0 == s["all_charges_total"]


def test_cumulative_history_none_when_only_today_or_empty(monkeypatch):
    monkeypatch.setattr(cl, "sync_all", lambda db: 0)
    s = cl.cumulative(_CumDb([_led("2026-10-08", 1.0, 0.18, 2.0, 5.0)]), today="2026-10-08")
    assert s["history"] is None and s["today"]["trades"] == 1
    e = cl.cumulative(_CumDb([]), today="2026-10-08")
    assert e["since"] is None and e["history"] is None and e["total"]["all_charges"] == 0
