"""F&O round 585: the candle-range 'median' of the surge and expiry strategies is a true median; the bar interval keeps its upper-middle pick.
Pure; no fixtures (runs with or without pytest)."""
from fno import strategies as S


def test_true_median_even_odd_empty():
    assert S._true_median([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert S._true_median([5.0, 1.0, 3.0]) == 3.0
    assert S._true_median([]) == 0.0
    assert S._true_median([7.0]) == 7.0


def test_interval_pick_still_returns_a_gap_that_occurs():
    assert S._median([300.0, 86400.0]) in (300.0, 86400.0)           # never the 43350 s mean
    assert S._median([300.0, 300.0, 300.0, 86400.0]) == 300.0
    assert S._median([]) == 0.0


def test_range_yardstick_uses_the_true_median():
    import inspect
    src = inspect.getsource(S)
    assert src.count("med = _true_median(") == 2 and "med = _median(" not in src


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok 3")
