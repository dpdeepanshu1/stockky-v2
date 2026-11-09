"""Round 261: the real-order probe (fno/realprobe.py), tested offline with a fake provider and a fake broker. No network, no money."""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace as NS

from fno import execution, realprobe
from fno.realprobe import IST, CONFIRM_PHRASE

NOW = datetime(2026, 11, 9, 11, 0, 0, tzinfo=IST)          # a Monday inside the live window


class Quote:
    def __init__(self, bid=100.0, ask=100.5, ltp=100.25, sid=777):
        self.bid, self.ask, self.ltp, self.security_id = bid, ask, ltp, sid
        self.mid = (bid + ask) / 2
        self.spread_pct = (ask - bid) / self.mid * 100


class Chain:
    def __init__(self, q, age=2.0):
        self.rows = [NS(strike=24400.0, side=lambda s: q), NS(strike=24450.0, side=lambda s: q), NS(strike=24500.0, side=lambda s: q)]
        self.spot, self._age = 24455.0, age

    def atm_index(self): return 1
    def step(self): return 50.0
    def row(self, strike): return next(r for r in self.rows if r.strike == strike)
    def age_s(self, now=None): return self._age


class Prov:
    def __init__(self, q=None, age=2.0, expiries=("2026-11-10",)):
        self.q, self.age, self._exp = q or Quote(), age, list(expiries)

    def expiries(self, und): return self._exp
    def chain(self, und, exp): return Chain(self.q, self.age)
    def lookup(self, und, exp, strike, side): return NS(security_id=777, lot_size=65, segment="NSE_FNO", tick_size=0.05)


class FakeBroker:
    """script = list of states per order in the order they are placed: 'fill' | 'nofill' | 'reject'."""
    def __init__(self, script, positions_before=None):
        self.script, self.placed, self.cancelled, self.pos = list(script), [], [], dict(positions_before or {})
        self.orders = {}

    def net_positions(self): return dict(self.pos) if self.pos is not None else None

    def place(self, payload):
        oid = f"O{len(self.placed) + 1}"
        self.placed.append(payload)
        kind = self.script.pop(0) if self.script else "fill"
        self.orders[oid] = (kind, payload)
        if kind == "fill":
            q = payload["quantity"] * (1 if payload["transactionType"] == "BUY" else -1)
            self.pos["777"] = self.pos.get("777", 0) + q
            if self.pos["777"] == 0:
                del self.pos["777"]
        return execution.OrderResult(kind != "reject", order_id=oid, status="TRANSIT")

    def order_status(self, oid):
        kind, p = self.orders[oid]
        if oid in self.cancelled:
            return execution.OrderStatus("CANCELLED", 0.0, 0, {})
        if kind == "fill":
            return execution.OrderStatus("TRADED", p["price"], p["quantity"], {"orderStatus": "TRADED"})
        return execution.OrderStatus("PENDING", 0.0, 0, {"orderStatus": "PENDING"})

    def cancel(self, oid):
        self.cancelled.append(oid)
        return execution.OrderResult(True, order_id=oid, status="CANCELLED")


def go(broker, live=True, prov=None, phrase=CONFIRM_PHRASE, now=NOW, **kw):
    return realprobe.run(prov or Prov(), broker, "1113433554", live=live, confirm=lambda _: phrase, sleep=lambda s: None, now=lambda: now, printer=lambda s: None, **kw)


def test_dry_run_sends_nothing_and_shows_the_order():
    b = FakeBroker([])
    r = go(b, live=False)
    assert r["exit_code"] == 0 and b.placed == [] and r["planned"]["buy_price"] == 100.5
    text = "\n".join(r["lines"])
    assert "POST /v2/orders" in text and "***554" in text and "1113433554" not in text


def test_full_round_trip_pnl_and_charges_identity():
    b = FakeBroker(["fill", "fill"])
    r = go(b)
    assert r["exit_code"] == 0 and r["bought"] == r["sold"] == 65 and r["dhan_net_position_after"] == 0
    assert [o["side"] for o in r["orders"]] == ["BUY", "SELL"]
    assert b.placed[0]["price"] == 100.5 and b.placed[0]["productType"] == "INTRADAY" and b.placed[0]["orderType"] == "LIMIT"
    pnl = r["pnl"]
    assert abs(pnl["gross"] - (100.0 - 100.5) * 65) < 0.01                      # bought at the ask, sold at the bid
    assert abs((pnl["market_move"] - pnl["spread_buy"] - pnl["spread_sell"]) - pnl["gross"]) < 0.01
    assert abs(pnl["net"] - (pnl["gross"] - pnl["charges"])) < 0.01 and pnl["charges"] > 40   # two flat Rs20 brokerages + GST alone are > 47


def test_wrong_phrase_sends_nothing():
    b = FakeBroker([])
    r = go(b, phrase="yes")
    assert r["exit_code"] == 2 and b.placed == []


def test_refuses_outside_window_stale_quote_wide_spread_and_big_premium():
    for kw in (dict(now=datetime(2026, 11, 9, 9, 20, tzinfo=IST)), dict(now=datetime(2026, 11, 9, 15, 0, tzinfo=IST)), dict(now=datetime(2026, 11, 8, 11, 0, tzinfo=IST)),
               dict(prov=Prov(age=120.0)), dict(prov=Prov(q=Quote(bid=90.0, ask=100.0))), dict(max_outlay=1000.0)):
        b = FakeBroker([])
        assert go(b, **kw)["exit_code"] == 2 and b.placed == []


def test_refuses_when_dhan_positions_unreadable_or_already_open():
    b = FakeBroker([]); b.pos = None
    assert go(b)["exit_code"] == 2 and b.placed == []
    b = FakeBroker([], positions_before={"777": 65})
    assert go(b)["exit_code"] == 2 and b.placed == []


def test_no_expiry_two_days_away_rule_and_expiry_day_is_skipped():
    assert go(FakeBroker([]), prov=Prov(expiries=("2026-11-09",)))["exit_code"] == 2     # today = the expiry day itself


def test_rejected_buy_leaves_nothing_open():
    b = FakeBroker(["reject"])
    r = go(b)
    assert r["exit_code"] == 2 and len(b.placed) == 1


def test_unfilled_buy_is_cancelled_exit_5():
    b = FakeBroker(["nofill"])
    r = go(b)
    assert r["exit_code"] == 5 and b.cancelled == ["O1"] and len(b.placed) == 1


def test_sell_that_does_not_fill_is_cancelled_and_resent_lower_then_fills():
    b = FakeBroker(["fill", "nofill", "fill"])
    r = go(b)
    sells = [p for p in b.placed if p["transactionType"] == "SELL"]
    assert r["exit_code"] == 0 and len(sells) == 2 and sells[1]["price"] < sells[0]["price"] and r["sold"] == 65
    assert r["pnl"]["charges"] > 0 and not any("extra SELL" in l for l in r["lines"])     # a cancelled, unfilled order costs no brokerage


def test_position_stuck_open_exits_3_and_never_sells_more_than_held():
    b = FakeBroker(["fill", "nofill", "nofill", "nofill", "nofill"])
    r = go(b)
    assert r["exit_code"] == 3 and r["sold"] == 0
    assert all(p["quantity"] <= 65 for p in b.placed if p["transactionType"] == "SELL") and len(b.placed) == 1 + realprobe.SELL_TRIES


def test_cancel_that_never_settles_is_in_doubt_exit_4_and_sends_nothing_more():
    class Stuck(FakeBroker):
        def cancel(self, oid):                       # the cancel is accepted but the order never shows a final state
            return execution.OrderResult(True, order_id=oid, status="PENDING")
    b = Stuck(["nofill"])
    r = go(b)
    assert r["exit_code"] == 4 and len(b.placed) == 1


def test_dhan_never_answers_exit_4():
    class Mute(FakeBroker):
        def order_status(self, oid): return None
    r = go(Mute(["fill"]))
    assert r["exit_code"] == 4
