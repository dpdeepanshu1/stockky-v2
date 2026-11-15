"""round 574 (E599): records_report.py counts every stop spelling in the stop-hit / overshoot line. Pure; synthetic records."""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import records_report as rr  # noqa: E402


def rec(pid, ret, **kw):
    d = {"position_id": pid, "mode": "REAL", "symbol": f"S{pid}", "return_pct": ret, "gross_pnl": ret * 10, "net_pnl": ret * 10 - 5, "charges": 5,
         "opened_at": "2026-11-12T04:00:00+00:00", "closed_at": "2026-11-12T09:30:00+00:00", "exit_reason": "eod_squareoff", "hold_bucket": "same_day"}
    d.update(kw)
    return d


def test_is_stop_hit_spellings():
    for r in ("stop_hit", "STOP_HIT", "broker_stop", "BROKER_STOP", "emergency_gap_down", "stop_loss", "STOP_BREACH_EXIT", "stop", "broker_stop:SL-M"):
        assert rr.is_stop_hit(r), r
    for r in ("time_stop", "trail_stop_approx", "eod_squareoff", "target_hit", "", None, "stopwatch"):
        assert not rr.is_stop_hit(r), r


def test_broker_stop_and_gap_exits_enter_the_overshoot_line():
    a = rec(1, -1.4, exit_reason="stop_hit", stop_pct=1.0, entry_price=100.0, exit_price=99.1, held_days=0)
    b = rec(2, -4.5, exit_reason="broker_stop", stop_pct=1.0, entry_price=100.0, exit_price=98.0, held_days=0)      # slow fill: 2.0 vs 1.0
    c = rec(3, -6.0, exit_reason="emergency_gap_down", stop_pct=1.0, entry_price=100.0, exit_price=95.0, held_days=1)   # overnight gap
    d = rec(4, 3.0, exit_reason="trail_stop_approx", stop_pct=1.0, entry_price=100.0, exit_price=103.0)             # a profit trail: not a stop hit
    sh = rr.build_report([a, b, c, d])["stop_hits"]
    assert sh["n"] == 3 and sh["with_stop_pct"] == 3 and sh["n_beyond"] == 2 and sh["n_inside"] == 1
    assert sh["max_overshoot_pct"] == 4.0 and sh["mean_overshoot_pct"] == 2.5
    assert sh["n_overnight"] == 1 and sh["n_beyond_overnight"] == 1
    assert sh["by_reason"] == {"broker_stop": 1, "emergency_gap_down": 1, "stop_hit": 1}


def test_markdown_names_the_spellings_only_when_there_are_several():
    one = rr.to_markdown(rr.build_report([rec(1, -2.0, exit_reason="stop_hit", stop_pct=1.0)]))
    assert "Stop hits: 1;" in one and "(stop_hit" not in one
    many = rr.to_markdown(rr.build_report([rec(1, -2.0, exit_reason="stop_hit", stop_pct=1.0), rec(2, -2.0, exit_reason="broker_stop", stop_pct=1.0)]))
    assert "Stop hits: 2 (broker_stop 1, stop_hit 1)" in many


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
