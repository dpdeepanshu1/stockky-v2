"""round 679: scripts/gate_rule_summary.py (the P2 rule of the 23 Nov plan on saved daily gate reads) and the shell guards of gates_daily.sh / check_1530.sh. No network, no docker."""
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))
import gate_rule_summary as g  # noqa: E402


def _row(sym, hhmm, best, worst, close, label):
    return f"{sym:<12}{hhmm:>10}{100.0:>10.2f}{best:>9.2f}{worst:>9.2f}{close:>9.2f}  {label}"


def _day(day, gate, n, ran, close=1.0, ran_worst=-0.3, star=()):
    out = [f"################ gate {gate}  day {day} ################", f"BLOCKED NAMES  day {day}  gate {gate}  ({n} symbols)", "",
           "symbol       waits 1st block  source_tab              label"]
    for i in range(n):
        s = f"S{day[-2:]}{i:02d}"
        out.append(f"{s:<12}{3:>6}{'10:00':>10}  {'surprise' if s in star else 'watchlist':<22}  BUY NOW" + (" *" if s in star else ""))
    out += ["", "symbols by source_tab: watchlist 1", "", "---- Dhan outcome, part 1 of 1 ----", f"{'symbol':<12}{'1st block':>10}{'price':>10}{'best %':>9}{'worst %':>9}{'close %':>9}  label"]
    for i in range(n):
        s = f"S{day[-2:]}{i:02d}"
        out.append(_row(s, "10:00", 2.5, ran_worst, close, "RAN") if i < ran else _row(s, "10:00", 0.5, -0.4, close, "NO_MOVE"))
    return "\n".join(out) + "\n"


def _args(**kw):
    class A:
        charges, stop, min_days, min_names, min_ran_pct = 0.25, 1.5, 3, 60, 15.0
    for k, v in kw.items():
        setattr(A, k, v)
    return A


def test_parse_reads_outcomes_stars_no_data_and_skips_the_recorded_bars_section():
    txt = _day("2026-11-23", "no_setup_window", 3, 1, star={"S2300"})
    txt += f"{'ZZZ':<12}{'10:00':>10}{'':>10}{'':>9}{'':>9}{'':>9}  no_data (DhanApiError: network: ReadTimeout)\n"
    txt += "################ structure gate, recorded bars ################\n" + _row("REC1", "10:00", 9, -1, 9, "RAN") + "\n"
    rows = g.parse(txt)
    assert [r["symbol"] for r in rows] == ["S2300", "S2301", "S2302", "ZZZ"]
    assert rows[0]["label"] == "RAN" and rows[0]["event"] is True and rows[1]["event"] is False
    assert rows[3]["label"] == "no_data" and rows[3]["close"] is None


def test_a_name_in_two_gates_counts_once_in_the_all_group_and_once_per_gate():
    rows = g.parse(_day("2026-11-23", "no_setup_window", 2, 0) + _day("2026-11-23", "structure", 2, 0))
    assert len(g.dedupe(rows, True)) == 4 and len(g.dedupe(rows, False)) == 2


def test_one_day_is_always_keep():
    rows = g.parse(_day("2026-11-23", "no_setup_window", 80, 40, close=2.0))
    s = g.stats(g.dedupe(rows, True), 0.25, 1.5)
    v, fails = g.verdict(s, _args())
    assert v == "KEEP" and any("(a)" in f for f in fails)


def test_three_sessions_enough_names_ran_share_and_close_above_charges_is_a_candidate():
    txt = "".join(_day(d, "no_setup_window", 25, 6, close=0.9) for d in ("2026-11-23", "2026-11-24", "2026-11-25"))
    s = g.stats(g.dedupe(g.parse(txt), True), 0.25, 1.5)
    assert s["days"] == 3 and s["priced"] == 75 and s["ran"] == 18
    assert g.verdict(s, _args())[0] == "CANDIDATE"


def test_a_ran_name_that_dipped_past_the_stop_or_a_close_below_charges_keeps_the_gate():
    base = "".join(_day(d, "no_setup_window", 25, 6, close=0.9, ran_worst=-2.0) for d in ("2026-11-23", "2026-11-24", "2026-11-25"))
    v, fails = g.verdict(g.stats(g.dedupe(g.parse(base), True), 0.25, 1.5), _args())
    assert v == "KEEP" and any("(e)" in f for f in fails)
    low = "".join(_day(d, "no_setup_window", 25, 6, close=0.1) for d in ("2026-11-23", "2026-11-24", "2026-11-25"))
    v, fails = g.verdict(g.stats(g.dedupe(g.parse(low), True), 0.25, 1.5), _args())
    assert v == "KEEP" and any("(d)" in f for f in fails)


def test_the_cli_prints_a_verdict_for_a_saved_file(tmp_path, capsys):
    f = tmp_path / "gates_2026-11-23.txt"
    f.write_text(_day("2026-11-23", "structure", 10, 1))
    assert g.main([str(f)]) == 0
    out = capsys.readouterr().out
    assert "VERDICT: KEEP" in out and "gate structure" in out


def test_shell_scripts_parse_and_refuse_a_future_day():
    for name in ("gates_daily.sh", "check_1530.sh"):
        assert subprocess.run(["bash", "-n", os.path.join(ROOT, "scripts", name)]).returncode == 0
    r = subprocess.run(["bash", os.path.join(ROOT, "scripts", "gates_daily.sh"), "2099-01-01"], capture_output=True, text=True, timeout=30)
    assert r.returncode == 2 and "after today" in r.stderr
