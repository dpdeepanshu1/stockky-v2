"""E274 round 41: the 09:21 S4 short window (short_gates.early_open / evaluate(early=True)), the squeeze guard (orders/squeeze_guard.py) and their wiring in plan_short."""
import json
import pathlib
from datetime import datetime

import config
from orders import short_gates, squeeze_guard
from tz_utils import IST

ROOT = pathlib.Path(__file__).resolve().parents[1]


def at(h, m):
    return datetime(2026, 11, 2, h, m, tzinfo=IST)


def test_early_window_is_09_21_to_09_45_only():
    assert short_gates.early_open(at(9, 21)) and short_gates.early_open(at(9, 44))
    assert not short_gates.early_open(at(9, 20)) and not short_gates.early_open(at(9, 45)) and not short_gates.early_open(at(11, 0))


def test_early_window_switch(monkeypatch):
    monkeypatch.setattr(config, "SHORT_EARLY_S4_ENABLED", False, raising=False)
    assert not short_gates.early_open(at(9, 30))


def test_start_time_is_configurable(monkeypatch):
    monkeypatch.setattr(config, "SHORT_EARLY_S4_START_IST", "09:30", raising=False)
    assert not short_gates.early_open(at(9, 25)) and short_gates.early_open(at(9, 31))


def test_time_window_early_still_has_the_cutoff():
    assert short_gates.time_window({}, at(9, 25))[0] == "SHORT_TOO_EARLY"
    assert short_gates.time_window({}, at(9, 25), early=True) is None
    assert short_gates.time_window({}, at(13, 40), early=True)[0] == "SHORT_CUTOFF"


def test_early_evaluate_skips_vwap_and_the_too_early_refusal_only(monkeypatch):
    seen = []
    gates = tuple((n, (lambda f, now, n=n: seen.append(n))) for n, _ in short_gates.GATES)
    monkeypatch.setattr(short_gates, "GATES", gates)
    short_gates.evaluate({}, at(9, 25), early=True)
    assert "vwap" not in seen and "time" not in seen and seen.count("circuit") == 1 and "quality" in seen
    seen.clear()
    short_gates.evaluate({}, at(10, 30))
    assert "vwap" in seen and "time" in seen


def test_early_evaluate_does_not_skip_the_other_gates():
    f = {"live": 97.0, "tick_age_s": 1.0, "signal_ltp": 97.0, "prev_close": 100.0, "day_open": 99.0, "day_high": 99.0, "day_low": 96.0, "vwap": 99.0, "quote": None}
    v = short_gates.evaluate(f, at(9, 25), early=True)
    assert v is not None and v[0] == "SHORT_NO_CIRCUIT_BAND"          # past time / price / range / gap, stopped by the circuit gate
    assert short_gates.evaluate(f, at(9, 25)) [0] == "SHORT_TOO_EARLY"  # the same facts without the window are still refused at 09:25


RNG = [1000.0, 1200.0, 800.0, 1000.0, 1000.0]


def test_rising_volume_needs_three_bars_above_the_range_average():
    assert squeeze_guard.rising_volume(RNG, [1500, 1600]) is None
    assert squeeze_guard.rising_volume(RNG, [900, 950, 1000]) is None
    assert squeeze_guard.rising_volume(RNG, [1500, 1800, 2100]) == 1.8
    assert squeeze_guard.rising_volume([], [1500, 1800, 2100]) is None
    assert squeeze_guard.rising_volume(RNG, [3000, 3000, 3000, 1500, 1600, 1700]) is None     # the last 3 bars are quieter than the 3 before


def test_guard_blocks_a_squeeze_and_names_it():
    r = squeeze_guard.evaluate(price=101.0, or_high=100.5, range_vols=RNG, after_vols=[1500, 1800, 2100], tape=1)
    assert r.startswith("SQUEEZE:") and "100.50" in r and "1.8x" in r


def test_every_leg_is_needed():
    base = dict(price=101.0, or_high=100.5, range_vols=RNG, after_vols=[1500, 1800, 2100], tape=1)
    assert squeeze_guard.evaluate(**dict(base, price=100.0)) is None            # below the range high
    assert squeeze_guard.evaluate(**dict(base, tape=0)) is None                 # tape unknown
    assert squeeze_guard.evaluate(**dict(base, tape=-1)) is None                # sellers
    assert squeeze_guard.evaluate(**dict(base, after_vols=[900, 950, 1000])) is None
    assert squeeze_guard.evaluate(**dict(base, or_high=None)) is None


def test_s4_is_never_blocked():
    assert squeeze_guard.evaluate(price=101.0, or_high=100.5, range_vols=RNG, after_vols=[1500, 1800, 2100], tape=1, s4=True) is None


def _bars(open_ts, vols_range, vols_after):
    rows = []
    for i, v in enumerate(vols_range):
        rows.append([open_ts + 60 * i, 100, 100.5, 99, 100, v])
    for i, v in enumerate(vols_after):
        rows.append([open_ts + 300 + 60 * i, 100, 102, 100, 101, v])
    return rows


TAPE = {"tape": {"reliable": True, "buy_pct_10m": 62.0, "delta_agrees": True}}


def test_reason_reads_bars_and_tape_end_to_end(monkeypatch):
    from orders import orb_shadow
    orb_shadow._bar_ranges.clear()
    now = at(9, 40)
    open_ts = now.replace(hour=9, minute=15, second=0, microsecond=0).timestamp()
    bars = _bars(open_ts, RNG, [1500, 1800, 2100])
    r = squeeze_guard.reason(symbol="AAA", price=101.0, now=now, tape_json=json.dumps(TAPE), bars=bars)
    assert r and r.startswith("SQUEEZE") and "100.50" in r                       # the range high is the highest high of the 09:15-09:20 bars
    assert squeeze_guard.reason(symbol="AAA", price=101.0, now=now, tape_json=None, bars=bars) is None
    assert squeeze_guard.reason(symbol="AAA", price=101.0, now=now, tape_json=TAPE, bars=bars, s4=True) is None
    monkeypatch.setattr(config, "SHORT_SQUEEZE_GUARD_ENABLED", False, raising=False)
    assert squeeze_guard.reason(symbol="AAA", price=101.0, now=now, tape_json=TAPE, bars=bars) is None
    orb_shadow._bar_ranges.clear()


def test_reason_never_raises_on_junk():
    assert squeeze_guard.reason(symbol="AAA", price="x", now="y", tape_json=3, bars=7) is None


def test_the_plan_uses_the_early_window_and_the_guard():
    src = (ROOT / "orders" / "short_entry.py").read_text()
    assert "short_gates.early_open(now)" in src and "short_gates.evaluate(facts, now, **_kw)" in src and '("early", True)' in src
    assert 'if early and not _is_s4:' in src and "squeeze_guard.reason(" in src and '"early": early' in src
    assert src.index("setup_short.decide(") < src.index("compute_levels(cand.pct_change")
    assert src.index("setup_short.decide(") > src.index("short_gates.evaluate(facts, now")
    cyc = src[src.index("async def run_short_cycle("):]
    assert "short_gates.time_window({}, now, early=short_gates.early_open(now))" in cyc
    assert "orb_shadow" not in src                                              # test_e119 keeps the shadow-only path free of the ORB module
