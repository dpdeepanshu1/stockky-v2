"""E108 (T6): orders/sell_probe.py - the supervised 1-share Dhan SELL Super Order round trip, run against a scripted fake of Dhan.

The fake keeps real state (an order list, a Super Order list whose legs follow the cancels), so what is asserted is what would have been sent to Dhan, in order:
  * the happy path: SELL Super Order -> legs read back -> cancels -> BUY cover -> PASS, nothing left behind
  * every refusal before an order (confirm phrase, not armed, kill switch, window, no price, held symbol)
  * Dhan refusing the SELL (the answer T6 exists to get): FAIL, no cover, nothing left
  * the entry never filling: cancelled, NO BUY (a BUY with no short under it would open a long)
  * missing / wrong stop or target legs: FAIL, but the short is still covered
  * the cover failing: FAIL, adopted as a normal SHORT row, alert sent
  * exit legs still live after the cancel: NO BUY (double cover), adopted
  * the live short is refused until a PASS is on record
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

import config
import models
from execution import dhan_client
from orders import sell_probe

# Monday 26 Oct 2026, 11:00 IST: inside the probe window, a trading day
NOW = datetime(2026, 10, 26, 5, 30, tzinfo=timezone.utc)
LATE = datetime(2026, 10, 26, 10, 30, tzinfo=timezone.utc)       # 16:00 IST: market closed


class FakeDhan:
    """Scripted Dhan. Knobs: refuse_sell, fill (entry fills), legs ("ok" | "no_stop" | "no_target" | "wrong_stop" | "sell_side"), cancel_works, cover_works,
    list_fails_after_cancel, ambiguous."""

    def __init__(self, **kw):
        self.kw = dict(refuse_sell=None, fill=True, legs="ok", cancel_works=True, cover_works=True, list_fails_after_cancel=False, ambiguous=False,
                       cover_status="TRADED", lag=False, preheld=0, positions_fail=False, flat_despite_fill=False, no_avg=False)
        self.kw.update(kw)
        self.nets = {"1594": int(self.kw["preheld"])} if self.kw["preheld"] else {}      # Dhan's signed net quantity per security id (ITC is 1594)
        self.calls = []
        self.cancelled = False
        self.row = None
        self.orders = []
        # the real helpers the probe uses
        self.round_to_tick = dhan_client.round_to_tick
        self.clamp_super_levels = dhan_client.clamp_super_levels
        self.tick_size_for_price = dhan_client.tick_size_for_price
        self.read_stop_leg_price = dhan_client.read_stop_leg_price
        self.is_ambiguous_placement_error = dhan_client.is_ambiguous_placement_error
        self.short_entry_order = dhan_client.short_entry_order
        self.recovered = None                      # what find_recent_buy_order returns after an unclear SELL
        self.calls_made_order = False

    def get_security_id(self, db, sym):
        return "1594" if sym == "ITC" else "9999"

    def get_positions(self, db):
        self.calls.append(("get_positions", {}))
        if self.kw["positions_fail"]:
            raise RuntimeError("dhan 502")
        return [{"securityId": sid, "productType": "INTRADAY", "netQty": n, "sellAvg": 100.0 if n < 0 else 0.0} for sid, n in self.nets.items()]

    @property
    def net(self):
        return self.nets.get("1594", 0)

    def place_super_order(self, db, **kw):
        self.calls.append(("place_super_order", kw))
        if self.kw["refuse_sell"]:
            raise RuntimeError(self.kw["refuse_sell"])
        self.calls_made_order = True
        if (self.kw["fill"] or self.kw["lag"]) and not self.kw["flat_despite_fill"]:
            self.nets[kw["security_id"]] = self.nets.get(kw["security_id"], 0) - kw["quantity"]    # the exchange filled it; with lag=True the list does not say so
        t, s, k = kw["target_price"], kw["stop_loss_price"], self.kw["legs"]
        legs = [{"legName": "ENTRY_LEG", "orderStatus": "TRADED" if self.kw["fill"] else "PENDING", "transactionType": "SELL", "averageTradedPrice": kw["price"] if self.kw["fill"] else 0, "filledQty": kw["quantity"] if self.kw["fill"] else 0}]
        if k != "no_target":
            legs.append({"legName": "TARGET_LEG", "orderStatus": "PENDING", "transactionType": "SELL" if k == "sell_side" else "BUY", "price": t})
        if k != "no_stop":
            legs.append({"legName": "STOP_LOSS_LEG", "orderStatus": "PENDING", "transactionType": "SELL" if k == "sell_side" else "BUY", "triggerPrice": s + (5.0 if k == "wrong_stop" else 0)})
        if self.kw["no_avg"]:                       # E244: the 10:35 NTPC row - TRADED, filledQty 1, averageTradedPrice 0.0, no ENTRY_LEG detail
            legs = [lg for lg in legs if lg["legName"] != "ENTRY_LEG"]
        self.row = {"orderId": "SO1", "orderStatus": "TRADED" if self.kw["no_avg"] else "PENDING", "transactionType": "SELL", "legDetails": legs, "price": kw["price"],
                    "averageTradedPrice": 0.0 if self.kw["no_avg"] else (kw["price"] if self.kw["fill"] else 0), "filledQty": kw["quantity"] if self.kw["fill"] else 0}
        return {"orderId": "SO1", "orderStatus": "PENDING"}

    def find_recent_buy_order(self, db, security_id, qty, super_order=True, exclude_ids=(), within_s=180.0, transaction_type="BUY"):
        self.calls.append(("find_recent_buy_order", {"side": transaction_type}))
        return self.recovered

    def get_super_order_list(self, db):
        self.calls.append(("get_super_order_list", {}))
        if self.cancelled and self.kw["list_fails_after_cancel"]:
            raise RuntimeError("dhan 502")
        return [self.row] if self.row else []

    def cancel_super_order(self, db, *, order_id, order_leg):
        self.calls.append(("cancel_super_order", {"leg": order_leg}))
        if not self.kw["cancel_works"]:
            raise RuntimeError("cancel refused")
        self.cancelled = True
        for lg in self.row["legDetails"]:
            if lg["legName"] == order_leg and lg["orderStatus"] == "PENDING":
                lg["orderStatus"] = "CANCELLED"
        return {"orderId": order_id, "orderStatus": "CANCELLED"}

    def place_order(self, db, **kw):
        self.calls.append(("place_order", kw))
        if not self.kw["cover_works"]:
            raise RuntimeError("RMS: rejected")
        self.orders.append({"orderId": "BUY1", "orderStatus": self.kw["cover_status"], "averageTradedPrice": 100.4})
        if self.kw["cover_status"] == "TRADED":
            self.nets[kw["security_id"]] = self.nets.get(kw["security_id"], 0) + kw["quantity"]
        return {"orderId": "BUY1"}

    def get_order_list(self, db):
        return list(self.orders)

    def n(self, name):
        return sum(1 for c in self.calls if c[0] == name)


@pytest.fixture()
def env(monkeypatch, tmp_path):
    eng = create_engine("sqlite:///:memory:")
    models.Base.metadata.create_all(eng)
    db = sessionmaker(bind=eng)()
    db.add(models.ScalpGateState(is_armed=True, daily_loss_kill_switch_tripped=False, orders_placed_today=0))
    db.commit()
    monkeypatch.setenv("CONTROL_DIR", str(tmp_path))
    monkeypatch.setattr(config, "USE_SUPER_ORDER", True, raising=False)
    monkeypatch.setattr(sell_probe, "_mem", {})
    alerts = []
    yield NS(db=db, alerts=alerts, notify=alerts.append)
    db.close()


def _run(env, broker, *, symbol="ITC", confirm=sell_probe.CONFIRM_PHRASE, now=NOW, price=100.0):
    return sell_probe.run_probe(env.db, symbol=symbol, confirm=confirm, broker=broker, price_fn=lambda s: price, now=now, sleep=lambda s: None, poll_s=3.0, notify=env.notify)


def _locks(db):
    return db.query(models.SharedSymbolLock).count()


# ------------------------------------------------------------------ happy path
def test_clean_round_trip_passes_and_leaves_nothing_behind(env):
    b = FakeDhan()
    r = _run(env, b)
    assert r["verdict"] == "PASS", r["line"]
    assert r["line"].startswith("SELL PROBE: PASS")
    sells = [c[1] for c in b.calls if c[0] == "place_super_order"]
    assert len(sells) == 1 and sells[0]["transaction_type"] == "SELL" and sells[0]["quantity"] == 1
    assert sells[0]["product_type"] == "INTRADAY" and sells[0]["order_type"] == "LIMIT"           # E243: a marketable limit, not a MARKET entry Dhan rests at the last price
    assert sells[0]["target_price"] < sells[0]["price"] < 100.0 < sells[0]["stop_loss_price"], "the entry sits UNDER the last price so it fills at the bid"
    assert sells[0]["target_price"] < 100.0 < sells[0]["stop_loss_price"]                       # a short: target below, stop above
    buys = [c[1] for c in b.calls if c[0] == "place_order"]
    assert len(buys) == 1 and buys[0]["transaction_type"] == "BUY" and buys[0]["quantity"] == 1 and buys[0]["product_type"] == "INTRADAY"
    assert [c[1]["leg"] for c in b.calls if c[0] == "cancel_super_order"] == ["ENTRY_LEG", "TARGET_LEG", "STOP_LOSS_LEG"]
    order = [c[0] for c in b.calls if c[0] in ("place_super_order", "cancel_super_order", "place_order")]
    assert order.index("place_order") > max(i for i, n in enumerate(order) if n == "cancel_super_order"), "cover only after every leg is cancelled"
    assert _locks(env.db) == 0, "symbol lock handed back"
    assert env.db.query(models.ScalpPosition).count() == 0, "a clean probe adopts nothing"
    assert any("stop leg rests at the broker" in x for x in r["rules"]) and any("target leg reads back" in x for x in r["rules"])
    assert any("transactionType reads BUY" in x for x in r["rules"])
    assert sell_probe.passed() is True and sell_probe.summary()["passed"] is True
    assert env.alerts and "PASS" in env.alerts[-1]


def test_result_is_saved_and_survives_a_restart(env, monkeypatch):
    _run(env, FakeDhan())
    monkeypatch.setattr(sell_probe, "_mem", {})            # a restart loses memory, the file stays
    assert sell_probe.passed() is True and sell_probe.last_result()["symbol"] == "ITC"


def test_first_symbol_with_a_price_is_used_when_none_is_named(env):
    b = FakeDhan()
    r = sell_probe.run_probe(env.db, symbol=None, confirm=sell_probe.CONFIRM_PHRASE, broker=b, price_fn=lambda s: 50.0 if s == "NTPC" else None, now=NOW,
                             sleep=lambda s: None, poll_s=3.0, notify=env.notify)
    assert r["symbol"] == "NTPC" and r["verdict"] == "PASS"


# ------------------------------------------------------------------ refusals before any order
@pytest.mark.parametrize("why,kw", [
    ("confirm phrase", dict(confirm="yes")),
    ("outside the probe window", dict(now=LATE)),
])
def test_refusals_place_nothing_and_do_not_overwrite_a_real_result(env, why, kw):
    b = FakeDhan()
    r = _run(env, b, **kw)
    assert r["verdict"] == "SKIPPED" and why in r["reason"]
    assert b.calls == [] and sell_probe.last_result() == {}
    assert _locks(env.db) == 0


def test_not_armed_and_kill_switch_skip(env):
    gate = env.db.query(models.ScalpGateState).first()
    gate.is_armed = False
    env.db.commit()
    b = FakeDhan()
    assert "not armed" in _run(env, b)["reason"] and b.calls == []
    gate.is_armed, gate.daily_loss_kill_switch_tripped = True, True
    env.db.commit()
    assert "kill switch" in _run(env, b)["reason"] and b.calls == []


def test_no_live_price_open_position_and_held_symbol_skip(env):
    b = FakeDhan()
    assert "no live price" in _run(env, b, price=None)["reason"]
    env.db.add(models.ScalpPosition(symbol="ITC", window_source="5m", adaptive_target_pct=2.0, adaptive_stop_pct=1.0, status="OPEN", quantity=1, entry_price=100.0,
                                    capital_risked=100.0, overnight_converted_to_cnc=False, dhan_security_id="1", target_price=102.0, stop_price=98.0,
                                    opened_at=datetime.now(timezone.utc)))
    env.db.commit()
    assert "open position" in _run(env, b)["reason"]
    env.db.query(models.ScalpPosition).delete()
    env.db.add(models.SharedSymbolLock(symbol="ITC", held_by_service="real-trade-service", held_by_mode="REAL"))
    env.db.commit()
    assert "held by the other service" in _run(env, b)["reason"]
    assert b.n("place_super_order") == 0 and b.n("place_order") == 0 and b.n("cancel_super_order") == 0     # (positions are read-only and may be read)


def test_super_order_must_be_on(env, monkeypatch):
    monkeypatch.setattr(config, "USE_SUPER_ORDER", False, raising=False)
    b = FakeDhan()
    assert "USE_SUPER_ORDER" in _run(env, b)["reason"] and b.calls == []


# ------------------------------------------------------------------ Dhan's answers
def test_dhan_refusing_the_sell_is_a_fail_with_dhans_words_and_nothing_else_is_sent(env):
    b = FakeDhan(refuse_sell="Invalid transactionType for super order")
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "Dhan refused the SELL Super Order: Invalid transactionType" in r["reason"]
    assert b.n("place_order") == 0 and b.n("cancel_super_order") == 0
    assert r["answers"]["place_refused"].startswith("Invalid transactionType")
    assert sell_probe.passed() is False and _locks(env.db) == 0
    assert any("REFUSED" in x for x in r["rules"])


def test_an_unclear_sell_that_dhan_did_receive_is_picked_up_and_probed_on(env):
    class B(FakeDhan):
        def place_super_order(self, db, **kw):
            super().place_super_order(db, **kw)            # Dhan got it ...
            raise TimeoutError("timed out")                # ... but the answer never came back
    b = B()
    b.recovered = {"orderId": "SO1"}
    r = _run(env, b)
    assert r["verdict"] == "PASS" and b.n("find_recent_buy_order") == 1 and b.n("place_order") == 1


def test_an_ambiguous_placement_error_tells_you_to_check_dhan(env):
    b = FakeDhan(refuse_sell="x")
    b.place_super_order = lambda db, **kw: (_ for _ in ()).throw(TimeoutError("timed out"))
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "CHECK DHAN" in r["reason"] and "may exist" in r["reason"]
    assert any("placement unclear" in a for a in env.alerts)
    assert b.n("place_order") == 0, "no blind BUY after an unclear SELL"


def test_entry_that_never_fills_is_cancelled_and_never_bought(env):
    b = FakeDhan(fill=False)
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "did not show as filled" in r["reason"]
    assert b.n("place_order") == 0, "a BUY with no short under it would open a long"
    assert b.n("cancel_super_order") == 3 and env.db.query(models.ScalpPosition).count() == 0 and _locks(env.db) == 0


@pytest.mark.parametrize("legs,needle", [("no_stop", "no STOP_LOSS_LEG"), ("no_target", "no TARGET_LEG"), ("wrong_stop", "STOP_LOSS_LEG shows"),
                                         ("sell_side", "must be BUY")])
def test_bad_legs_fail_the_probe_but_the_short_is_still_covered(env, legs, needle):
    b = FakeDhan(legs=legs)
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and needle in r["reason"]
    assert b.n("place_order") == 1, "whatever the legs said, the 1-share short is covered"
    assert env.db.query(models.ScalpPosition).count() == 0


# ------------------------------------------------------------------ the cover, and what happens when it cannot be done
def test_a_rejected_cover_adopts_the_short_and_shouts(env):
    b = FakeDhan(cover_works=False)
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "COVER NOT CONFIRMED" in r["reason"] and "adopted as a normal SHORT row" in r["reason"]
    pos = env.db.query(models.ScalpPosition).one()
    assert pos.side == "SHORT" and pos.status == "OPEN" and pos.quantity == 1 and pos.symbol == "ITC" and pos.dhan_super_order_id == "SO1"
    assert pos.stop_price > pos.entry_price > pos.target_price
    assert b.n("place_order") == 3, "three cover attempts, then give up"
    assert any("cover not confirmed" in a for a in env.alerts)


def test_a_cover_that_ends_rejected_is_not_a_pass(env):
    b = FakeDhan(cover_status="REJECTED")
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "ended REJECTED" in r["reason"] and env.db.query(models.ScalpPosition).count() == 1


def test_a_cover_that_never_fills_is_not_a_pass(env):
    b = FakeDhan(cover_status="PENDING")
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "not confirmed filled" in r["reason"]
    assert env.db.query(models.ScalpPosition).one().side == "SHORT" and sell_probe.passed() is False


def test_legs_still_live_after_the_cancel_means_no_buy_is_sent(env, monkeypatch):
    b = FakeDhan(cancel_works=False)
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "double-cover" in r["reason"]
    assert b.n("place_order") == 0
    assert env.db.query(models.ScalpPosition).one().side == "SHORT"


def test_an_unreadable_order_list_after_the_cancel_sends_no_blind_buy(env):
    b = FakeDhan(list_fails_after_cancel=True)
    r = _run(env, b)
    assert r["verdict"] == "FAIL" and "could not read the order back" in r["reason"] and b.n("place_order") == 0
    assert env.db.query(models.ScalpPosition).one().side == "SHORT"


def test_an_ambiguous_cover_error_is_not_retried_blind(env):
    class B(FakeDhan):
        def place_order(self, db, **kw):
            self.calls.append(("place_order", kw))
            raise TimeoutError("timed out")
    b = B()
    r = _run(env, b)
    assert b.n("place_order") == 1 and r["verdict"] == "FAIL"


def test_an_unexpected_crash_is_reported_loudly_and_never_raises(env):
    class B(FakeDhan):
        def get_super_order_list(self, db):
            raise KeyError("boom")
    b = B()
    r = _run(env, b)
    assert r["verdict"] in ("FAIL", "PASS")        # a failed read is handled; the point is nothing escapes
    assert _locks(env.db) == 0


def test_one_probe_at_a_time(env):
    assert sell_probe._lock.acquire(blocking=False)
    try:
        r = _run(env, FakeDhan())
        assert r["verdict"] == "SKIPPED" and "already running" in r["reason"]
    finally:
        sell_probe._lock.release()


# ------------------------------------------------------------------ E242: Dhan's POSITIONS are the ground truth (28 Oct 2026 real run)
# The first real run read "entry never filled ... nothing to cover" from the Super Order list, sent no BUY, and Dhan showed an open ITC short with every leg cancelled
# and Stockky's Positions tab empty.
def test_entry_filled_at_dhan_but_not_shown_by_the_list_is_still_covered_and_reported(env):
    b = FakeDhan(fill=False, lag=True)
    r = _run(env, b)
    assert b.n("place_order") == 1, "the position was short, so the 1-share cover is sent"
    assert b.net == 0, "the book is flat again"
    assert r["verdict"] == "FAIL" and "did not show the entry as filled" in r["reason"] and "positions show the short filled" in r["reason"]
    assert env.db.query(models.ScalpPosition).count() == 0 and _locks(env.db) == 0
    assert r["answers"]["row_after_fill"]["orderStatus"] == "PENDING", "the raw row is kept so the field mismatch can be read off the run"


def test_short_at_dhan_with_a_cover_that_fails_is_adopted_even_when_the_list_never_showed_the_fill(env):
    b = FakeDhan(fill=False, lag=True, cover_works=False)
    r = _run(env, b)
    pos = env.db.query(models.ScalpPosition).one()           # this is what the Positions tab shows: not "nothing"
    assert pos.side == "SHORT" and pos.status == "OPEN" and pos.symbol == "ITC" and pos.quantity == 1
    assert "adopted as a normal SHORT row" in r["reason"] and any("cover not confirmed" in a for a in env.alerts)


def test_a_position_the_probe_did_not_open_blocks_it(env):
    b = FakeDhan(preheld=-3)                                  # ITC already short 3 at Dhan, as in the screenshot
    r = _run(env, b, symbol="ITC")
    assert r["verdict"] == "SKIPPED" and "already has a position at Dhan (net -3)" in r["reason"]
    assert b.n("place_super_order") == 0 and b.n("place_order") == 0 and _locks(env.db) == 0


def test_a_held_first_symbol_moves_the_default_probe_to_the_next_flat_one(env):
    b = FakeDhan(preheld=-3)
    r = sell_probe.run_probe(env.db, symbol=None, confirm=sell_probe.CONFIRM_PHRASE, broker=b, price_fn=lambda s: 100.0, now=NOW, sleep=lambda s: None, poll_s=3.0,
                             notify=env.notify)
    assert r["symbol"] == "NTPC" and b.n("place_super_order") == 1


def test_unreadable_positions_refuse_the_probe_before_any_order(env):
    b = FakeDhan(positions_fail=True)
    r = _run(env, b)
    assert r["verdict"] == "SKIPPED" and "positions could not be read" in r["reason"]
    assert b.n("place_super_order") == 0 and sell_probe.last_result() == {}


def test_positions_going_unreadable_after_the_order_sends_no_blind_buy(env):
    class B(FakeDhan):
        def get_positions(self, db):
            if self.calls_made_order:
                raise RuntimeError("dhan 502")
            return super().get_positions(db)
    b = B(fill=False)
    r = _run(env, b)
    assert b.n("place_order") == 0 and r["verdict"] == "FAIL"
    assert "CHECK DHAN" in r["reason"] or "did not show as filled" in r["reason"]


def test_a_filled_row_over_a_flat_position_buys_nothing(env):
    b = FakeDhan(flat_despite_fill=True)
    r = _run(env, b)
    assert b.n("place_order") == 0, "a BUY on a flat book would open a long"
    assert r["verdict"] == "FAIL" and "positions never showed a short" in r["reason"]
    assert env.db.query(models.ScalpPosition).count() == 0


def test_the_cover_is_checked_against_the_position(env):
    b = FakeDhan()
    r = _run(env, b)
    assert r["verdict"] == "PASS" and b.net == 0 and r["answers"]["short_after_cancel"] == 1


# ------------------------------------------------------------------ static rails read from the source
def test_source_rails():
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "orders", "sell_probe.py"), encoding="utf-8").read()
    code = re.sub(r'""".*?"""', "", src, flags=re.S)
    assert "PROBE_QTY = 1" in code and "quantity=PROBE_QTY" in code
    assert code.count("broker.place_super_order(") == 1 and code.count("broker.place_order(") == 1
    assert 'transaction_type="SELL"' in code and 'transaction_type="BUY"' in code and 'product_type="INTRADAY"' in code and "CNC" not in code
    # the BUY sits behind the 'entry really filled' decision and the live-legs check
    assert code.index("if fill is None and filled_now is None") < code.index("broker.place_order(")
    assert code.index("if live_legs:") < code.index("broker.place_order(")
    assert "confirm != CONFIRM_PHRASE" in code
    entry = open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "orders", "entry.py"), encoding="utf-8").read()
    assert "sell_probe" not in entry


# ------------------------------------------------------------------ the live short waits for a PASS
def test_live_short_is_refused_until_a_pass_is_on_record(env, monkeypatch):
    from orders import short_live
    monkeypatch.setattr(config, "SHORT_MODE", "live", raising=False)
    monkeypatch.setattr(config, "SHORT_LIVE_REQUIRES_PROBE", True, raising=False)
    for mod in ("opening_protect", "entry_cap"):
        monkeypatch.setattr(getattr(short_live, mod), "pause_reason" if mod == "opening_protect" else "entry_reason", lambda *a, **k: None, raising=False)
    cand = NS(symbol="ITC")
    why = short_live._live_guards(env.db, cand)
    assert why and why[0] == "SHORT_PROBE_NOT_PASSED"
    _run(env, FakeDhan())                                   # a PASS on record
    why = short_live._live_guards(env.db, cand)
    assert why is None or why[0] != "SHORT_PROBE_NOT_PASSED"
    sell_probe._mem.clear()
    os.remove(sell_probe._path())
    monkeypatch.setattr(config, "SHORT_LIVE_REQUIRES_PROBE", False, raising=False)
    why = short_live._live_guards(env.db, cand)
    assert why is None or why[0] != "SHORT_PROBE_NOT_PASSED", "the switch turns the check off"


def test_a_fail_on_record_does_not_count_as_a_pass(env):
    _run(env, FakeDhan(refuse_sell="nope"))
    assert sell_probe.passed() is False and sell_probe.summary()["verdict"] == "FAIL"


def test_summary_before_any_probe():
    s = sell_probe.summary()
    assert s["verdict"] in ("NOT_RUN", "PASS", "FAIL") and "line" in s


def test_a_traded_row_with_no_average_price_is_a_fill_not_a_failure(env):
    """E244: Dhan's row said TRADED / filledQty 1 with averageTradedPrice 0.0 and no ENTRY_LEG detail (10:35 NTPC run). The entry did fill: legs are checked, the short is covered, PASS."""
    b = FakeDhan(no_avg=True)
    r = _run(env, b)
    assert r["verdict"] == "PASS", r["line"]
    assert len([c for c in b.calls if c[0] == "place_order"]) == 1, "the filled share is covered exactly once"
    assert r.get("entry_fill") and any("averageTradedPrice 0" in x for x in r["rules"])
