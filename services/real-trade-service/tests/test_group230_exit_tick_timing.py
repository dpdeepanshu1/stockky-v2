"""group230 (log review item 4): a slow exit tick (exit evaluation + reconcile) is logged once, with the split."""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from execution import auto_pilot as ap


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("EXIT_TICK_WARN_S", raising=False)
    ap._exit_tick_last_warn.clear()
    yield
    ap._exit_tick_last_warn.clear()


def test_fast_tick_does_not_warn(caplog):
    with caplog.at_level("WARNING"):
        assert ap._note_exit_tick_timing("REAL", 3.0, 2.0, now_m=1000.0) is False
    assert not caplog.records


def test_slow_tick_warns_with_the_split(caplog):
    with caplog.at_level("WARNING"):
        assert ap._note_exit_tick_timing("REAL", 12.0, 6.0, now_m=1000.0) is True
    msg = caplog.records[-1].getMessage()
    assert "exit tick REAL took 18.0s" in msg and "exit evaluation 12.0s" in msg and "reconcile 6.0s" in msg


def test_warning_is_throttled_per_mode():
    assert ap._note_exit_tick_timing("REAL", 20.0, 0.0, now_m=1000.0) is True
    assert ap._note_exit_tick_timing("REAL", 20.0, 0.0, now_m=1030.0) is False
    assert ap._note_exit_tick_timing("DEMO", 20.0, 0.0, now_m=1030.0) is True       # other mode is independent
    assert ap._note_exit_tick_timing("REAL", 20.0, 0.0, now_m=1061.0) is True


def test_env_threshold_and_off_switch(monkeypatch):
    monkeypatch.setenv("EXIT_TICK_WARN_S", "5")
    assert ap._note_exit_tick_timing("REAL", 4.0, 2.0, now_m=1.0) is True
    monkeypatch.setenv("EXIT_TICK_WARN_S", "0")
    assert ap._note_exit_tick_timing("DEMO", 99.0, 99.0, now_m=1.0) is False
    monkeypatch.setenv("EXIT_TICK_WARN_S", "abc")
    assert ap._exit_tick_warn_s() == 15.0


def test_bad_inputs_never_raise():
    assert ap._note_exit_tick_timing("REAL", "x", None) is False
