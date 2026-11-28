"""Round 702: gates_daily.sh refuses a weekend like a holiday (nothing saved); ps_skip_reasons names a weekend in its empty-day line."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import ps_skip_reasons as p


def _run(day):
    return subprocess.run(["bash", os.path.join(ROOT, "scripts", "gates_daily.sh"), day], capture_output=True, text=True, cwd=ROOT)


def test_saturday_and_sunday_are_refused_and_save_nothing():
    for day, name in (("2025-11-29", "Saturday"), ("2025-11-30", "Sunday")):
        r = _run(day)
        assert r.returncode == 3 and name in r.stderr and "nothing saved" in r.stderr, (day, r.returncode, r.stderr)
        assert not os.path.exists(os.path.join(ROOT, "gates_out", f"gates_{day}.txt"))


def test_saturday_message_points_at_the_friday():
    assert "bash scripts/gates_daily.sh 2025-11-28" in _run("2025-11-29").stderr
    assert "bash scripts/gates_daily.sh 2025-11-28" in _run("2025-11-30").stderr


def test_future_day_still_exits_2():
    assert _run("2099-01-01").returncode == 2


def test_ps_skip_reasons_names_the_weekend():
    assert "Saturday" in p.format_day("2026-11-28", []) and "Sunday" in p.format_day("2026-11-29", [])
    assert "a holiday or a day the service was off" in p.format_day("2026-11-27", [])


def test_days_counts_sessions_not_calendar_days():            # round 703
    assert p.day_list("2026-11-30", 3) == ["2026-11-30", "2026-11-27", "2026-11-26"]      # Monday: Fri, Thu, not Sun, Sat
    assert p.day_list("2026-11-28", 3) == ["2026-11-28", "2026-11-27", "2026-11-26"]      # a Saturday is shown (with its reason) then Fri, Thu
    assert p.day_list("2026-11-27", 1) == ["2026-11-27"] and p.day_list("2026-11-27", 0) == ["2026-11-27"]
    assert p.day_list("2026-11-24", 2) == ["2026-11-24", "2026-11-23"]


def test_soft_rows_are_broken_out_by_gate_with_placed_count():            # round 704
    from datetime import datetime
    T = datetime(2026, 11, 27, 4, 0)
    rows = [("A", "SOFT", "OPENING_GATE:NEAR_DAY_HIGH:range_pos 0.93 >= 0.9", T), ("A", "SOFT", "OPENING_GATE:NEAR_DAY_HIGH:range_pos 0.95", T),
            ("B", "SOFT", "OPENING_GATE:NEAR_DAY_HIGH:x", T), ("C", "SOFT", "QUALITY_GATE:technical_score 28 < floor 30", T),
            ("A", "ENTERED", "ok", T), ("D", "SKIPPED", "PRICE_GUARD:x", T)]
    r = p.summarize(rows)
    assert r["soft_rows"] == 4 and r["skipped_rows"] == 1
    g = {x["gate"]: x for x in r["soft_gates"]}
    assert g["OPENING_GATE:NEAR_DAY_HIGH"] == {"gate": "OPENING_GATE:NEAR_DAY_HIGH", "rows": 3, "syms": 2, "placed": 1}
    assert g["QUALITY_GATE:technical_score"] == {"gate": "QUALITY_GATE:technical_score", "rows": 1, "syms": 1, "placed": 0}
    assert p.soft_gate("") == "(no reason)" and p.soft_gate("A:B:C") == "A:B"
    txt = p.format_day("2026-11-27", rows)
    assert "SOFT by gate" in txt and "OPENING_GATE:NEAR_DAY_HIGH" in txt
    assert "SOFT by gate" not in p.format_day("2026-11-27", [("D", "SKIPPED", "PRICE_GUARD:x", T)])


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
