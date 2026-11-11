"""Round 386 (E402): real-order probe, three things the 11 Nov live run showed.
1) an empty expiry list (market-data unreachable) is no longer reported as "only the expiry day is left";
2) the "+ N s" timeline counts from the moment each order is SENT, not from program start (typing the confirmation phrase used to count);
3) a fill BELOW the mid is a price improvement and is worded as one (the line used to say "above the mid by Rs -16.25")."""
from __future__ import annotations

from fno import execution, realprobe
from test_round261 import FakeBroker, Prov, go


class _P(Prov):
    last_error = "/dhan/options/expiries: URLError"


def test_empty_expiry_list_names_market_data_not_expiry_day():
    b = FakeBroker([])
    r = go(b, live=False, prov=_P(expiries=()))
    text = "\n".join(r["lines"])
    assert r["exit_code"] == 2 and b.placed == []
    assert "returned no expiry list" in text and "URLError" in text and "only the expiry day" not in text


def test_only_the_expiry_day_left_still_says_so():
    r = go(FakeBroker([]), live=False, prov=Prov(expiries=("2026-11-09",)))      # NOW is 9 Nov: the only listed expiry is today
    text = "\n".join(r["lines"])
    assert r["exit_code"] == 2 and "only the expiry day itself is left" in text and "2026-11-09" in text


def test_timeline_counts_from_the_send_not_from_the_start(monkeypatch=None):
    clock = {"t": 1000.0}
    real = realprobe.time.monotonic
    realprobe.time.monotonic = lambda: clock["t"]
    try:
        def typing(_):
            clock["t"] += 30.0                                 # the person takes 30 s to type the phrase
            return realprobe.CONFIRM_PHRASE
        r = realprobe.run(Prov(), FakeBroker(["fill", "fill"]), "1113433554", live=True, confirm=typing, sleep=lambda s: None,
                          now=lambda: __import__("test_round261").NOW, printer=lambda s: None)
    finally:
        realprobe.time.monotonic = real
    assert r["exit_code"] == 0
    assert [o["timeline"][0]["after_s"] for o in r["orders"]] == [0.0, 0.0]


class _Better(FakeBroker):
    def order_status(self, oid):
        kind, p = self.orders[oid]
        if p["transactionType"] == "BUY":
            return execution.OrderStatus("TRADED", 100.0, p["quantity"], {"orderStatus": "TRADED"})        # asked 100.5, filled 100.0: below the mid 100.25
        return super().order_status(oid)


def test_fill_below_mid_is_worded_as_improvement():
    r = go(_Better(["fill", "fill"]))
    text = "\n".join(r["lines"])
    assert r["exit_code"] == 0
    assert "BELOW the mid" in text and "above the mid by Rs -" not in text and "--" not in text.split("WHERE THE MONEY WENT")[1]
    pn = r["pnl"]
    assert abs(pn["gross"] - (pn["market_move"] - pn["spread_buy"] - pn["spread_sell"])) < 0.02      # the identity still holds
