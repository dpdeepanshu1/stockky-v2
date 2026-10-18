"""group 431: scripts/export_records.py against a throwaway local HTTP server (no real service)."""
from __future__ import annotations

import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import export_records as ex

SEEN = {}


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, doc):
        b = json.dumps(doc).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
        SEEN["login"] = body
        self._send(200, {"token": "tok1"}) if body.get("password") == "pw" else self._send(401, {"detail": "bad"})

    def do_GET(self):
        SEEN["path"], SEEN["auth"] = self.path, self.headers.get("Authorization")
        if self.path.startswith("/positions/REAL") and self.headers.get("Authorization") != "Bearer tok1":
            return self._send(401, {"detail": "no"})
        recs = [{"position_id": 1, "closed_at": "2026-10-09T09:30:00+00:00"}, {"position_id": 2, "closed_at": "2026-10-12T09:30:00+00:00"}]
        self._send(200, {"mode": "REAL", "total": 3 if "days=7" in self.path else 2, "returned": 2, "records": recs})


def _server():
    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"


def test_login_then_export_and_summary(tmp_path, monkeypatch, capsys):
    srv, base = _server()
    monkeypatch.delenv("TOKEN", raising=False)
    monkeypatch.setenv("ADMIN_PASSWORD", "pw")
    out = tmp_path / "rt.json"
    assert ex.main(["--base", base, "--user", "admin", "--days", "90", "--out", str(out)]) == 0
    srv.shutdown()
    assert SEEN["login"] == {"username": "admin", "password": "pw"} and "days=60" in SEEN["path"] and "limit=1000" in SEEN["path"]    # days clamped to 60
    assert json.loads(out.read_text())["records"][1]["position_id"] == 2
    assert "newest closed 2026-10-12" in capsys.readouterr().out


def test_token_env_skips_login_and_bad_login_fails(tmp_path, monkeypatch, capsys):
    srv, base = _server()
    monkeypatch.setenv("TOKEN", "tok1")
    SEEN.clear()
    assert ex.main(["--base", base, "--out", str(tmp_path / "a.json")]) == 0 and "login" not in SEEN
    monkeypatch.delenv("TOKEN")
    monkeypatch.setenv("ADMIN_PASSWORD", "wrong")
    assert ex.main(["--base", base, "--user", "admin", "--out", str(tmp_path / "b.json")]) == 1
    srv.shutdown()
    assert "401" in capsys.readouterr().err and not (tmp_path / "b.json").exists()


def test_truncation_warning_demo_needs_no_login_and_unreachable(tmp_path, monkeypatch, capsys):
    assert "WARNING" in ex.summarize({"total": 5, "records": [{"closed_at": "2026-10-01T00:00:00"}]})
    assert "WARNING" not in ex.summarize({"total": 1, "records": [{"closed_at": "2026-10-01T00:00:00"}]})
    srv, base = _server()
    monkeypatch.delenv("TOKEN", raising=False)
    SEEN.clear()
    assert ex.main(["--base", base, "--mode", "DEMO", "--days", "7", "--out", str(tmp_path / "d.json")]) == 0 and "login" not in SEEN
    srv.shutdown()
    assert ex.main(["--base", "http://127.0.0.1:9", "--mode", "DEMO", "--out", str(tmp_path / "e.json")]) == 1
    assert "docker compose ps" in capsys.readouterr().err
