"""P5: market_state / profile_version on the position-stocks per-trade record and the by_market_state group of /trades/report."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace as NS

from orders import trade_report
from tz_utils import IST


def _t(pnl=10.0, status="TARGET_HIT", **kw):
    opened = datetime(2026, 10, 9, 10, 5, tzinfo=IST).astimezone(timezone.utc)
    closed = datetime(2026, 10, 9, 10, 20, tzinfo=IST).astimezone(timezone.utc)
    d = dict(symbol="ABC", opened_at=opened, closed_at=closed, entry_price=100.0, exit_price=100.0 + pnl / 100, quantity=100,
             status=status, window_source="5m", signal_price=None, realized_pnl=pnl, error_message=None)
    d.update(kw)
    return NS(**d)


def test_record_carries_the_stamp_and_none_when_absent():
    r = trade_report.trade_record(_t(market_state="stressed", profile_version="v7"))
    assert r["market_state"] == "stressed" and r["profile_version"] == "v7"
    r = trade_report.trade_record(_t())               # no attribute at all
    assert r["market_state"] is None and r["profile_version"] is None


def test_report_groups_by_market_state_with_none_bucket():
    rep = trade_report.report([_t(50.0, market_state="normal", profile_version="v7"),
                               _t(60.0, market_state="stressed", profile_version="v7"),
                               _t(55.0)])
    assert set(rep["by_market_state"]) == {"normal", "stressed", "none"}
    assert rep["by_profile_version"]["v7"]["trades"] == 2


def test_empty_report_lists_the_groups():
    rep = trade_report.report([])
    assert rep["by_market_state"] == {} and rep["by_profile_version"] == {}
