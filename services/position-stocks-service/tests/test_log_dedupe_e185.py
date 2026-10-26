"""E185: the same entry-gate skip / target-cap line is logged at INFO once, then at DEBUG until it changes or goes stale."""
from __future__ import annotations

import pytest

import main
from orders import adaptive


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(main, "_skip_logged", {})
    monkeypatch.setattr(adaptive, "_cap_logged", {})


class TestSkipLogDedupe:
    def test_first_time_is_new(self):
        assert main._skip_is_new("ABC", "OPENING_GATE:GAP_UP:+9.85% > +3.00%", now=1000.0) is True

    def test_same_gate_again_is_not_new_even_if_the_numbers_move(self):
        main._skip_is_new("ABC", "RANGE_GATE:NEAR_DAY_HIGH:range_pos=0.96", now=1000.0)
        assert main._skip_is_new("ABC", "RANGE_GATE:NEAR_DAY_HIGH:range_pos=0.97", now=1015.0) is False

    def test_a_different_gate_is_new(self):
        main._skip_is_new("ABC", "RANGE_GATE:NEAR_DAY_HIGH:x", now=1000.0)
        assert main._skip_is_new("ABC", "OPENING_GATE:GAP_UP:x", now=1015.0) is True

    def test_same_gate_is_logged_again_once_stale(self):
        main._skip_is_new("ABC", "RANGE_GATE:X", now=1000.0)
        assert main._skip_is_new("ABC", "RANGE_GATE:X", now=1000.0 + main._SKIP_LOG_REPEAT_S + 1) is True

    def test_symbols_are_independent(self):
        main._skip_is_new("ABC", "RANGE_GATE:X", now=1000.0)
        assert main._skip_is_new("XYZ", "RANGE_GATE:X", now=1001.0) is True


class TestCapLogDedupe:
    def test_same_numbers_once_per_window(self):
        k = (3.91, 2.5, 1.95, 1.5)
        assert adaptive._cap_line_is_new(k, now=1000.0) is True
        assert adaptive._cap_line_is_new(k, now=1010.0) is False
        assert adaptive._cap_line_is_new(k, now=1000.0 + adaptive._CAP_LOG_REPEAT_S + 1) is True

    def test_different_numbers_are_new(self):
        adaptive._cap_line_is_new((3.91, 2.5, 1.95, 1.5), now=1000.0)
        assert adaptive._cap_line_is_new((4.0, 2.5, 2.0, 1.7), now=1001.0) is True


class TestQualityRejectDedupe:
    """E187: the quality-gate reject line and its audit row use the same change-or-5-minutes rule, keyed apart from the entry-gate skips."""
    def test_quality_and_entry_skips_do_not_share_state(self):
        main._skip_is_new("ABC", "RANGE_GATE:X", now=1000.0)
        assert main._skip_is_new("Q:ABC", "fundamental_score 12 < floor 25", now=1001.0) is True

    def test_same_quality_reason_is_not_new_until_stale(self):
        r = "EVENT_CHECK_UNAVAILABLE: the news / earnings service did not answer"
        assert main._skip_is_new("Q:MRPL", r, now=1000.0) is True
        assert main._skip_is_new("Q:MRPL", r, now=1015.0) is False
        assert main._skip_is_new("Q:MRPL", r, now=1000.0 + main._SKIP_LOG_REPEAT_S + 1) is True
