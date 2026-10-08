"""group 258: cumulative brokerage ledger (orders/charges_ledger.py). Pure maths + booking idempotence with a fake
session (no real DB needed)."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import config
from orders import charges_ledger as cl


def _row(i, entry=100.0, exit_=101.0, qty=10, status="TARGET_HIT", pnl=10.0, msg=None,
         closed=datetime(2026, 10, 8, 6, 0, tzinfo=timezone.utc)):
    return SimpleNamespace(id=i, symbol=f"S{i}", entry_price=entry, exit_price=exit_, quantity=qty, status=status,
                           realized_pnl=pnl, error_message=msg, closed_at=closed, opened_at=closed)


class _Q:
    def __init__(self, rows): self.rows = rows
    def filter(self, *_a, **_k): return self
    def all(self): return list(self.rows)


def _booked(i, buy=1000.0, sell=1010.0, pnl=10.0):
    return SimpleNamespace(position_id=i, quantity=10, buy_value=buy, sell_value=sell, brokerage=0.0,
                           gst_on_brokerage=0.0, total_charges=0.0, gross_pnl=pnl)


class FakeDb:
    def __init__(self, existing=()):
        # `existing` = ids already booked; a booked row matches the default _row() (buy 1000 / sell 1010 / pnl 10)
        self.rows = [_booked(i) if not hasattr(i, "position_id") else i for i in existing]
        self.added, self.commits, self.rollbacks = [], 0, 0
    def query(self, *_a): return _Q(self.rows)
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
_LED_ID = [0]


def _led(day, brokerage, gst_b, total, gross, buy=1000.0, sell=1010.0):
    _LED_ID[0] += 1
    return SimpleNamespace(position_id=_LED_ID[0], day=day, brokerage=brokerage, gst_on_brokerage=gst_b,
                           total_charges=total, gross_pnl=gross, buy_value=buy, sell_value=sell)


class _CumDb:
    def __init__(self, rows): self.rows = rows
    def query(self, *_a): return SimpleNamespace(all=lambda: self.rows)


def test_cumulative_has_range_today_and_total_rows(monkeypatch):
    monkeypatch.setattr(cl, "sync_all", lambda db: 0)
    one = cl.charges_for_values(1000.0, 1010.0)["total"]     # every row has the same 1000 / 1010 legs
    rows = [_led("2026-10-05", 4.0, 0.72, 8.0, 20.0), _led("2026-10-06", 2.0, 0.36, 5.0, -10.0),
            _led("2026-10-08", 1.0, 0.18, 2.0, 5.0)]
    s = cl.cumulative(_CumDb(rows), today="2026-10-08")
    h, t, tot = s["history"], s["today"], s["total"]
    assert (h["from"], h["to"], h["trades"]) == ("2026-10-05", "2026-10-07", 2)
    assert h["all_charges"] == pytest.approx(2 * one, abs=0.011) and h["net_pnl"] == pytest.approx(10.0 - 2 * one, abs=0.011)
    assert (t["from"], t["to"], t["trades"]) == ("2026-10-08", "2026-10-08", 1)
    assert t["all_charges"] == pytest.approx(one, abs=0.011)
    assert (tot["from"], tot["to"], tot["trades"]) == ("2026-10-05", "2026-10-08", 3)
    assert tot["all_charges"] == pytest.approx(3 * one, abs=0.02) == pytest.approx(s["all_charges_total"], abs=0.02)


def test_cumulative_history_none_when_only_today_or_empty(monkeypatch):
    monkeypatch.setattr(cl, "sync_all", lambda db: 0)
    s = cl.cumulative(_CumDb([_led("2026-10-08", 1.0, 0.18, 2.0, 5.0)]), today="2026-10-08")
    assert s["history"] is None and s["today"]["trades"] == 1
    e = cl.cumulative(_CumDb([]), today="2026-10-08")
    assert e["since"] is None and e["history"] is None and e["total"]["all_charges"] == 0


def test_triveni_round_trip_matches_hand_calc():
    c = cl.charges_for_trade(250.0, 246.49, 11)         # buy 2,750 / sell 2,711.39
    assert abs(c["brokerage"] - (0.825 + 0.813417)) < 1e-6
    assert c["stt"] == pytest.approx(2711.39 * 0.00025, abs=1e-6)
    assert c["total"] == pytest.approx(2.8995, abs=0.01)         # was 3.61 with STT on both legs + the stale exchange rate


def test_exchange_charge_includes_ipft():
    c = cl.charges_for_values(10_000.0, 0.0)
    assert c["exchange"] == pytest.approx(10_000 * (0.00297 + 0.0001) / 100)


def test_cumulative_restates_rows_booked_with_an_old_rate_card(monkeypatch):
    monkeypatch.setattr(cl, "sync_all", lambda db: 0)
    stale = _led("2026-10-08", 99.0, 99.0, 99.0, 5.0)           # stored total is nonsense; legs are what count
    s = cl.cumulative(_CumDb([stale]), today="2026-10-08")
    assert s["all_charges_total"] == pytest.approx(cl.charges_for_values(1000.0, 1010.0)["total"], abs=0.01)
    assert s["total"]["components"]["stt"] == pytest.approx(1010 * 0.00025, abs=0.01)


def test_book_positions_refreshes_a_booked_row_whose_prices_were_repaired():
    db = FakeDb(existing=[1])
    stale = db.rows[0]
    fixed = _row(1, entry=250.0, exit_=246.49, qty=11, pnl=-38.61)
    assert cl.book_positions(db, [fixed]) == 0              # nothing NEW booked
    assert db.added == [] and db.commits == 1               # but the existing row was corrected
    assert stale.buy_value == 2750.0 and stale.sell_value == pytest.approx(2711.39) and stale.gross_pnl == -38.61
    assert stale.total_charges == pytest.approx(cl.charges_for_trade(250.0, 246.49, 11)["total"], abs=0.006)


def test_book_positions_leaves_an_unchanged_booked_row_alone():
    db = FakeDb(existing=[1])
    assert cl.book_positions(db, [_row(1)]) == 0
    assert db.commits == 0
