"""round 581: flow_tape_report.saved_pct also measures a STOP_BREACH_EXIT (position-stocks' other stop exit). Plain asserts or pytest."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import flow_tape_report as r  # noqa: E402
from test_flow_tape_report import _ps  # noqa: E402

TX = [{"old_stop": 98.7, "new_stop": 100.2, "old_target": 102.5, "new_target": 101.5}]


def _saved(status, exit_=100.2):
    return r.saved_pct(r.normalise(_ps(1, 1, 0.2, status=status, tape_exit=TX, entry=100.0, exit_=exit_), "ps"))


def test_stop_breach_exit_on_the_tape_stop_is_measured():
    assert abs(_saved("STOP_BREACH_EXIT") - 1.5) < 1e-9


def test_stop_breach_exit_far_from_the_tape_stop_is_still_left_out():
    assert _saved("STOP_BREACH_EXIT", exit_=98.0) is None


def test_stop_hit_and_stop_loss_unchanged_and_non_stops_still_excluded():
    assert abs(_saved("STOP_HIT") - 1.5) < 1e-9
    assert abs(_saved("STOP_LOSS") - 1.5) < 1e-9
    for s in ("TARGET_HIT", "STAGNATION_EXIT", "EOD_SQUAREOFF", "TIME_STOP"):
        assert _saved(s) is None, s


def test_exit_report_counts_the_breach_exit():
    rows = [r.normalise(_ps(1, 1, 0.2, status="STOP_BREACH_EXIT", tape_exit=TX, entry=100.0, exit_=100.2), "ps"),
            r.normalise(_ps(2, 1, 0.2, status="STOP_HIT", tape_exit=TX, entry=100.0, exit_=100.2), "ps")]
    rep = r.exit_report([dict(x, tightens=1) for x in rows], 1, 1)
    assert rep["stop_exits_after_tighten"] == 2 and abs(rep["avoided_loss_upper_bound_pct_total"] - 3.0) < 1e-9


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
    print("ok 4")
