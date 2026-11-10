"""Round 301: the real-order probe's final verdict follows what Dhan's positions show, and never says 'round trip done' (exit 0) when the SELL was not sent or Dhan still shows a position."""
from __future__ import annotations

from test_round261 import FakeBroker, go


class BlindBroker(FakeBroker):
    """The positions view always says flat (a segment / id mismatch, or a lag) although the BUY filled."""
    def net_positions(self):
        return {}


class StaleBroker(FakeBroker):
    """Dhan's positions still show the lot after our SELL filled."""
    def __init__(self, script, after):
        super().__init__(script)
        self.after = after

    def net_positions(self):
        return {"777": self.after} if len(self.placed) >= 2 else super().net_positions()


def test_a_flat_positions_view_after_a_filled_buy_is_not_a_clean_round_trip():
    b = BlindBroker(["fill"])
    r = go(b)
    text = "\\n".join(r["lines"])
    assert r["exit_code"] == 4 and r["bought"] == 65 and r["sold"] == 0                  # no SELL was sent, and the run does not claim success
    assert [p["transactionType"] for p in b.placed] == ["BUY"] and "NOT A CLEAN ROUND TRIP" in text and "NOT A CLEAN ROUND TRIP" in r["reason"]


def test_a_position_dhan_still_shows_after_our_sell_is_open_even_if_our_books_say_sold():
    b = StaleBroker(["fill", "fill"], after=65)
    r = go(b)
    assert r["sold"] == 65 and r["exit_code"] == 3 and r["dhan_net_position_after"] == 65
    assert "STILL OPEN" in "\\n".join(r["lines"])


def test_a_short_after_the_run_is_reported_as_open():
    b = StaleBroker(["fill", "fill"], after=-65)
    r = go(b)
    assert r["exit_code"] == 3 and "SHORT" in "\\n".join(r["lines"])


def test_the_normal_round_trip_is_still_exit_0():
    r = go(FakeBroker(["fill", "fill"]))
    assert r["exit_code"] == 0 and r["bought"] == 65 and r["sold"] == 65
