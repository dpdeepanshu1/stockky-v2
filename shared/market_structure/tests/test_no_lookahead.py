"""T-NLA: no look-ahead. analyze() on bars[:i+1] must not depend on any later bar, replay() must equal analyze() on each prefix, and
appending future bars must never change what an earlier replayed day said."""
import os, sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from _bars import walk
from market_structure.engine import analyze
from market_structure.params import MSParams
from market_structure.replay import default_ctx, replay
from market_structure.summary import flatten_summary


def test_replay_row_equals_analyze_on_the_prefix():
    bars = walk(180, seed=11)
    rows = replay(bars, step=7)
    assert rows and rows[0]["i"] == MSParams().min_bars - 1
    for r in rows[::3]:
        want = flatten_summary(analyze(bars[: r["i"] + 1], ctx=default_ctx(bars[: r["i"] + 1])))
        assert r["summary"] == want and r["ts"] == bars[r["i"]][0]


def test_future_bars_never_change_a_past_replayed_day():
    bars = walk(200, seed=12)
    short = replay(bars[:150], step=10)
    long_ = {r["i"]: r for r in replay(bars, step=10)}
    for r in short:
        assert long_[r["i"]]["summary"] == r["summary"]


def test_a_crash_after_day_i_is_invisible_on_day_i():
    bars = list(walk(160, seed=13))
    crash = bars[:120] + [(t, o * 0.4, h * 0.4, l * 0.4, c * 0.4, v) for (t, o, h, l, c, v) in bars[120:]]
    a = analyze(bars[:120])
    b = analyze(crash[:120])
    assert flatten_summary(a) == flatten_summary(b)


def test_swings_are_confirmed_after_they_form():
    from market_structure.atr import atr_series
    from market_structure.prep import prep
    from market_structure.swings import raw_swings
    p = MSParams()
    pr = prep(walk(200, seed=14), p)
    a = atr_series(pr["bars"], p.atr_period, p.atr_min_bars, p.corp_action_jump_pct)
    assert all(s["confirmed_at"] > s["idx"] for s in raw_swings(pr["bars"], a, p))


def test_replay_is_deterministic():
    bars = walk(150, seed=15)
    assert replay(bars, step=5) == replay(bars, step=5)
