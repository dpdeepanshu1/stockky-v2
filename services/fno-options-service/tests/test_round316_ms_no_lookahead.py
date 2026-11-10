"""Round 316: stockky_ms never looks ahead and never raises.
(1) swings, BOS and CHOCH found on a prefix of the bars equal the ones a longer history shows for the same bars (`failed` is judged on later bars, so it is left out);
(2) analyze() returns a block, with a technical_quality_score inside 0..100, for flat / tiny-range / gappy / zero-volume / random data of every length."""
from __future__ import annotations

import random

from stockky_ms.atr import atr_series
from stockky_ms.engine import analyze
from stockky_ms.params import MSParams
from stockky_ms.prep import prep
from stockky_ms.structure import scan_structure
from stockky_ms.swings import raw_swings

P = MSParams()


def _rows(kind: str, n: int, rng: random.Random):
    px, rows = 24000.0, []
    for k in range(n):
        if kind == "flat":
            o = h = l = c = px
        elif kind == "tiny":
            o = px; c = px + rng.choice([-0.05, 0.0, 0.05]); h = max(o, c) + 0.01; l = min(o, c) - 0.01
        elif kind == "gappy":
            px *= 1 + rng.choice([0, 0, 0.05, -0.05, 0.12]); o = px; c = px * (1 + rng.uniform(-0.01, 0.01)); h = max(o, c) * 1.002; l = min(o, c) * 0.998
        elif kind == "trend":
            o = px; c = px * 1.004; h = c * 1.001; l = o * 0.999
        else:
            o = px; c = px * (1 + rng.uniform(-0.02, 0.02)); h = max(o, c) * (1 + rng.random() * 0.01); l = min(o, c) * (1 - rng.random() * 0.01)
        rows.append((1700000000.0 + 86400 * k, o, h, l, c, 0 if kind in ("flat", "trend") else rng.randint(0, 10 ** 6)))
        px = c
    return rows


def _scan(rows):
    pr = prep(rows, P)
    bars = pr["bars"]
    atr = atr_series(bars, P.atr_period, P.atr_min_bars, P.corp_action_jump_pct)
    raw = raw_swings(bars, atr, P, "v1", circuit=pr["circuit"])
    return len(bars), raw, scan_structure(bars, atr, raw, pr["gap_event"], pr["circuit"], P)


def _strip(events):
    return [{k: v for k, v in e.items() if k != "failed"} for e in events]


def test_swings_and_breaks_on_a_prefix_equal_those_of_the_longer_history():
    rng = random.Random(11)
    for kind in ("rand", "gappy"):
        for _ in range(8):
            rows = _rows(kind, 220, rng)
            _, full_raw, full = _scan(rows)
            for k in (90, 150, 200):
                n, raw, st = _scan(rows[:k])
                assert raw == [s for s in full_raw if s["confirmed_at"] < n]
                assert _strip(st["bos"]) == [e for e in _strip(full["bos"]) if e["break_idx"] < n]
                assert _strip(st["choch"]) == [e for e in _strip(full["choch"]) if e["break_idx"] < n]


def test_analyze_never_raises_on_awkward_data():
    rng = random.Random(7)
    for kind in ("flat", "tiny", "gappy", "trend", "rand"):
        for n in (10, 60, 130, 260):
            rows = _rows(kind, n, rng)
            for sub in (rows, rows[: n // 2 + 1]):
                b = analyze(sub, P, {"ema20": 24000.0, "vwap": 24010.0})
                assert isinstance(b, dict) and "quality" in b
                sc = b.get("technical_quality_score")
                assert sc is None or 0 <= sc <= 100
