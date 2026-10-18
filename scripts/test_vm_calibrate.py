"""group405: scripts/vm_calibrate.sh - DRY_RUN prints the commands and runs nothing; a fake python proves the failure handling."""
import os
import stat
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.join(HERE, "vm_calibrate.sh")


def _run(tmp_path, extra=None, py=None):
    env = dict(os.environ, OUT_DIR=str(tmp_path / "out"), GATEWAY_WAIT_S="0")   # group433: no real gateway in these tests, do not wait for one
    for k in ("SYMBOLS", "SEEDS", "LIMIT", "LIVE_REPORT_URL", "PS_STATUS_URL", "PS_TOKEN", "DRY_RUN", "PYTHON_BIN"):
        env.pop(k, None)
    env.update(extra or {})
    if py:
        env["PYTHON_BIN"] = py
    return subprocess.run(["bash", SCRIPT], capture_output=True, text=True, env=env, timeout=60)


def _fake_py(tmp_path, body):
    p = tmp_path / "fakepy"
    p.write_text("#!/bin/bash\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def test_dry_run_lists_every_step_and_runs_nothing(tmp_path):
    r = _run(tmp_path, {"DRY_RUN": "1", "SEEDS": "7 11", "LIMIT": "150", "PS_STATUS_URL": "http://localhost:8006/board/status"})
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert out.count("fetch_history_for_backtest.py") == 2 and out.count("ms_ab_report.py") == 2
    assert "--sample 150 --seed 7" in out and "--seed 11" in out
    assert "--universe-url http://localhost:8000/api/universe" in out
    for tool in ("ms_g1_check.py", "structure_backtest.py", "structure_gate_check.py", "calibration_pack.py", "board_parity_check.py"):
        assert tool in out, tool
    assert not (tmp_path / "out" / "hist_7").exists()          # nothing was downloaded
    assert (tmp_path / "out" / "summary.txt").read_text().startswith("=== seed 7")


def test_symbols_override_the_universe_and_parity_is_skipped_without_url(tmp_path):
    r = _run(tmp_path, {"DRY_RUN": "1", "SEEDS": "7", "SYMBOLS": "TCS,INFY"})
    assert "--symbols TCS,INFY" in r.stdout and "--universe-url" not in r.stdout
    assert "board parity: skipped" in r.stdout and "board_parity_check.py" not in r.stdout.split("board parity: skipped")[0]


def test_token_and_live_report_are_passed_through(tmp_path):
    r = _run(tmp_path, {"DRY_RUN": "1", "SEEDS": "7", "PS_STATUS_URL": "http://x/board/status", "PS_TOKEN": "abc",
                        "LIVE_REPORT_URL": "http://x/api/structure/report"})
    assert "--token abc" in r.stdout and "http://x/api/structure/report" in r.stdout


def test_a_failing_step_is_reported_and_the_exit_code_is_nonzero(tmp_path):
    r = _run(tmp_path, {"SEEDS": "7"}, py=_fake_py(tmp_path, "exit 3\n"))
    assert r.returncode == 1
    assert "FAILED steps:" in r.stdout and "fetch_history_for_backtest.py" in r.stdout


def test_all_steps_ok_with_a_fake_python_exits_zero(tmp_path):
    body = 'for a in "$@"; do if [ "$prev" = "--out" ]; then mkdir -p "$a"; echo "{}" > "$a/ab_report.json"; echo "# g1" > "$a/g1_report.md"; fi; prev="$a"; done\necho "{}"\n'
    r = _run(tmp_path, {"SEEDS": "7"}, py=_fake_py(tmp_path, body))
    assert r.returncode == 0, r.stdout
    assert "FAILED" not in r.stdout and "=== done." in r.stdout


def test_group433_waits_for_the_gateway_then_goes_on(tmp_path):
    r = _run(tmp_path, {"GATEWAY_WAIT_S": "6", "API_GATEWAY_URL": "http://127.0.0.1:9", "SEEDS": "7"}, py=_fake_py(tmp_path, "exit 3\n"))   # nothing listens on port 9
    assert "waiting for http://127.0.0.1:9/health" in r.stdout and "did not answer within 6s" in r.stdout
