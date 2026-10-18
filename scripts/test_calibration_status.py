"""Tests for scripts/calibration_status.py (group 434)."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import calibration_status as c  # noqa: E402


def _hist(d, n):
    d.mkdir(parents=True, exist_ok=True)
    for i in range(n):
        (d / f"S{i}.json").write_text("{}")
    (d / "NIFTY.json").write_text("{}")                       # the benchmark does not count


def _ab(d, verdict="no_edge", signals=100, edge=0.3):
    d.mkdir(parents=True, exist_ok=True)
    (d / "ab_report.json").write_text(json.dumps({"verdict": verdict, "signals": {"baseline": signals}, "edge_pp": edge}))


def _manifest(path, symbols, drawn=None):
    path.write_text(json.dumps({"drawn": drawn if drawn is not None else len(symbols), "symbols": symbols}))


def test_hist_guard_fails_a_mostly_failed_download(tmp_path, capsys):
    _hist(tmp_path / "hist_7", 3)
    _manifest(tmp_path / "m7.json", [f"S{i}" for i in range(300)])
    assert c.main(["--hist", str(tmp_path / "hist_7"), "--manifest", str(tmp_path / "m7.json"), "--label", "seed_7"]) == 1
    assert "3 symbol file(s), need 180" in capsys.readouterr().out
    _hist(tmp_path / "hist_11", 200)
    _manifest(tmp_path / "m11.json", [f"S{i}" for i in range(300)])
    assert c.main(["--hist", str(tmp_path / "hist_11"), "--manifest", str(tmp_path / "m11.json")]) == 0


def test_hist_guard_without_a_manifest_uses_50_and_min_symbols_overrides(tmp_path):
    _hist(tmp_path / "h", 30)
    assert c.main(["--hist", str(tmp_path / "h")]) == 1
    assert c.main(["--hist", str(tmp_path / "h"), "--min-symbols", "20"]) == 0
    assert c.main(["--hist", str(tmp_path / "missing")]) == 1


def test_run_all_complete_exits_zero(tmp_path, capsys):
    for s in ("7", "11", "13"):
        _hist(tmp_path / f"hist_{s}", 60)
        _ab(tmp_path / f"ab_{s}")
    assert c.main(["--out-dir", str(tmp_path), "--min-symbols", "50"]) == 0
    assert "all seeds complete" in capsys.readouterr().out


def test_run_names_the_seeds_to_rerun(tmp_path, capsys):
    _hist(tmp_path / "hist_7", 3)                                  # download failed
    _hist(tmp_path / "hist_11", 60)                                # no A/B report
    _hist(tmp_path / "hist_13", 60)
    _ab(tmp_path / "ab_13", signals=10)                            # too few signals
    rc = c.main(["--out-dir", str(tmp_path)])
    out = capsys.readouterr().out
    assert rc == 1 and 'SEEDS="7 11 13"' in out and "no A/B report" in out and "only 10 baseline signals" in out and "history has 3 symbols" in out
    assert "0 complete seed(s): the gates need at least 2" in out


def test_no_data_verdict_is_incomplete(tmp_path):
    _hist(tmp_path / "hist_7", 60)
    _ab(tmp_path / "ab_7", verdict="no_data")
    r = c.check_seed(tmp_path, "7", 50, 30)
    assert r["status"] == "INCOMPLETE" and "no_data" in r["reason"]


def test_identical_samples_are_flagged(tmp_path):
    syms = [f"S{i}" for i in range(60)]
    for s in ("7", "11"):
        _hist(tmp_path / f"hist_{s}", 60)
        _ab(tmp_path / f"ab_{s}")
        _manifest(tmp_path / f"sample_manifest_{s}.json", syms)
    res = c.check_run(tmp_path, ["7", "11"])
    assert res["complete"] and any("identical symbols" in n for n in res["notes"])


def test_one_complete_seed_still_warns_that_two_are_needed(tmp_path):
    _hist(tmp_path / "hist_7", 60)
    _ab(tmp_path / "ab_7")
    res = c.check_run(tmp_path, ["7"], 50)
    assert res["complete"] is True and any("at least 2" in n for n in res["notes"])
