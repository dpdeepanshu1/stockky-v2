"""group277: real-trade closed-trade breakdown (entry time, source tab, exit hour, expectancy).
Run: python3 -m pytest tests/test_group277_trade_breakdown.py -q"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from types import SimpleNamespace as NS

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import trade_breakdown as tb


def utc(h, m=0):          # 2026-10-09 h:m UTC  (IST = UTC + 5:30)
    return datetime(2026, 10, 9, h, m, tzinfo=timezone.utc)


def row(pnl, net=None, opened=utc(3, 50), closed=utc(5, 10), tab="watchlist", status="CLOSED"):
    return NS(status=status, realized_pnl=pnl, net_realized_pnl=net, opened_at=opened, closed_at=closed, source_tab=tab)


def test_empty():
    out = tb.breakdown([])
    assert out["trades"] == 0 and out["overall"] is None and out["by_entry_time_ist"] == {}


def test_open_and_pnl_less_rows_ignored():
    out = tb.breakdown([row(5, status="OPEN"), row(None), row(10)])
    assert out["trades"] == 1


def test_expectancy_uses_net_where_known():
    out = tb.breakdown([row(100, net=60), row(-40, net=-55), row(20)])
    o = out["overall"]
    assert o["trades"] == 3 and o["wins"] == 2
    assert o["gross_pnl"] == 80.0 and o["net_pnl"] == 5.0 and o["net_known_trades"] == 2
    assert o["expectancy"] == round((60 - 55 + 20) / 3, 2)


def test_win_is_judged_after_cost():
    out = tb.breakdown([row(3, net=-2)])             # gross win, net loss
    assert out["overall"]["wins"] == 0 and out["overall"]["win_rate_pct"] == 0.0


def test_buckets_are_ist_and_split_30_min():
    rows = [row(1, opened=utc(3, 50)),               # 09:20 IST -> 09:00
            row(1, opened=utc(4, 10)),               # 09:40 IST -> 09:30
            row(1, opened=utc(4, 10))]
    out = tb.breakdown(rows)
    assert out["by_entry_time_ist"]["09:00"]["trades"] == 1
    assert out["by_entry_time_ist"]["09:30"]["trades"] == 2


def test_exit_hour_and_source_groups():
    rows = [row(5, closed=utc(5, 10), tab="a"), row(-5, closed=utc(8, 40), tab=None)]
    out = tb.breakdown(rows)
    assert set(out["by_exit_hour_ist"]) == {"10", "14"}
    assert set(out["by_source_tab"]) == {"a", "?"}


def test_naive_datetimes_are_treated_as_utc():
    r = row(5, opened=datetime(2026, 10, 9, 3, 50), closed=datetime(2026, 10, 9, 5, 10))
    assert "09:00" in tb.breakdown([r])["by_entry_time_ist"]
