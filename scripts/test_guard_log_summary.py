"""Round 596: guard_log_summary counts tokens and shows samples without parsing the format."""
import os, sys, tempfile
sys.path.insert(0, os.path.dirname(__file__))
import guard_log_summary as g


def test_counts_samples_and_zero_tokens():
    lines = ["x WINDOW_WAIT: no new entry before 09:20", "y WINDOW_NO_SETUP: only a setup", "z WINDOW_NO_SETUP again", "q WINDOW_NO_SETUP 3", "r WINDOW_NO_SETUP 4",
             "gate_tag=no_setup_window", "nothing here", "A" * 500 + " PENNY_TICK"]
    r = g.summarize(lines)
    assert r["WINDOW_WAIT"][0] == 1 and r["WINDOW_NO_SETUP"][0] == 4 and len(r["WINDOW_NO_SETUP"][1]) == 3
    assert r["no_setup_window"][0] == 1 and r["PENNY_TICK"][0] == 1 and len(r["PENNY_TICK"][1][0]) == 200
    assert r["MIN_NOTIONAL"] == (0, []) and set(r) == set(g.TOKENS)


def test_one_line_with_two_tokens_counts_for_both():
    r = g.summarize(["WINDOW_NO_SETUP gate_tag=no_setup_window"])
    assert r["WINDOW_NO_SETUP"][0] == 1 and r["no_setup_window"][0] == 1


def test_format_shows_zero_rows():
    txt = g.format_block("real-trade", g.summarize([]))
    assert "== real-trade ==" in txt and all(t in txt for t in g.TOKENS) and "MIN_NOTIONAL" in txt


def test_file_mode_and_missing_file(capsys=None):
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False, encoding="utf-8") as f:
        f.write("a PENNY_TICK b\n")
    assert g.main(["--file", f.name, "--file", f.name + ".nope"]) == 0
    os.unlink(f.name)


def test_missing_docker_is_reported_not_raised(monkeypatch=None):
    lines, err = g.docker_lines("no-such-container-xyz", "1m")
    assert lines == [] and err
