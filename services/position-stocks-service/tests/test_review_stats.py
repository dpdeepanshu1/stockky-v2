"""Group 167: orders/review_stats.breakdown and GET /trades/breakdown."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS

import pytest

from orders import review_stats
from tz_utils import IST


def _t(h, m, pnl, window="5m", status="TARGET_HIT", entry=100.0, hi=None, lo=None, closed=True):
    opened = datetime(2026, 10, 5, h, m, tzinfo=IST).astimezone(timezone.utc)
    return NS(opened_at=opened, realized_pnl=pnl, window_source=window, status=status,
              entry_price=entry, max_price_seen=hi, min_price_seen=lo)


def test_empty():
    r = review_stats.breakdown([])
    assert r["trades"] == 0 and r["overall"] is None and r["by_entry_time_ist"] == {}


def test_only_closed_rows_with_pnl_count():
    rows = [_t(10, 0, 5), _t(10, 5, 5, status="OPEN"), _t(10, 5, 5, status="ERROR"),
            _t(10, 5, 5, status="EXIT_LEGS_REJECTED"), _t(10, 5, None)]
    assert review_stats.breakdown(rows)["trades"] == 1


def test_buckets_split_at_half_hour_in_ist():
    rows = [_t(9, 45, -10), _t(9, 59, 20), _t(10, 0, 5), _t(10, 29, -5), _t(10, 30, 7)]
    b = review_stats.breakdown(rows)["by_entry_time_ist"]
    assert list(b) == ["09:30", "10:00", "10:30"]
    assert b["09:30"]["trades"] == 2 and b["09:30"]["total_pnl"] == 10.0 and b["09:30"]["win_rate_pct"] == 50.0
    assert b["10:00"]["trades"] == 2 and b["10:30"]["trades"] == 1


def test_window_and_status_groups_and_overall():
    rows = [_t(10, 0, 10, window="5m", status="TARGET_HIT"), _t(10, 0, -20, window="60m", status="STOP_HIT"),
            _t(10, 0, -5, window="5m", status="STAGNATION_EXIT")]
    r = review_stats.breakdown(rows)
    assert r["by_window"]["5m"]["trades"] == 2 and r["by_window"]["60m"]["total_pnl"] == -20.0
    assert set(r["by_exit_status"]) == {"TARGET_HIT", "STOP_HIT", "STAGNATION_EXIT"}
    assert r["overall"]["total_pnl"] == -15.0 and r["overall"]["wins"] == 1 and r["overall"]["avg_pnl"] == -5.0


def test_excursion_averages():
    rows = [_t(10, 0, 5, hi=102.0, lo=99.0), _t(10, 1, -5, hi=101.0, lo=97.0), _t(10, 2, 1)]
    o = review_stats.breakdown(rows)["overall"]
    assert o["avg_max_gain_pct"] == pytest.approx(1.5) and o["avg_max_drawdown_pct"] == pytest.approx(-2.0)


def test_excursion_none_when_unknown():
    assert review_stats.breakdown([_t(10, 0, 5)])["overall"]["avg_max_gain_pct"] is None
