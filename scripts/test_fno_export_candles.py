"""E522 (round 502): scripts/fno_export_candles.py against a local stub of market-data's /history endpoint (no network)."""
import csv
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fno_export_candles as E  # noqa: E402

ROWS = [
    {"date": "2026-11-11", "open": 25000.0, "high": 25100.0, "low": 24900.0, "close": 25050.0, "volume": 0},
    {"date": "2026-11-12", "open": 25050.0, "high": 25200.0, "low": 25000.0, "close": 25150.0, "volume": "-"},
    {"date": "2026-11-12", "open": 25050.0, "high": 25210.0, "low": 25000.0, "close": 25160.0, "volume": 5},        # a duplicate day: the later row wins
    {"date": "2026-11-13", "open": 25160.0, "high": 25100.0, "low": 25300.0, "close": 25200.0, "volume": 0},        # high < low: dropped by the engine's bar rule
    {"date": "2026-11-13", "open": 25160.0, "high": 25300.0, "low": 25100.0, "close": 25250.0, "volume": 0},
]


def _stub(vix=True):
    seen = []

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            seen.append(self.path)
            if "INDIAVIX" in self.path and not vix:
                self.send_response(404)
                self.end_headers()
                return
            body = json.dumps({"candles": ROWS}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen, f"http://127.0.0.1:{srv.server_address[1]}"


def test_writes_the_daily_index_and_vix_files_the_backtests_read(tmp_path):
    srv, seen, base = _stub()
    try:
        assert E.main(["--base", base, "--out", str(tmp_path)]) == 0
    finally:
        srv.shutdown()
    assert any("%5ENSEI" in p and "interval=1d" in p and "period=1y" in p for p in seen)           # the same request the live engine makes
    rows = list(csv.DictReader(open(tmp_path / "nifty_d.csv", encoding="utf-8")))
    assert [r["date"] for r in rows] == ["2026-11-11", "2026-11-12", "2026-11-13"]                  # ascending, one row per day, the bad bar gone
    assert rows[1]["close"] == "25160.0" and rows[2]["close"] == "25250.0" and rows[1]["volume"] == "5"
    assert (tmp_path / "vix.csv").exists()


def test_the_files_load_with_the_backtest_loader(tmp_path):
    srv, _, base = _stub()
    try:
        E.main(["--base", base, "--out", str(tmp_path)])
    finally:
        srv.shutdown()
    from backtest.cli import load_candles
    c = load_candles(str(tmp_path / "nifty_d.csv"))
    assert len(c) == 3 and c[-1]["c"] == 25250.0


def test_a_missing_vix_is_not_an_error_and_a_dead_market_data_is(tmp_path, capsys):
    srv, _, base = _stub(vix=False)
    try:
        assert E.main(["--base", base, "--out", str(tmp_path)]) == 0
    finally:
        srv.shutdown()
    assert (tmp_path / "nifty_d.csv").exists() and not (tmp_path / "vix.csv").exists()
    assert E.main(["--base", "http://127.0.0.1:9", "--out", str(tmp_path / "x")]) == 1
    assert "is market-data-service running" in capsys.readouterr().err


FAKE_DOCKER = r"""#!/usr/bin/env bash
case "$*" in
  "compose ps -q fno-options-service") [ -n "${FAKE_PS-x}" ] && [ "${FAKE_PS-x}" != "none" ] && echo abc123; exit 0;;
  "compose exec -T fno-options-service sh -c"*) echo "40M /data/fno/snapshots/2026-11-12"; exit 0;;
  "inspect -f {{.Config.Image}} abc123") echo stockky-v2-fno-options-service; exit 0;;
  inspect*) echo /var/lib/docker/volumes/stockky-v2_fno-data/_data; exit 0;;
esac
printf "%s
" "$@" > "$FAKE_LOG"
exit ${FAKE_RC:-0}
"""


def _wrapper_env(tmp_path, base, **extra):
    fake = tmp_path / "bin"
    fake.mkdir(exist_ok=True)
    (fake / "docker").write_text(FAKE_DOCKER)
    (fake / "docker").chmod(0o755)
    return dict(os.environ, PATH=str(fake) + os.pathsep + os.environ["PATH"], MARKET_DATA_URL_HOST=base, REPLAY_INPUTS=str(tmp_path / "in"), REPLAY_OUT=str(tmp_path / "out"),
                FAKE_LOG=str(tmp_path / "docker_args.txt"), **extra)


def _run_wrapper(tmp_path, args=(), **extra):
    import subprocess
    srv, _, base = _stub()
    try:
        script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fno_replay_vm.sh")
        return subprocess.run(["bash", script, *args], env=_wrapper_env(tmp_path, base, **extra), capture_output=True, text=True, timeout=60)
    finally:
        srv.shutdown()


def test_the_vm_replay_wrapper_runs_the_replay_in_its_own_capped_container_not_the_live_one(tmp_path):
    """Round 505: scripts/fno_replay_vm.sh with a fake `docker` and a stub market-data. Round 503 used `docker compose exec` in the live container; a memory kill (137) restarted the service."""
    import shutil
    if not shutil.which("bash"):
        return
    r = _run_wrapper(tmp_path, ["--from", "2026-11-11"])
    assert r.returncode == 0, r.stderr
    assert "== replaying in a separate container" in r.stdout and "== replay finished, exit code 0" in r.stdout and "40M /data/fno/snapshots/2026-11-12" in r.stdout
    args = (tmp_path / "docker_args.txt").read_text().split("\n")
    assert args[0] == "run" and "--rm" in args and "exec" not in args                      # a throw-away container, never `compose exec` for the replay itself
    assert args[args.index("--memory") + 1] == "3g" and args[args.index("--memory-swap") + 1] == "3g" and args[args.index("--network") + 1] == "none"
    assert "/var/lib/docker/volumes/stockky-v2_fno-data/_data:/data/fno:ro" in args           # the recordings are read-only
    assert any(a.endswith("services/fno-options-service:/app:ro") for a in args) and "stockky-v2-fno-options-service" in args
    assert "--snapshots" in args and "/data/fno" in args and "/data/fno_real_probe/replay_inputs/nifty_d.csv" in args and "NIFTY,SENSEX" in args
    assert args[args.index("--out") + 1] == "/data/fno_real_probe/replay_out" and args[-3:-1] == ["--from", "2026-11-11"] and "--set" not in args
    assert (tmp_path / "in" / "nifty_d.csv").exists() and (tmp_path / "in" / "sensex_d.csv").exists()


def test_the_vm_replay_wrapper_reports_a_killed_replay_and_a_stopped_service(tmp_path):
    import shutil
    if not shutil.which("bash"):
        return
    down = _run_wrapper(tmp_path, FAKE_PS="none")
    assert down.returncode == 1 and "is not running" in down.stderr
    killed = _run_wrapper(tmp_path, FAKE_RC="137", REPLAY_MEM="5g")
    assert killed.returncode == 137 and "exit code 137" in killed.stdout and "the live service is untouched" in killed.stderr
    assert (tmp_path / "docker_args.txt").read_text().split("\n")[(tmp_path / "docker_args.txt").read_text().split("\n").index("--memory") + 1] == "5g"
