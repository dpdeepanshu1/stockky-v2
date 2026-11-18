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


# ---- round 609: the decisions tables ----
import ast, json, subprocess, types


def test_parse_since():
    assert g.parse_since("30m") == 1800 and g.parse_since("8h") == 28800 and g.parse_since("2d") == 172800 and g.parse_since("3") == 10800
    assert g.parse_since("junk") == 28800 and g.parse_since("") == 28800 and g.parse_since("0m") == 60


def test_summarize_rows_counts_rows_and_distinct_symbols_and_shows_ist_time():
    rows = [["AAA", "no_setup_window | WINDOW_NO_SETUP: only a setup", "2026-11-18T04:32:10"],
            ["AAA", "WINDOW_NO_SETUP again", "2026-11-18T04:33:10"],
            ["BBB", "WINDOW_NO_SETUP x", "2026-11-18T04:34:10"],
            ["CCC", "PENNY_TICK:one tick", "2026-11-18T04:35:10"], ["DDD", None, ""], ["bad"]]
    r = g.summarize_rows(rows)
    assert r["WINDOW_NO_SETUP"][:2] == (3, 2) and r["no_setup_window"][:2] == (1, 1) and r["PENNY_TICK"][:2] == (1, 1)
    assert r["MIN_NOTIONAL"] == (0, 0, []) and set(r) == set(g.TOKENS)
    assert r["WINDOW_NO_SETUP"][2][0].startswith("10:02:10 AAA:")          # 04:32 UTC = 10:02 IST
    assert len(r["WINDOW_NO_SETUP"][2]) == 3 and g.summarize_rows([]) == {t: (0, 0, []) for t in g.TOKENS}


def test_format_db_block_shows_zero_rows_and_an_error_instead_of_zero():
    txt = g.format_db_block("real-trade", g.summarize_rows([]))
    assert "(decisions table)" in txt and "MIN_NOTIONAL" in txt and "0 rows" in txt
    bad = g.format_db_block("real-trade", g.summarize_rows([]), "table read failed")
    assert "table read failed" in bad and "WINDOW_WAIT" not in bad


def test_snippet_is_valid_python_and_filters_by_service_kind():
    ast.parse(g._DB_SNIPPET)
    assert 'GLS_KIND' in g._DB_SNIPPET and "trade_decisions" in g._DB_SNIPPET and "scalp_candidate_log" in g._DB_SNIPPET
    assert '"REAL"' in g._DB_SNIPPET and '"ENTRY"' in g._DB_SNIPPET


def _fake_run(stdout="", returncode=0, stderr=""):
    def run(cmd, **kw):
        run.cmd, run.kw = cmd, kw
        return types.SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)
    return run


def test_db_rows_parses_the_json_line_and_reports_errors():
    real = subprocess.run
    try:
        subprocess.run = _fake_run("noise\n" + json.dumps({"rows": [["AAA", "WINDOW_WAIT x", "2026-11-18T04:00:00"]]}))
        rows, err = g.db_rows("real-trade-service", "real", 3600)
        assert rows == [["AAA", "WINDOW_WAIT x", "2026-11-18T04:00:00"]] and err == ""
        cmd = subprocess.run.cmd
        assert cmd[:5] == ["docker", "compose", "exec", "-T", "-e"] and "real-trade-service" in cmd and "GLS_SECONDS=3600" in cmd
        subprocess.run = _fake_run(json.dumps({"error": "OperationalError: boom"}))
        assert g.db_rows("x", "real", 60) == ([], "table read failed in x: OperationalError: boom")
        subprocess.run = _fake_run("", 1, "no such service")
        assert g.db_rows("x", "real", 60)[1].startswith("docker compose exec x failed")
        subprocess.run = _fake_run("not json")
        assert "unreadable" in g.db_rows("x", "real", 60)[1]
    finally:
        subprocess.run = real


def test_main_source_db_prints_both_tables_and_skips_the_log_read():
    import io, contextlib
    real_rows, real_docker = g.db_rows, g.docker_lines
    g.db_rows = lambda svc, kind, secs: ([["AAA", "no_setup_window", "2026-11-18T04:00:00"]], "")
    g.docker_lines = lambda *a: (_ for _ in ()).throw(AssertionError("logs must not be read with --source db"))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert g.main(["--source", "db", "--since", "2h"]) == 0
        out = buf.getvalue()
        assert "== real-trade (decisions table) ==" in out and "== position-stocks (decisions table) ==" in out
        assert "no_setup_window" in out and "1 rows    1 symbols" in out
    finally:
        g.db_rows, g.docker_lines = real_rows, real_docker
