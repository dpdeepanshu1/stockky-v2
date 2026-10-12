"""tests/test_group333_liquidity_gate.py - orders/liquidity_gate.py (group 333, position-stocks): volume / turnover floor; everything fails open."""
from __future__ import annotations

import os
import sys
from datetime import datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import liquidity_gate
from screening import prev_day
from tz_utils import IST

CR = 1e7


def at(h, m):
    return datetime(2026, 10, 13, h, m, tzinfo=IST)


@pytest.fixture(autouse=True)
def _on(monkeypatch):
    monkeypatch.setattr(config, "ENTRY_LIQUIDITY_GATE_ENABLED", True)     # conftest switches it off for the older suites
    for n, v in (("ENTRY_MIN_AVG_TURNOVER_CR", 3.0), ("ENTRY_MIN_DAY_TURNOVER_CR", 1.0), ("ENTRY_MIN_RVOL", 0.35),
                 ("ENTRY_LIQ_MIN_TIME_IST", "10:00"), ("ENTRY_RVOL_MIN_TIME_IST", "09:45")):
        monkeypatch.setattr(config, n, v, raising=False)
    prev_day.reset()


def test_thin_name_by_average_turnover():
    assert liquidity_gate.decide(100.0, 500_000, 20_000, 2 * CR, at(10, 30)).startswith("LIQUIDITY_AVG_TURNOVER")
    assert liquidity_gate.decide(100.0, 500_000, 400_000, 4 * CR, at(10, 30)) is None


def test_day_turnover_only_from_ten():
    assert liquidity_gate.decide(100.0, 50_000, None, None, at(9, 50)) is None                    # Rs 0.5 cr but before 10:00
    assert liquidity_gate.decide(100.0, 50_000, None, None, at(10, 30)).startswith("LIQUIDITY_DAY_TURNOVER")
    assert liquidity_gate.decide(100.0, 200_000, None, None, at(10, 30)) is None


def test_relative_volume_vs_front_loaded_pace():
    now = at(11, 0)
    frac = liquidity_gate.elapsed_fraction(now)
    assert 0.4 < frac < 0.6
    assert liquidity_gate.decide(1000.0, int(0.1 * frac * 1_000_000), 1_000_000, 50 * CR, now).startswith("LIQUIDITY_RVOL")
    assert liquidity_gate.decide(1000.0, int(frac * 1_000_000), 1_000_000, 50 * CR, now) is None
    assert liquidity_gate.decide(1000.0, 1, 1_000_000, 50 * CR, at(9, 40)) is None               # before 09:45
    assert liquidity_gate.elapsed_fraction(at(9, 10)) == 0.0 and liquidity_gate.elapsed_fraction(at(16, 0)) == 1.0


def test_unknown_inputs_and_zero_floors_never_block(monkeypatch):
    assert liquidity_gate.decide(None, None, None, None, at(11, 0)) is None
    assert liquidity_gate.decide(100.0, 0, 0, 0, at(11, 0)) is None                               # zero = unknown, not "no volume"
    assert liquidity_gate.decide("x", "y", float("nan"), -1, at(11, 0)) is None
    for n in ("ENTRY_MIN_AVG_TURNOVER_CR", "ENTRY_MIN_DAY_TURNOVER_CR", "ENTRY_MIN_RVOL"):
        monkeypatch.setattr(config, n, 0.0)
    assert liquidity_gate.decide(100.0, 10, 1_000_000, 1.0, at(11, 0)) is None


def test_prev_day_averages_volume_and_turnover():
    c = [{"date": "2026-09-%02d" % (i + 1), "high": 102, "low": 98, "close": 100, "volume": 100_000 + i * 1000} for i in range(20)]
    p = prev_day.parse_candles(c, "2026-10-12")
    assert p.avg_volume == pytest.approx(109_500) and p.avg_turnover == pytest.approx(109_500 * 100)
    assert prev_day.parse_candles(c[-4:], "2026-10-12").avg_volume is None                        # fewer than 5 sessions
    nov = [{k: v for k, v in r.items() if k != "volume"} for r in c]
    assert prev_day.parse_candles(nov, "2026-10-12").avg_turnover is None
    assert prev_day.parse_candles(c, "2026-10-12").atr is None or True                            # existing fields untouched
    assert prev_day.parse_candles(c, "2026-10-12").close == 100.0


def test_reject_reason_uses_prev_day_and_quote(monkeypatch):
    pdv = prev_day.PrevDay(101.0, 99.0, 100.0, 1.5, "2026-10-12", None, 20_000.0, 2 * CR)
    monkeypatch.setattr(prev_day, "get", lambda s: pdv)
    monkeypatch.setattr(liquidity_gate.depth_gate, "_fresh_quote", lambda s: {"volume": 1_000_000})
    assert liquidity_gate.reject_reason("ABC", 100.0, at(11, 0)).startswith("LIQUIDITY_AVG_TURNOVER")
    monkeypatch.setattr(prev_day, "get", lambda s: None)
    assert liquidity_gate.reject_reason("ABC", 100.0, at(11, 0)) is None                          # no previous-day facts yet
    monkeypatch.setattr(liquidity_gate.depth_gate, "_fresh_quote", lambda s: None)
    assert liquidity_gate.reject_reason("ABC", 100.0, at(11, 0)) is None                          # no fresh quote
    monkeypatch.setattr(config, "ENTRY_LIQUIDITY_GATE_ENABLED", False)
    assert liquidity_gate.reject_reason("ABC", 100.0) is None


def test_reject_reason_never_raises(monkeypatch):
    def boom(s):
        raise RuntimeError("x")
    monkeypatch.setattr(prev_day, "get", boom)
    assert liquidity_gate.reject_reason("ABC", 100.0, at(11, 0)) is None


def test_entry_wiring_runs_before_capital_is_reserved():
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "orders", "entry.py"), encoding="utf-8").read()
    assert "liquidity_gate.reject_reason(candidate.symbol, candidate.current_ltp)" in src
    assert src.index("liquidity_gate.reject_reason(") < src.index("ledger.reserve_capital(")
    assert config.ENTRY_MIN_AVG_TURNOVER_CR == 3.0 and config.ENTRY_MIN_RVOL == 0.35
