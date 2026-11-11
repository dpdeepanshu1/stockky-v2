"""E375: a triangle / wedge / rectangle breakout must reach `confirmed`. Before, the pattern was only reported while price was within 0.25 ATR of
its lines, and `confirmed` needed price more than 0.25 ATR beyond them, so no such pattern could ever be confirmed (and it vanished on the break).
A break the wrong way for the pattern's bias (a bullish triangle breaking down) means it failed and is not reported."""
from market_structure.patterns import _clean, chart_patterns

_NAMES = ("ascending_triangle", "descending_triangle", "symmetrical_triangle", "rising_wedge", "falling_wedge", "rectangle")


def _ascending_triangle(final):
    """Flat highs at ~110.2, lows rising 1.1 per 8-bar swing, then the closes in `final`."""
    bars, t = [], 0
    for i in range(40):
        c = 100 + 0.05 * i
        bars.append((t, c, c + 0.6, c - 0.6, c + 0.1, 1000))
        t += 60
    for k in range(8):
        low = 100 + k * 1.1
        for j in range(4):
            c = low + (110 - low) * (j + 1) / 4
            o = c - (110 - low) / 4
            bars.append((t, o, min(110, c + 0.2), o - 0.2, c, 1000))
            t += 60
        for j in range(4):
            c = 110 - (110 - low - 1.1) * (j + 1) / 4
            o = c + (110 - low - 1.1) / 4
            bars.append((t, o, o + 0.2, c - 0.2, c, 1000))
            t += 60
    for c in final:
        o = bars[-1][4]
        bars.append((t, o, max(o, c) + 0.2, min(o, c) - 0.2, c, 1000))
        t += 60
    return _clean(bars)


def _found(final):
    return [(x["name"], x["status"]) for x in chart_patterns(_ascending_triangle(final)) if x["name"] in _NAMES]


def test_inside_the_lines_is_forming():
    assert _found([109.0, 109.5]) == [("ascending_triangle", "forming")]


def test_break_in_the_bias_direction_is_confirmed():
    assert _found([110.0, 110.7]) == [("ascending_triangle", "confirmed")]


def test_break_the_wrong_way_is_not_reported():
    assert _found([108.8, 108.4]) == []
