"""Round 260: a rolling backtest with no usable India VIX cannot look good. The live engine refuses to trade on an unknown VIX; the sim read a missing value as 15, so the VIX gate never
blocked a volatile day. data_quality now counts the days with no VIX (none, or older than VIX_STALE_DAYS) and the verdict drops to NOT_PROVEN above MAX_VIX_UNKNOWN_PCT."""
from __future__ import annotations

from datetime import datetime

from backtest import report, rolling_sim
from fno.params import Params
from fno.provider import IST
from test_backtests import build_world
from test_round246 import book_from, make_files

P = Params(min_score=30, max_trades_per_day=2)


def vix_on(daily, level=14.0, until=None):
    """A daily VIX close on every TRADING day of the world (so a weekend is a 3-day gap, like the real file); optionally only up to `until` (ISO date)."""
    out = {}
    for c in daily:
        k = datetime.fromtimestamp(c["t"], IST).date().isoformat()
        if until is None or k <= until:
            out[k] = level
    return out


def test_a_full_vix_file_counts_no_unknown_day_and_the_weekend_gap_is_not_stale():
    daily, intr = build_world()
    r = rolling_sim.run("NIFTY", daily, intr, book_from(make_files(intr)), vix_by_day=vix_on(daily), p=P)
    dq = r["data_quality"]
    assert dq["vix_unknown_days"] == 0 and dq["vix_unknown_pct"] == 0.0 and dq["ok"] is True
    line = report.render_text(r).split("data quality:")[1].splitlines()[0]
    assert "days without a usable VIX 0 (0.0%)" in line and line.rstrip().endswith("OK")


def test_no_vix_at_all_is_not_proven_and_says_so():
    daily, intr = build_world()
    r = rolling_sim.run("NIFTY", daily, intr, book_from(make_files(intr)), p=P)                  # strikes are complete: only the VIX is missing
    dq = r["data_quality"]
    assert dq["unmanaged_pct"] <= rolling_sim.MAX_UNMANAGED_PCT and dq["vix_unknown_pct"] == 100.0 and dq["vix_unknown_days"] == r["days"] > 0 and dq["ok"] is False
    assert r["verdict"]["status"] == "NOT_PROVEN" and "India VIX" in r["verdict"]["reasons"][0] and "--vix" in r["verdict"]["reasons"][0]
    assert "NOT OK" in report.render_text(r)


def test_a_vix_file_that_stops_early_is_stale_for_the_later_days():
    daily, intr = build_world()
    first_tested = datetime.fromtimestamp(intr[0]["t"], IST).date().isoformat()
    vix = vix_on(daily, until=sorted(k for k in vix_on(daily) if k < first_tested)[-12])          # the last close is about two weeks before the first tested day
    r = rolling_sim.run("NIFTY", daily, intr, book_from(make_files(intr)), vix_by_day=vix, p=P)
    dq = r["data_quality"]
    assert dq["vix_unknown_days"] == r["days"] and dq["ok"] is False and r["verdict"]["status"] == "NOT_PROVEN"


def test_the_gate_itself(monkeypatch):
    daily, intr = build_world()
    monkeypatch.setattr(rolling_sim, "MAX_VIX_UNKNOWN_PCT", -1.0)                                 # any share of unknown days (even 0 %) is over the limit
    r = rolling_sim.run("NIFTY", daily, intr, book_from(make_files(intr)), vix_by_day=vix_on(daily), p=P)
    assert r["data_quality"]["vix_unknown_pct"] == 0.0 and r["data_quality"]["ok"] is False
    assert r["verdict"]["status"] == "NOT_PROVEN" and "India VIX" in r["verdict"]["reasons"][0]
