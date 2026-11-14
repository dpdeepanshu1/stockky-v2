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
