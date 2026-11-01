"""E273: setup_engine. Plain runner: python3 tests/test_e273_setup_engine.py (pytest collects it too). Pure module, loaded by path."""
from __future__ import annotations

import importlib.util
import os
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
spec = importlib.util.spec_from_file_location("se", os.path.join(os.path.dirname(HERE), "orders", "setup_engine.py"))
se = importlib.util.module_from_spec(spec)
spec.loader.exec_module(se)


def at(h, m):
    return datetime(2026, 11, 2, h, m)


def s1(**kw):
    f = dict(side="LONG", now=at(9, 21), event_polarity=1, event_priced_in=0.4, price=101.0, preopen_price=100.0, or_high=100.8, or_low=99.0, bar_close=101.0,
             above_vwap=True, tape=1, sector_third="mid")
    f.update(kw)
    return f


def s4(**kw):
    f = dict(side="SHORT", now=at(9, 21), event_polarity=-1, event_priced_in=0.3, price=99.0, preopen_price=100.0, or_high=101.0, or_low=99.4, bar_close=99.0,
             above_vwap=False, tape=-1, sector_third="mid")
    f.update(kw)
    return f


def s2(side="LONG", **kw):
    sg = 1 if side == "LONG" else -1
    f = dict(side=side, now=at(9, 30), rs_pct=0.95 if side == "LONG" else 0.05, gap_pct=0.5 * sg, structure_ok=True, or_break=sg, vol_vs_norm=1.5, candle=sg, tape=0)
    f.update(kw)
    return f


def s3(side="SHORT", **kw):
    f = dict(side=side, now=at(9, 45), z_move=2.5 if side == "SHORT" else -2.5, failure_candle=True, tape_exhaustion=True, vwap_stretch=True, climax_volume=True,
             liquid=True, kind="spike" if side == "SHORT" else "panic_dump")
    f.update(kw)
    return f


def test_s1_fires_on_trigger_with_two_of_three_and_stamps_it():
    r = se.classify(s1())
    assert r["id"] == se.S1 and r["confirmations"] == ["tape_buying", "above_vwap"] and r["slot"] == "0920"
    assert r["size_mult"] == 0.25 and r["stamp"]["id"] == se.S1 and r["stamp"]["side"] == "LONG"


def test_one_confirmation_is_not_enough():
    assert se.classify(s1(tape=0, sector_third="mid"))["skip"] == "NO_SETUP"


def test_unknown_or_wrong_polarity_never_triggers():
    for pol in (0, None, -1):
        assert se.classify(s1(event_polarity=pol))["id"] is None


def test_priced_in_refuses_s1_and_s4():
    assert se.classify(s1(event_priced_in=1.2))["id"] is None
    assert se.classify(s4(event_priced_in=1.2))["id"] is None


def test_s1_needs_a_closed_bar_beyond_the_range_and_price_above_preopen():
    assert se.classify(s1(bar_close=100.5))["id"] is None             # closed inside the range
    assert se.classify(s1(price=99.5))["id"] is None                  # below the pre-open price
    assert se.classify(s1(bar_closed_after_range=False))["id"] is None


def test_clock_nothing_before_0921_s2_from_0930_s3_from_0945():
    assert se.classify(s1(now=at(9, 20)))["id"] is None and se.classify(s1(now=at(9, 8)))["id"] is None
    assert se.classify(s1(now=at(9, 21)))["id"] == se.S1
    assert se.classify(s2(now=at(9, 29)))["id"] is None and se.classify(s2(now=at(9, 30)))["id"] == se.S2
    assert se.classify(s3(now=at(9, 44)))["id"] is None and se.classify(s3(now=at(9, 45)))["id"] == se.S3
    assert se.classify(s1(now=None))["id"] is None


def test_s4_is_the_short_mirror_of_s1():
    r = se.classify(s4())
    assert r["id"] == se.S4 and r["side"] == "SHORT" and r["confirmations"] == ["tape_selling", "below_vwap"]
    assert se.classify(s4(side="LONG"))["id"] is None                 # a long never takes S4


def test_real_trade_is_long_only_and_never_enters_on_s4():
    assert se.classify({**s4(), "service": "RT"})["id"] is None
    assert se.classify({**s1(), "service": "RT"})["id"] == se.S1


def test_s2_never_fires_on_a_breakout_alone():
    assert se.classify(s2(rs_pct=0.5))["id"] is None
    assert se.classify(s2(gap_pct=-0.5))["id"] is None                # gap against the side
    assert se.classify(s2(structure_ok=False))["id"] is None
    assert se.classify(s2(vol_vs_norm=0.5, candle=0))["id"] is None   # only 0 of 3 (tape 0) confirmations
    r = se.classify(s2())
    assert r["id"] == se.S2 and r["size_mult"] == 0.5


def test_s2_short_mirror():
    r = se.classify(s2("SHORT"))
    assert r["id"] == se.S2 and r["side"] == "SHORT"
    assert se.classify(s2("SHORT", gap_pct=0.5))["id"] is None


def test_s1_beats_s2_and_s2_is_kept_as_an_extra_confirmation():
    r = se.classify({**s2(now=at(9, 30)), **s1(now=at(9, 30))})
    assert r["id"] == se.S1 and r["extra"] == [se.S2] and se.S2 in r["confirmations"]


def test_s3_short_after_up_spike_and_long_after_panic_dump():
    assert se.classify(s3())["id"] == se.S3
    r = se.classify(s3("LONG"))
    assert r["id"] == se.S3 and r["side"] == "LONG"
    assert se.classify(s3("LONG", liquid=False))["id"] is None        # long reversal only in liquid names
    assert se.classify(s3(z_move=1.0))["id"] is None
    assert se.classify(s3(failure_candle=False))["id"] is None
    assert se.classify(s3(tape_exhaustion=False))["id"] is None


def test_allowed_switch_removes_a_setup():
    assert se.classify({**s1(), "allowed": {se.S2}})["id"] is None


def test_gap_beyond_the_95th_percentile_halves_the_size():
    assert se.classify(s1(gap_pctile=0.97))["size_mult"] == 0.125


def test_promotion_gives_full_size_and_penalties_shrink_or_skip():
    assert se.classify(s2(promoted={"S2_RS_BREAKOUT|0930"}))["size_mult"] == 1.0
    r = se.classify(s2(penalties={"near_high": 1.0}))
    assert r["size_mult"] == 0.25 and r["penalty_total"] == 1.0       # 0.5 / 2 = 0.25 (the floor)
    big = se.classify(s2(penalties={"a": 2.5, "b": 2.0}))
    assert big["id"] is None and big["skip"] == "PENALTY_TOO_HIGH"


def test_never_raises():
    for bad in (None, {}, {"now": "x"}, {"side": 5, "now": at(10, 0)}, {"penalties": {"a": "x"}, "now": at(10, 0)}):
        assert se.classify(bad)["id"] is None


def test_the_two_copies_are_identical_and_registered():
    root = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
    a = open(os.path.join(root, "services", "position-stocks-service", "orders", "setup_engine.py"), "rb").read()
    b = open(os.path.join(root, "services", "real-trade-service", "entry_engine", "setup_engine.py"), "rb").read()
    assert a == b
    assert '"setup_engine"' in open(os.path.join(root, "scripts", "check_shared_copies.py"), encoding="utf-8").read()


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v(); n += 1
    print(f"{n} passed")
