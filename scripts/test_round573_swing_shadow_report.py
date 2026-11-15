"""round 573 (E598): swing_shadow_report.py: one snapshot per day, and stale-bar rows left out. Offline, stdlib only."""
import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import swing_shadow_report as sr  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def _t(day):
    return datetime.fromisoformat(day + "T15:30:00").replace(tzinfo=IST).timestamp()


def _row(sym, close, rank, screen, last_t=None, last_bar_day=None):
    r = {"symbol": sym, "last_close": close, "rank": rank, "screen": screen}
    if last_t is not None:
        r["last_t"] = last_t
    if last_bar_day is not None:
        r["last_bar_day"] = last_bar_day
    return r


def test_stale_row_is_left_out_and_counted():
    rows = [_row("A", 100, 1, True, _t("2026-11-13")), _row("HALTED", 50, 2, True, _t("2026-11-06")), _row("B", 10, 3, False, _t("2026-11-13"))]
    s = sr._snap("2026-11-13", rows)
    assert set(s["rows"]) == {"A", "B"} and s["stale_rows"] == 1


def test_row_without_bar_day_is_kept_and_last_bar_day_wins():
    rows = [_row("OLD", 10, 1, True), _row("X", 10, 2, True, _t("2026-11-06"), last_bar_day="2026-11-13"), _row("Y", 10, 3, True, _t("2026-11-13"), last_bar_day="2026-11-12")]
    s = sr._snap("2026-11-13", rows)
    assert set(s["rows"]) == {"OLD", "X"} and s["stale_rows"] == 1


def test_two_files_of_one_day_make_one_snapshot():
    with tempfile.TemporaryDirectory() as d:
        for name, close in (("swing_snap_2026-11-13.json", 100), ("swing_snap_2026-11-13_copy.json", 101), ("swing_snap_2026-11-16.json", 102)):
            Path(d, name).write_text(json.dumps({"asof_bar_date": name.split("_")[2][:10], "rows": [_row("A", close, 1, True)]}))
        snaps = sr.load_snapshots(Path(d))
        assert [s["day"] for s in snaps] == ["2026-11-13", "2026-11-16"]
        assert snaps[0]["rows"]["A"]["last_close"] == 101        # the last file of the day wins (sorted by name)
        rows = sr.forward_rows(snaps, 1, 0.0, 0.0)
        assert len(rows) == 1 and round(rows[0]["gross_pct"], 2) == round((102 / 101 - 1) * 100, 2)


def test_report_and_markdown_name_the_left_out_stock_days():
    snaps = [sr._snap("2026-11-13", [_row("A", 100, 1, True, _t("2026-11-13")), _row("H", 5, 2, True, _t("2026-11-06"))]),
             sr._snap("2026-11-16", [_row("A", 101, 1, True, _t("2026-11-16"))])]
    rep = sr.build_report(snaps, 1, 20000.0, 0.1, 30, 20, 0)
    assert rep["stale_rows_left_out"] == 1
    assert "1 stock-days left out" in sr.to_markdown(rep)


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
