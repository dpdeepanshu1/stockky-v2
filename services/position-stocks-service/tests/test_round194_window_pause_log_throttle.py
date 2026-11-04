"""Round 194: the 'window(s) paused for losing' line is not repeated every cycle."""
from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

import config
from orders import window_health as wh


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    wh.reset()
    monkeypatch.setattr(config, "WINDOW_PAUSE_LOG_EVERY_S", 600, raising=False)
    yield
    wh.reset()


def _cands():
    return [SimpleNamespace(symbol="AAA", window_label="day"), SimpleNamespace(symbol="BBB", window_label="5m")]


def _msgs(caplog, level):
    return [r for r in caplog.records if "paused for losing" in r.getMessage() and r.levelno == level]


def test_same_pause_logs_info_once_then_debug(monkeypatch, caplog):
    monkeypatch.setattr(wh, "disabled_windows", lambda db: {"day": "day 2/8 wins"})
    with caplog.at_level(logging.DEBUG, logger="position-stocks-window-health"):
        for _ in range(5):
            kept, dropped = wh.filter_candidates(None, _cands())
            assert dropped == ["AAA:day"] and len(kept) == 1
    assert len(_msgs(caplog, logging.INFO)) == 1
    assert len(_msgs(caplog, logging.DEBUG)) == 4


def test_changed_reason_logs_info_again(monkeypatch, caplog):
    state = {"r": "day 2/8 wins"}
    monkeypatch.setattr(wh, "disabled_windows", lambda db: {"day": state["r"]})
    with caplog.at_level(logging.DEBUG, logger="position-stocks-window-health"):
        wh.filter_candidates(None, _cands())
        state["r"] = "day 2/9 wins"
        wh.filter_candidates(None, _cands())
    assert len(_msgs(caplog, logging.INFO)) == 2


def test_zero_interval_logs_every_time(monkeypatch, caplog):
    monkeypatch.setattr(config, "WINDOW_PAUSE_LOG_EVERY_S", 0, raising=False)
    monkeypatch.setattr(wh, "disabled_windows", lambda db: {"day": "x"})
    with caplog.at_level(logging.DEBUG, logger="position-stocks-window-health"):
        for _ in range(3):
            wh.filter_candidates(None, _cands())
    assert len(_msgs(caplog, logging.INFO)) == 3


def test_blank_interval_falls_back_to_default(monkeypatch):
    monkeypatch.setattr(config, "WINDOW_PAUSE_LOG_EVERY_S", "", raising=False)
    assert wh._log_every_s() == 600.0
    monkeypatch.setattr(config, "WINDOW_PAUSE_LOG_EVERY_S", "abc", raising=False)
    assert wh._log_every_s() == 600.0
