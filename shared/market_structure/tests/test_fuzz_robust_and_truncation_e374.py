"""E374: property tests over random and degenerate series. (1) analyze() never raises and never returns a non-finite number, for any length
and for flat / gappy / low-price / zero-volume data. (2) BOS and CHOCH events found on a prefix are exactly the events a longer run reports
before that prefix ends (truncation invariance), so what a live system saw is what the backtest sees. E373 (one bar through two swings = one
BOS) would have failed (2)'s sibling checks; this keeps that class of bug out."""
from __future__ import annotations

import math
import random

from market_structure.atr import atr_series
from market_structure.engine import analyze
from market_structure.params import MSParams
from market_structure.structure import scan_structure
from market_structure.swings import raw_swings


def _gen(seed: int, n: int, kind: str):
    rnd = random.Random(seed)
    px, out = 100.0, []
    for i in range(n):
        o = px
        if kind == "flat":
            c = o
        elif kind == "gappy":
            c = o * (1 + rnd.choice([0, 0, 0, rnd.uniform(-0.2, 0.2)])) + rnd.gauss(0, 0.5)
        elif kind == "lowpx":
            c = max(0.5, o + rnd.gauss(0, 0.05))
        else:
            c = max(1.0, o + rnd.gauss(0, 1.2))
        h = max(o, c) + abs(rnd.gauss(0, 0.3))
        l = min(o, c) - abs(rnd.gauss(0, 0.3))
        v = 0.0 if kind == "flat" and i % 3 == 0 else 1000 + rnd.random() * 500
        out.append((1_700_000_000 + i * 86400, o, h, l, c, v))
        px = c
    return out


def _nonfinite(x, path=""):
    if isinstance(x, float) and not math.isfinite(x):
        return [path]
    out = []
    if isinstance(x, dict):
        for k, v in x.items():
            out += _nonfinite(v, f"{path}/{k}")
    elif isinstance(x, (list, tuple)):
        for i, v in enumerate(x):
            out += _nonfinite(v, f"{path}[{i}]")
    return out


def test_analyze_never_raises_and_is_finite():
    bad = []
    for kind in ("rand", "flat", "gappy", "lowpx"):
        for seed in range(12):
            for n in (0, 1, 5, 30, 61, 130, 260):
                blk = analyze(_gen(seed, n, kind), ctx={}, stale=False)
                nf = _nonfinite(blk)
                if nf:
                    bad.append((kind, seed, n, nf[:2]))
    assert not bad, bad[:5]


def test_bos_choch_are_truncation_invariant():
    p = MSParams()
    bad = []
    for seed in range(40):
        bars = _gen(seed, 200, "rand")
        n = len(bars)
        atr = [1.5] * n
        full = scan_structure(bars, atr, raw_swings(bars, atr, p), [False] * n, [False] * n, p)
        for cut in (80, 120, 160):
            b, a = bars[:cut], atr[:cut]
            part = scan_structure(b, a, raw_swings(b, a, p), [False] * cut, [False] * cut, p)
            f = sorted((e["break_idx"], e["level_idx"]) for e in full["bos"] + full["choch"] if e["break_idx"] < cut)
            q = sorted((e["break_idx"], e["level_idx"]) for e in part["bos"] + part["choch"])
            if f != q:
                bad.append((seed, cut))
    assert not bad, bad[:5]


def test_no_event_fires_on_a_level_an_earlier_event_bar_already_closed_through():
    """The E373 shape: after an event at bar t, a later same-direction event must not use a swing that was already confirmed before t and
    that bar t's close had already passed (that is the same move counted twice). Checked on random series with a 2-bar swing confirmation."""
    p = MSParams()
    n_ev = 0
    for seed in range(80):
        rnd = random.Random(seed)
        bars, px = [], 100.0
        for i in range(200):                       # big random bodies and ranges, so one bar often clears several swings
            o = px
            c = max(1.0, o + rnd.gauss(0, 2.5))
            bars.append((i * 60, o, max(o, c) + rnd.random(), min(o, c) - rnd.random(), c, 1000.0))
            px = c
        n = len(bars)
        atr = [1.5] * n
        r = scan_structure(bars, atr, raw_swings(bars, atr, p), [False] * n, [False] * n, p)
        ev = sorted(r["bos"] + r["choch"], key=lambda e: e["break_idx"])
        for k, e2 in enumerate(ev):
            n_ev += 1
            for e1 in ev[:k]:
                if e1["dir"] != e2["dir"]:
                    continue
                t = e1["break_idx"]
                close = bars[t][4]
                buf = p.bos_min_atr * 1.5                  # the engine's break buffer (ATR is fixed at 1.5 here); a close inside it did not break the swing
                stale = (close > e2["level"] + buf) if e2["dir"] > 0 else (close < e2["level"] - buf)
                assert not (stale and e2["level_idx"] + p.swing_right < t), (seed, e1["break_idx"], e2["break_idx"], e2["level"], close)
    assert n_ev > 100
