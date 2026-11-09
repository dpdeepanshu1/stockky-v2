"""Round 262: the real-order probe says in plain words what a Dhan refusal means (the first live try on 9 Nov 2026 hit DH-906 'enable in trade control'), and can be pointed at a chosen expiry."""
from __future__ import annotations

from fno import execution, realprobe
from test_round261 import FakeBroker, Prov, go


class Refusing(FakeBroker):
    def __init__(self, status, message):
        super().__init__([])
        self._s, self._m = status, message

    def place(self, payload):
        self.placed.append(payload)
        return execution.OrderResult(False, status=self._s, message=self._m)


def test_hint_for_trader_control_switched_off():
    h = realprobe.dhan_hint("HTTP 400", "DH-906: Please enable in trade control to proceed")
    assert "Trader Control" in h and "Nothing was bought" in h


def test_hint_for_segment_not_yet_registered_ip_token_and_rate_limit():
    assert "1-2 working days" in realprobe.dhan_hint("HTTP 400", "DH-906: Order error")
    assert "whitelisted" in realprobe.dhan_hint("HTTP 400", "DH-905: x")
    assert "token" in realprobe.dhan_hint("HTTP 401", "DH-901: x")
    assert "rate limit" in realprobe.dhan_hint("HTTP 429", "DH-904")
    assert realprobe.dhan_hint("NETWORK", "URLError") == ""


def test_a_refused_buy_prints_what_to_do_and_exits_2_with_nothing_open():
    b = Refusing("HTTP 400", "DH-906: Please enable in trade control to proceed")
    r = go(b)
    text = "\n".join(r["lines"])
    assert r["exit_code"] == 2 and len(b.placed) == 1 and "WHAT TO DO" in text and "Trader Control" in text


def test_expiry_can_be_chosen_and_must_be_one_dhan_lists():
    p = Prov(expiries=("2026-11-10", "2026-11-17"))
    r = go(FakeBroker([]), live=False, prov=p, expiry_pick="2026-11-17")
    assert r["exit_code"] == 0 and any("2026-11-17" in l for l in r["lines"])
    r = go(FakeBroker([]), live=False, prov=p, expiry_pick="2026-12-01")
    assert r["exit_code"] == 2 and any("not one Dhan lists" in l for l in r["lines"])
    r = go(FakeBroker([]), live=False, prov=p)                       # default stays the nearest one
    assert any("2026-11-10" in l for l in r["lines"])
