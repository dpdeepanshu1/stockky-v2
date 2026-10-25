"""E141: swing_shadow_report scores the saved rankings at 10 and 20 sessions too. Offline, stdlib only."""
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import swing_shadow_report as sr  # noqa: E402


def _snaps(n, start=date(2026, 11, 2), gap_after=None):
    out, d, px = [], start, 100.0
    for i in range(n):
        while d.weekday() >= 5:
            d += timedelta(days=1)
        if gap_after is not None and i == gap_after:
            d += timedelta(days=30)                     # a month of missed runs
            while d.weekday() >= 5:
                d += timedelta(days=1)
        out.append({"day": d.isoformat(), "rows": {"A": {"last_close": px, "rank": 1, "screen": True}, "B": {"last_close": 100.0, "rank": 2, "screen": False}}})
        px *= 1.01
        d += timedelta(days=1)
    return out


def test_horizons_include_10_and_20():
    assert sr.HORIZONS == (1, 3, 5, 10, 20) and sr.MAX_GAP_DAYS[10] == 18 and sr.MAX_GAP_DAYS[20] == 34


def test_20_session_forward_return_is_close_to_close_twenty_saved_days_later():
    rows = sr.forward_rows(_snaps(30), 20, 0.3, 0.1)
    a = [r for r in rows if r["sym"] == "A"]
    assert len(a) == 10 and all(abs(r["gross_pct"] - (1.01 ** 20 - 1) * 100) < 0.01 for r in a)
    assert abs(a[0]["net_pct"] - (a[0]["gross_pct"] - 0.4)) < 0.001


def test_a_month_of_missed_runs_is_not_counted_as_20_sessions():
    rows = sr.forward_rows(_snaps(30, gap_after=5), 20, 0.3, 0.1)
    assert all(r["entry_day"] >= "2026-11-09" for r in rows) and len([r for r in rows if r["sym"] == "A"]) < 10


def test_build_report_accepts_hold_20_and_says_not_enough_data_when_short():
    rep = sr.build_report(_snaps(30), 20, 20000.0, 0.1, 30, 20, 50)
    assert rep["hold"] == 20 and rep["verdict"] == "NOT_ENOUGH_DATA" and "20" in rep["horizons"]


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)
