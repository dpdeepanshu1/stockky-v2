"""Round 308: an F&O 'active' flag that has not been refreshed no longer keeps F&O's share reserved."""
from __future__ import annotations

import capital_board as cb

NOW = 1_800_000_000.0


def _put(d, svc, active, age):
    assert cb.write_stats(str(d), svc, net_pnl=0, trades=0, capital_base=1000, exposure=0, active=active, mode="live", now=NOW - age)


def _run(d, **kw):
    return cb.refresh(str(d), mode="shadow", day="2026-11-10", free_cash=100000, now=NOW, **kw)


def test_fresh_active_fno_keeps_its_share(tmp_path):
    for s in ("real_trade", "position_stocks", "fno"):
        _put(tmp_path, s, True, 30)
    doc = _run(tmp_path)
    assert "fno" in doc["active"] and doc["pcts"]["fno"] > 30


def test_stale_active_fno_is_dropped_and_its_share_goes_to_the_others(tmp_path):
    for s in ("real_trade", "position_stocks"):
        _put(tmp_path, s, True, 30)
    _put(tmp_path, "fno", True, 3600)                 # published an hour ago, still says active
    doc = _run(tmp_path)
    assert "fno" not in doc["active"] and doc["pcts"]["fno"] == 0.0 and abs(sum(doc["pcts"].values()) - 100.0) < 1e-6


def test_limit_can_be_widened(tmp_path):
    for s in ("real_trade", "position_stocks"):
        _put(tmp_path, s, True, 30)
    _put(tmp_path, "fno", True, 3600)
    assert "fno" in _run(tmp_path, fno_active_max_age_s=7200.0)["active"]


def test_stock_services_with_old_figures_stay_active(tmp_path):
    _put(tmp_path, "real_trade", True, 86400)          # unchanged rule: a stock service may just be down for the night
    assert "real_trade" in _run(tmp_path)["active"]
