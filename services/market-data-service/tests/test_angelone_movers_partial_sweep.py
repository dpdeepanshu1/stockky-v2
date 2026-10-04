"""group127: /angelone/movers flags a rate-limited (partial) sweep instead of returning it as a
plain status=ok result cached for the full TTL."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main as m


def test_full_sweep_is_not_partial():
    assert m._movers_sweep_coverage(2710, 2710) == (False, 0)


def test_boot_log_case_2610_of_2710_is_partial():
    assert m._movers_sweep_coverage(2610, 2710) == (True, 100)


def test_just_above_threshold_is_not_partial():
    assert m._movers_sweep_coverage(2660, 2710)[0] is False  # 98.2 %


def test_empty_universe_and_negative_fetch_are_safe():
    assert m._movers_sweep_coverage(0, 0) == (False, 0)
    assert m._movers_sweep_coverage(-5, 100) == (True, 100)


def test_partial_ttl_is_short():
    assert 0 < m._MOVERS_PARTIAL_TTL_S <= 300
