"""T-CA: corporate actions and event gaps. An unadjusted split must not create a fake break / zone, a 12-30 % gap is an event (not a split),
circuit bars are flagged. Runs with pytest or plain `python3 tests/run_plain.py`."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bars import T0, DAY, retime, scale, walk
from market_structure.engine import analyze
from market_structure.params import MSParams
from market_structure.prep import prep


def test_unadjusted_split_cuts_history_at_the_discontinuity():
    pre = walk(150)
    post = retime(scale(walk(100, seed=6, base=pre[-1][4]), 0.5), 150)      # price halves overnight (2:1 split, history NOT adjusted)
    p = prep(pre + post, MSParams())
    assert p["dq"]["ca_discontinuity"] is True and p["dq"]["ca_index"] == 150
    assert len(p["bars"]) == 100 and "corporate_action_discontinuity" in p["dq"]["reasons"]


def test_split_creates_no_bearish_break_or_zone_from_the_old_prices():
    pre = walk(150)
    post = retime(scale(walk(100, seed=6, base=pre[-1][4]), 0.5), 150)
    b = analyze(pre + post)
    assert b["data_quality"]["ca_discontinuity"] is True
    # every level the engine reports lives on the post-split price scale
    last = b["last_close"]
    for z in (b["levels"]["supports"] + b["levels"]["resistances"]):
        assert 0.3 * last < z["price_low"] < 3.0 * last
    for s in b["swings"]:
        assert 0.3 * last < s["price"] < 3.0 * last


def test_split_leaving_too_little_history_is_insufficient_not_a_guess():
    pre = walk(150)
    post = retime(scale(walk(20, seed=6, base=pre[-1][4]), 0.5), 150)
    b = analyze(pre + post)
    assert b["quality"] == "insufficient" and b["trend"] is None and b["technical_quality_score"] is None


def test_adjusted_history_is_not_cut():
    bars = walk(200)
    p = prep(bars, MSParams())
    assert p["dq"]["ca_discontinuity"] is False and len(p["bars"]) == 200


def test_event_gap_between_12_and_30_pct_is_flagged_not_cut():
    a = walk(130)
    b = retime(scale(walk(70, seed=9, base=a[-1][4]), 1.0), 130)
    b = [(t, o * 1.2, h * 1.2, l * 1.2, c * 1.2, v) for (t, o, h, l, c, v) in b]     # +20 % gap, history stays in
    p = prep(a + b, MSParams())
    assert p["dq"]["ca_discontinuity"] is False and len(p["bars"]) == 200
    assert p["gap_event"][130] is True and sum(p["gap_event"]) == 1


def test_circuit_bars_are_flagged():
    bars = list(walk(150))
    t, o, h, l, c, v = bars[100]
    bars[100] = (t, c, c, c, c, v)        # high == low: locked at a circuit
    p = prep(bars, MSParams())
    assert p["circuit"][100] is True
    analyze(bars)                          # and the engine still runs
