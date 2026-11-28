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


if __name__ == "__main__":
    for n, f in list(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
