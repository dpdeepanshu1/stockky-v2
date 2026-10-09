"""Group 280: orders/trade_report (per-trade measurement) and the signal_price record."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace as NS

import pytest

from orders import trade_report
from tz_utils import IST


def _t(oh, ch, entry, exit_, qty=100, status="TARGET_HIT", tier="5m", signal=None, pnl="auto", msg=None):
    opened = datetime(2026, 10, 9, oh, 5, tzinfo=IST).astimezone(timezone.utc)
    closed = datetime(2026, 10, 9, ch, 20, tzinfo=IST).astimezone(timezone.utc)
    if pnl == "auto":
        pnl = round((exit_ - entry) * qty, 2)
    return NS(symbol="ABC", opened_at=opened, closed_at=closed, entry_price=entry, exit_price=exit_, quantity=qty,
              status=status, window_source=tier, signal_price=signal, realized_pnl=pnl, error_message=msg)


def test_slippage_pct():
    assert trade_report.slippage_pct(NS(signal_price=100.0, entry_price=100.5)) == pytest.approx(0.5)
    assert trade_report.slippage_pct(NS(signal_price=100.0, entry_price=99.0)) == pytest.approx(-1.0)
    for sig in (None, 0, -1):
        assert trade_report.slippage_pct(NS(signal_price=sig, entry_price=100.0)) is None
    assert trade_report.slippage_pct(NS(signal_price=100.0, entry_price=0)) is None


def test_net_is_gross_minus_round_trip_charges_and_decides_win():
    # tiny gross win that charges eat: a loss on net
    r = trade_report.trade_record(_t(10, 10, 100.0, 100.02, qty=10))
    assert r["gross_pnl"] == pytest.approx(0.2) and r["charges"] > 0.2 and r["net_pnl"] < 0
    rep = trade_report.report([_t(10, 10, 100.0, 100.02, qty=10)])
    assert rep["overall"]["wins"] == 0 and rep["overall"]["win_rate_pct"] == 0.0


def test_groups_and_expectancy():
    rows = [_t(10, 10, 100.0, 101.5, status="TARGET_HIT", tier="5m", signal=100.0),
            _t(10, 11, 100.0, 98.0, status="STOP_HIT", tier="5m", signal=99.0),
            _t(13, 14, 100.0, 100.0, status="STAGNATION_EXIT", tier="15m", signal=100.0, pnl=0.0)]
    rep = trade_report.report(rows)
    assert rep["trades"] == 3
    assert set(rep["by_exit_reason"]) == {"TARGET_HIT", "STOP_HIT", "STAGNATION_EXIT"}
    assert set(rep["by_exit_hour_ist"]) == {"10", "11", "14"} and set(rep["by_entry_hour_ist"]) == {"10", "13"}
    assert set(rep["by_tier"]) == {"5m", "15m"}
    stop = rep["by_exit_reason"]["STOP_HIT"]
    assert stop["trades"] == 1 and stop["net_pnl"] < stop["gross_pnl"] and stop["expectancy"] == stop["net_pnl"]
    assert stop["avg_slippage_pct"] == pytest.approx(1.0101, abs=1e-3)
    stag = rep["by_exit_reason"]["STAGNATION_EXIT"]
    assert stag["net_pnl"] < 0                       # a flat exit still pays charges
    o = rep["overall"]
    assert o["net_pnl"] == pytest.approx(sum(v["net_pnl"] for v in rep["by_exit_reason"].values()), abs=0.02)
    assert o["avg_slippage_pct"] is not None and o["slippage_known_trades"] == 3


def test_only_settled_closed_rows_count():
    rows = [_t(10, 10, 100, 101, status="OPEN"), _t(10, 10, 100, 101, status="ERROR"),
            _t(10, 10, 100, 101, status="EXIT_LEGS_REJECTED"), _t(10, 10, 100, 101, pnl=None),
            _t(10, 10, 100, 101, msg="STOP_HIT_PENDING_RECONCILE"), _t(10, 10, 100, 101)]
    assert trade_report.report(rows)["trades"] == 1


def test_empty_and_missing_prices():
    assert trade_report.report([])["overall"] is None
    r = trade_report.trade_record(_t(10, 10, 100.0, 0.0, pnl=5.0))
    assert r is None or r["charges"] is None          # no exit price -> charges unknown, never invented


def test_old_rows_without_signal_price_have_no_slippage():
    rep = trade_report.report([_t(10, 10, 100.0, 101.0)])
    assert rep["overall"]["avg_slippage_pct"] is None and rep["overall"]["slippage_known_trades"] == 0
