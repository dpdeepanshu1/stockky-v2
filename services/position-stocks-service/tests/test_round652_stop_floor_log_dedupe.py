"""Round 652: the "adaptive: <SYM> stop floor" line is INFO once per (symbol, numbers to 0.1) per 5 minutes, otherwise DEBUG (it ran on every scan cycle).
The returned levels are unchanged. httpx / sqlalchemy are stubbed when missing (this sandbox); the VM image has them."""
from __future__ import annotations

import logging
import os
import sys
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
for _n in ("httpx", "sqlalchemy", "sqlalchemy.orm", "sqlalchemy.exc", "sqlalchemy.ext", "sqlalchemy.ext.declarative"):
    try:
        __import__(_n)
    except Exception:  # noqa: BLE001
        sys.modules[_n] = mock.MagicMock(name=_n)

import pytest  # noqa: E402

from orders import adaptive  # noqa: E402


@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    monkeypatch.setattr(adaptive, "_cap_logged", {})
    monkeypatch.setattr(adaptive.config, "ADAPTIVE_STOP_FLOOR_ENABLED", True, raising=False)
    monkeypatch.setattr(adaptive.config, "ADAPTIVE_STOP_DAILY_ATR_FRAC", 0.5, raising=False)
    monkeypatch.setattr(adaptive.config, "ADAPTIVE_STOP_M1_MULT", 0.0, raising=False)
    monkeypatch.setattr(adaptive.config, "ADAPTIVE_STOP_FLOOR_MAX_PCT", 0.0, raising=False)
    from screening import prev_day
    monkeypatch.setattr(prev_day, "peek", lambda s: mock.Mock(atr_pct=_ATR[0]))
    from orders import entry_timing
    monkeypatch.setattr(entry_timing, "live_avg_1m_range_pct", lambda s: None)


_ATR = [6.0]


def _levels(stop=2.0):
    return adaptive.AdaptiveLevels(target_pct=4.0, stop_pct=stop, target_price=0, stop_price=0, breakeven_trigger_pct=1.0,
                                   range_regime="mid", range_position=0.5, atr_proxy_pct=1.0, basis="legacy", side="LONG")


def _floor_records(caplog):
    return [r for r in caplog.records if "stop floor" in r.getMessage()]


def test_repeat_calls_log_info_once(caplog):
    caplog.set_level(logging.DEBUG, logger=adaptive.logger.name)
    outs = [adaptive.apply_stop_floor(_levels(), 100.0, "KISSHT", "LONG") for _ in range(5)]
    recs = _floor_records(caplog)
    assert [r.levelno for r in recs] == [logging.INFO] + [logging.DEBUG] * 4
    assert len({round(o.stop_pct, 4) for o in outs}) == 1 and outs[0].stop_pct >= 3.0     # levels identical on every call


def test_other_symbol_and_changed_numbers_log_info_again(caplog):
    caplog.set_level(logging.DEBUG, logger=adaptive.logger.name)
    adaptive.apply_stop_floor(_levels(), 100.0, "KISSHT", "LONG")
    adaptive.apply_stop_floor(_levels(), 100.0, "PSB", "LONG")
    _ATR[0] = 8.0
    try:
        adaptive.apply_stop_floor(_levels(), 100.0, "KISSHT", "LONG")
    finally:
        _ATR[0] = 6.0
    assert [r.levelno for r in _floor_records(caplog)] == [logging.INFO, logging.INFO, logging.INFO]


def test_info_again_after_the_window(caplog, monkeypatch):
    caplog.set_level(logging.DEBUG, logger=adaptive.logger.name)
    t = [1000.0]
    import time as _t
    monkeypatch.setattr(_t, "monotonic", lambda: t[0])
    adaptive.apply_stop_floor(_levels(), 100.0, "KISSHT", "LONG")
    t[0] += adaptive._CAP_LOG_REPEAT_S + 1
    adaptive.apply_stop_floor(_levels(), 100.0, "KISSHT", "LONG")
    assert [r.levelno for r in _floor_records(caplog)] == [logging.INFO, logging.INFO]


def test_no_line_and_same_levels_when_stop_already_above_floor(caplog):
    caplog.set_level(logging.DEBUG, logger=adaptive.logger.name)
    lv = _levels(stop=5.0)
    assert adaptive.apply_stop_floor(lv, 100.0, "KISSHT", "LONG") is lv and not _floor_records(caplog)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
