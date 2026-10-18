"""Tests for scripts/tsfm_nightly.py (group 434)."""
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import tsfm_nightly as n  # noqa: E402


def _server(fail=(), skip=()):
    seen = []

    class H(BaseHTTPRequestHandler):
        def do_POST(self):
            b = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append(b)
            syms = b["symbols"]
            out = json.dumps({"done": [s for s in syms if s not in fail and s not in skip], "skipped": [s for s in syms if s in skip],
                              "failed": [{"symbol": s, "status": 422, "error": "x"} for s in syms if s in fail]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen


def test_chunks_dedups_sorts_and_reports(capsys):
    srv, seen = _server(fail={"B"}, skip={"C"})
    try:
        rc = n.main(["--url", f"http://127.0.0.1:{srv.server_port}", "--symbols", "d,a,b,c,a,e", "--chunk", "2"])
    finally:
        srv.shutdown()
    out = json.loads(capsys.readouterr().out)
    assert [b["symbols"] for b in seen] == [["A", "B"], ["C", "D"], ["E"]]
    assert out["symbols"] == 5 and out["done"] == 3 and out["skipped"] == 1 and out["failed"] == 1 and rc == 0


def test_failed_share_above_the_limit_exits_1(capsys):
    srv, _ = _server(fail={"A", "B", "C"})
    try:
        rc = n.main(["--url", f"http://127.0.0.1:{srv.server_port}", "--symbols", "A,B,C,D", "--max-failed-pct", "40"])
    finally:
        srv.shutdown()
    assert rc == 1 and json.loads(capsys.readouterr().out)["failed_pct"] == 75.0


def test_unreachable_service_exits_1_and_counts_chunks(capsys):
    rc = n.main(["--url", "http://127.0.0.1:9", "--symbols", "A,B", "--retries", "1", "--timeout", "1"])
    out = json.loads(capsys.readouterr().out)
    assert rc == 1 and out["unreachable_chunks"] == 1 and out["failed"] == 2


def test_no_symbols_is_an_error(capsys):
    assert n.main(["--url", "http://127.0.0.1:9"]) == 1
