"""E373: a single bar that closes through two swing highs (or lows) is one BOS, not one now and a stale one on the next bar."""
from market_structure.params import MSParams
from market_structure.swings import raw_swings
from market_structure.structure import scan_structure

_UP = [(100, 101, 99, 100.5), (100.5, 102, 100, 101.5), (101.5, 104, 101, 103.5), (103.5, 106, 103, 105.5), (105.5, 106.5, 104.5, 105),
       (105, 105.2, 102, 102.5), (102.5, 103, 100, 100.5), (100.5, 102, 100, 101.5), (101.5, 104, 101, 103.5),
       (103.5, 106, 103, 105), (105, 107.5, 104.5, 106), (106, 106.4, 104, 104.5), (104.5, 105, 103, 103.5), (103.5, 104, 102, 102.5),
       (102.5, 103.5, 101.5, 103), (103, 105, 102.5, 104.5), (104.5, 106, 104, 105.5),
       (105.5, 112, 105.5, 111.5), (111.5, 113, 111, 112.5), (112.5, 114, 112, 113.5), (113.5, 115, 113, 114.5)]


def _run(rows):
    bars = [(i * 60,) + r + (1000,) for i, r in enumerate(rows)]
    atr = [1.5] * len(bars)
    p, n = MSParams(), len(bars)
    return scan_structure(bars, atr, raw_swings(bars, atr, p), [False] * n, [False] * n, p)


def test_one_bar_through_two_swing_highs_is_one_bos():
    r = _run(_UP)
    assert [e["break_idx"] for e in r["bos"]] == [17]


def test_one_bar_through_two_swing_lows_is_one_bos():
    mirror = [(2 * 100 - o, 2 * 100 - l, 2 * 100 - h, 2 * 100 - c) for (o, h, l, c) in _UP]
    mirror = [(o, h, l, c) for (o, h, l, c) in mirror]
    r = _run(mirror)
    assert [e["break_idx"] for e in r["bos"]] == [17]
