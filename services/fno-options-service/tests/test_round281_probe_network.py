"""Round 281: the real-order probe never says "nothing is open" after a network failure on the BUY, and never re-sends a SELL after one."""
from __future__ import annotations

from fno import execution
from test_round261 import FakeBroker, go


class NetBroker(FakeBroker):
    """The n-th place() call (1-based) fails on the network; the order may or may not have reached Dhan (the test decides by `reaches`)."""
    def __init__(self, script, fail_at, reaches=True):
        super().__init__(script)
        self.fail_at, self.reaches, self.calls = fail_at, reaches, 0

    def place(self, payload):
        self.calls += 1
        if self.calls == self.fail_at:
            self.placed.append(payload)
            if self.reaches:
                oid = f"O{len(self.placed)}"
                self.orders[oid] = ("nofill", payload)
            return execution.OrderResult(False, status="NETWORK", message="timed out")
        return super().place(payload)


def test_a_network_failure_on_the_buy_is_in_doubt_not_nothing_open():
    b = NetBroker([], fail_at=1)
    r = go(b)
    text = "\n".join(r["lines"])
    assert r["exit_code"] == 4 and len(b.placed) == 1                      # exit 4 = state in doubt; nothing else was sent
    assert "MAY have reached Dhan" in text and "Nothing is open" not in text and "correlationId" in text


def test_a_plain_rejection_of_the_buy_is_still_nothing_open():
    b = FakeBroker(["reject"])
    r = go(b)
    assert r["exit_code"] == 2 and "Nothing is open" in "\n".join(r["lines"])


def test_a_network_failure_on_the_sell_sends_no_second_sell():
    b = NetBroker(["fill"], fail_at=2)                                       # BUY fills, the SELL call times out
    r = go(b)
    assert r["exit_code"] == 4 and [p["transactionType"] for p in b.placed] == ["BUY", "SELL"]     # no SELL on top of a possibly live one
    assert r["bought"] == 65 and r["sold"] == 0 and "close" in "\n".join(r["lines"]).lower()


def test_a_plain_rejection_of_a_sell_still_retries():
    b = FakeBroker(["fill", "reject", "fill"])
    r = go(b)
    assert r["exit_code"] == 0 and r["sold"] == 65 and [p["transactionType"] for p in b.placed] == ["BUY", "SELL", "SELL"]
