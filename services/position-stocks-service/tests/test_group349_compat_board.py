"""group349: the new-engine board adapter (stockky_ms.compat_board) agrees with the legacy screening.market_structure zones / swings."""
from __future__ import annotations

import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from screening import market_structure as ms
from stockky_ms import compat_board as cb

_KEYS = ("side", "top", "bottom", "t", "idx", "bos", "fvg", "valid", "impulse")


def _walk(seed, n=300):
    random.seed(seed)
    px, out = 100.0, []
    for i in range(n):
        o = px
        c = max(5.0, o + random.gauss(0, 0.6) + (random.choice((-1, 1)) * random.uniform(1.5, 3.0) if random.random() < 0.06 else 0.0))
        h = max(o, c) + abs(random.gauss(0, 0.2))
        l = min(o, c) - abs(random.gauss(0, 0.2))
        out.append((1_790_000_000 + i * 300, o, h, l, c, 1000.0 + random.random() * 500))
        px = c
    return out


def _proj(zs):
    return [tuple(z[k] for k in _KEYS) for z in zs]


def test_swings_identical():
    for seed in range(25):
        b = _walk(seed)
        assert cb.swings(b) == ms.swings(b)
        assert cb.swings(b, 3, 3) == ms.swings(b, 3, 3)


def test_atr_identical():
    b = _walk(3)
    assert cb.legacy_atr(b) == ms.atr(b)
    assert cb.legacy_atr(b[:4]) is None and ms.atr(b[:4]) is None


def test_zones_agree_except_for_the_documented_one_bar_rule():
    """The engine looks at the bar right after the displacement candle (legacy skips it), so it may (a) call a zone `tested` that legacy
    still calls `fresh`, and (b) drop a zone that bar already closed through. Everything else must be identical."""
    zones_seen = extra_dropped = 0
    for seed in range(60):
        b = _walk(seed)
        for full in (False, True):
            old = {z["idx"]: z for z in ms.find_zones(b, full_range=full, max_zones=-1)}
            new = {z["idx"]: z for z in cb.find_zones(b, full_range=full, max_zones=-1)}
            assert set(new) <= set(old), (seed, full)  # the adapter never invents a zone
            for k, z in new.items():
                assert _proj([z]) == _proj([old[k]]) and abs(z["atr"] - old[k]["atr"]) < 1e-12
                if z["status"] != old[k]["status"]:
                    assert (old[k]["status"], z["status"]) == ("fresh", "tested")
            for k in set(old) - set(new):  # dropped by the engine: price closed through the zone after the impulse
                z = old[k]
                nxt = [c for c in b if c[0] > z["t"]]
                assert any((c[4] < z["bottom"]) if z["side"] == "bull" else (c[4] > z["top"]) for c in nxt)
                extra_dropped += 1
            zones_seen += len(old)
    assert zones_seen > 20 and extra_dropped < zones_seen * 0.2


def test_max_zones_keeps_the_newest():
    b = _walk(5)
    allz = cb.find_zones(b, max_zones=-1)
    assert cb.find_zones(b, max_zones=2) == allz[-2:]
    assert cb.find_zones(b, max_zones=0) == []


def test_short_and_flat_input():
    assert cb.find_zones([]) == [] and cb.swings([]) == []
    assert cb.find_zones(_walk(1)[:5]) == []
    flat = [(1_790_000_000 + i * 300, 100.0, 100.0, 100.0, 100.0, 0.0) for i in range(50)]
    assert cb.find_zones(flat) == ms.find_zones(flat) == []
