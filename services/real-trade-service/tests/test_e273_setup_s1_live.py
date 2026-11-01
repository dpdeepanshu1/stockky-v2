"""E273 round 19: Real Trade S1_CATALYST_UP. Plain runner: python3 tests/test_e273_setup_s1_live.py (service folder). Stdlib only; clock and bars are made up."""
from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE))

import config  # noqa: E402
from entry_engine import orb_bars as OB  # noqa: E402
from entry_engine import preopen  # noqa: E402
from entry_engine import setup_live as SL  # noqa: E402
from entry_engine import setup_stamp  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def at(h, m, s=0):
    return datetime(2026, 10, 26, h, m, s, tzinfo=IST)


def bar(h, m, o, hi, lo, c, v=1000):
    return (at(h, m).timestamp(), o, hi, lo, c, v)


RANGE = [bar(9, 15, 100.0, 100.6, 99.9, 100.4), bar(9, 16, 100.4, 100.8, 100.3, 100.7), bar(9, 17, 100.7, 101.0, 100.6, 100.9),
         bar(9, 18, 100.9, 101.1, 100.8, 101.0), bar(9, 19, 101.0, 101.2, 100.9, 101.1)]
BREAK = RANGE + [bar(9, 20, 101.1, 101.6, 101.0, 101.5), bar(9, 21, 101.5, 101.9, 101.4, 101.8)]
EVENT = {"polarity": 1, "type": "order_win", "strength": 0.8}
NOW = at(9, 22, 30)


def run(**over):
    kw = dict(price=101.9, prev_close=100.0, gap_pct=0.5, event=EVENT, bars_facts=OB.range_trigger(BREAK, NOW), imbalance_5=0.3, now=NOW)
    kw.update(over)
    return SL.classify_long(**kw)


def test_range_trigger_facts():
    f = OB.range_trigger(BREAK, NOW)
    assert f["or_high"] == 101.2 and f["or_low"] == 99.9 and f["bar_closed_after_range"] is True and f["bar_close"] == 101.8 and f["vwap"] > 100.0
    assert OB.range_trigger(RANGE, at(9, 20, 30))["bar_closed_after_range"] is False       # the first bar after the range is not closed yet
    assert OB.range_trigger(BREAK, at(9, 40)) == {}                                       # stale
    assert OB.range_trigger(None, NOW) == {} and OB.range_trigger([("x",)], NOW) == {}


def test_s1_matches_with_event_break_and_two_confirmations():
    r = run()
    assert r["id"] == "S1_CATALYST_UP" and set(r["confirmations"]) >= {"tape_buying", "above_vwap"} and r["slot"] == "0920" and r["size_mult"] == 0.25
    assert r["stamp"]["id"] == "S1_CATALYST_UP" and setup_stamp.attach(None, r["stamp"])


def test_later_slot_is_half_size():
    later = at(9, 50)
    bars = BREAK + [bar(9, m, 101.8, 102.0, 101.7, 101.9) for m in range(22, 50)]
    r = run(now=later, bars_facts=OB.range_trigger(bars, later))
    assert r["id"] == "S1_CATALYST_UP" and r["size_mult"] == 0.5 and r["slot"] == "0930"


def test_no_event_no_setup():
    assert run(event=None)["id"] is None
    assert run(event={"polarity": -1})["id"] is None
    assert run(event={"polarity": 0})["id"] is None


def test_needs_a_closed_bar_above_the_range_high_and_price_over_preopen():
    assert run(bars_facts=OB.range_trigger(RANGE + [bar(9, 20, 101.1, 101.15, 101.0, 101.1)], at(9, 21, 30)), now=at(9, 21, 30))["id"] is None   # closed bar inside the range
    assert run(price=100.2)["id"] is None                       # price under the pre-open price 100.5
    assert run(gap_pct=None)["id"] is None                      # unknown pre-open price = not true
    assert run(bars_facts={})["id"] is None                     # no bars


def test_two_confirmations_needed():
    assert run(imbalance_5=0.0)["id"] is None                   # only above_vwap left
    assert run(imbalance_5=None)["id"] is None
    assert run(imbalance_5=0.3, bars_facts={**OB.range_trigger(BREAK, NOW), "vwap": 105.0})["id"] is None   # price under vwap, only the book left


def test_before_09_21_and_when_switched_off_nothing_fires():
    assert run(now=at(9, 20, 30))["id"] is None
    assert SL.classify_long(**{**dict(price=101.9, prev_close=100.0, gap_pct=0.5, event=EVENT, bars_facts=OB.range_trigger(BREAK, NOW), imbalance_5=0.3, now=NOW), "allowed": {"S2_RS_BREAK"}})["id"] is None


def test_penalties_shrink_the_size_and_a_large_total_skips():
    r = run(penalties={"opening_gate": 0.5})
    assert r["id"] == "S1_CATALYST_UP" and r["size_mult"] == 0.25         # the floor holds
    big = run(penalties={"a": 3.0, "b": 2.0})
    assert big["id"] is None and big["skip"] == "PENALTY_TOO_HIGH"


def test_preopen_gap_for():
    with preopen._lock:
        old = dict(preopen._snap)
        from tz_utils import ist_today_str
        preopen._snap.update({"day": ist_today_str(), "gaps": {"ABC": 1.25}})
    try:
        assert preopen.gap_for("ABC") == 1.25 and preopen.gap_for("abc.NS") == 1.25 and preopen.gap_for("ZZZ") is None
    finally:
        with preopen._lock:
            preopen._snap.clear(); preopen._snap.update(old)


def test_wiring_and_switches():
    src = (HERE / "entry_engine" / "entry.py").read_text()
    i = src.index("# E273 round 19")
    assert i < src.index("max_trade_risk = account_state.equity") and "except Exception" in src[i:i + 3500] and "never blocks an entry" in src[i:i + 900]
    assert "positive_event" in (HERE / "watchlist_engine" / "intraday_news.py").read_text()
    assert config.SETUP_ENGINE_ENABLED is True and config.SETUP_S1_ENABLED is True
    cat = (HERE / "control" / "catalog.py").read_text()
    assert 'Flag("SETUP_ENGINE_ENABLED"' in cat and 'Flag("SETUP_S1_ENABLED"' in cat


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn(); n += 1
            print("ok", name)
    print(n, "passed")
