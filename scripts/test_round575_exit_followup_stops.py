"""round 575 (E600): exit_followup_study.py reads every stop spelling of both services. Offline, stdlib only."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import exit_followup_study as s  # noqa: E402

T0 = 1_800_000_000.0


def _rec(pid, **kw):
    base = {"mode": "REAL", "position_id": pid, "symbol": "X", "entry_price": 100.0, "exit_price": 99.0, "target_price": 102.5, "stop_price": 98.7,
            "opened_at": T0, "closed_at": T0 + 300, "exit_reason": "broker_stop"}
    base.update(kw)
    return base


def test_status_is_normalised():
    assert s._status({"exit_reason": "broker_stop"}) == "BROKER_STOP"
    assert s._status({"exit_reason": "broker_stop:SL-M"}) == "BROKER_STOP"
    assert s._status({"status": " stop_loss "}) == "STOP_LOSS"


def test_d2_counts_broker_stop_gap_and_stop_loss_not_a_trail_or_a_time_stop():
    recs = [_rec(1, exit_reason="stop_hit"), _rec(2, exit_reason="broker_stop"), _rec(3, exit_reason="emergency_gap_down", closed_at=T0 + 3600),
            _rec(4, mode="PS", exit_reason="STOP_LOSS"), _rec(5, exit_reason="trail_stop_approx", exit_price=101.0), _rec(6, exit_reason="time_stop"),
            _rec(7, exit_reason="broker_stop", exit_price=100.5)]               # a stop order that filled ABOVE entry: trailed, not a stop-out at a loss
    out = s.study_early_stopouts(recs, early_min=10.0)
    assert out["REAL"] == {"stop_outs": 3, "early": 2}
    assert out["PS"] == {"stop_outs": 1, "early": 1}


def test_trail_population_includes_broker_stop_above_entry_and_trail_stop_approx():
    assert s.classify_exit(_rec(1, exit_reason="broker_stop", exit_price=101.0)) == "trail"
    assert s.classify_exit(_rec(2, exit_reason="trail_stop_approx", exit_price=101.0)) == "trail"
    assert s.classify_exit(_rec(3, exit_reason="broker_stop", exit_price=99.0)) is None
    assert s.classify_exit(_rec(4, exit_reason="emergency_gap_down", exit_price=101.0)) is None
    short = _rec(5, exit_reason="broker_stop", exit_price=99.0, side="SHORT")
    assert s.classify_exit(short) == "trail"


def test_median_of_an_even_count_is_the_middle_pair_mean():
    def row(v):
        return {"best_30m_pct": v, "best_60m_pct": v, "best_day_pct": v, "first_level": "none"}
    recs = [_rec(i, exit_reason="STAGNATION_EXIT", exit_price=100.4) for i in range(4)]
    # bars so that every trade's best 30m differs: highs 101, 102, 103, 104 -> median 102.5 % of entry 100 = +2.5
    days = {}
    for i, r in enumerate(recs):
        r["symbol"] = f"S{i}"
        r["closed_at"] = T0 + 1500
        day = s._bars.date_of(T0 + 1500)
        days[f"S{i}"] = {day: {"bars": [(T0 + 1500 + 60, 100, 101 + i, 99.9, 100, 1000)]}}
    res = s.study_followup(recs, days, min_trades=2)
    assert res["stagnation"]["n"] == 4 and abs(res["stagnation"]["best_30m_pct_median"] - 2.5) < 1e-9


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
