"""round 583: robustness.py reports the true median of the shuffled drawdowns (even count = middle pair mean). Plain asserts or pytest."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import robustness as rb  # noqa: E402


def test_median_even_and_odd():
    assert rb._median_sorted([1.0, 2.0, 3.0, 4.0]) == 2.5
    assert rb._median_sorted([1.0, 2.0, 9.0]) == 2.0
    assert rb._median_sorted([5.0]) == 5.0


if __name__ == "__main__":
    test_median_even_and_odd()
    print("ok 1")
