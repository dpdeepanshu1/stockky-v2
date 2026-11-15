"""round 572 (E597): scripts/swing_snapshot.py keeps a chunk with an older bar date out of today's file; scripts/export_records.py without a terminal and with an atomic write. Plain asserts or pytest."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import export_records as ex  # noqa: E402
import swing_snapshot as sw  # noqa: E402


def _row(sym, r12):
    return {"symbol": sym, "status": "ok", "last_close": 100.0, "ret_12_1": r12, "ret_6_1": r12 / 2, "mr_12": r12, "mr_6": r12 / 2, "above_sma200": True}


def _fetch_factory(answers):
    def f(url, token, part, benchmark=""):
        a = answers[part[0]]
        if isinstance(a, Exception):
            raise a
        return a
    return f


def test_older_chunk_is_left_out_and_counted():
    ans = {"A": {"asof_bar_date": "2026-11-13", "rows": [_row("A", 10.0), _row("B", 5.0)]},
           "C": {"asof_bar_date": "2026-11-12", "rows": [_row("C", 99.0)]},          # stale chunk: a different day's closes
           "E": {"rows": [_row("E", 50.0)]}}                                         # no bar date at all
    snap = sw.build("u", "t", ["A", "B", "C", "D", "E"], chunk=2, fetch=_fetch_factory(ans))
    syms = {r["symbol"] for r in snap["rows"]}
    assert syms == {"A", "B"} and snap["asof_bar_date"] == "2026-11-13" and snap["stale_chunks"] == 2


def test_all_chunks_same_day_all_kept():
    ans = {"A": {"asof_bar_date": "2026-11-13", "rows": [_row("A", 10.0)]}, "B": {"asof_bar_date": "2026-11-13", "rows": [_row("B", 5.0)]}}
    snap = sw.build("u", "t", ["A", "B"], chunk=1, fetch=_fetch_factory(ans))
    assert {r["symbol"] for r in snap["rows"]} == {"A", "B"} and snap["stale_chunks"] == 0


def test_failed_chunk_and_unnamed_failed_symbols():
    ans = {"A": {"asof_bar_date": "2026-11-13", "rows": [_row("A", 10.0)], "failed": [{"symbol": "Z"}, {"symbol": None}, "x"]}, "B": RuntimeError("boom")}
    snap = sw.build("u", "t", ["A", "B"], chunk=1, fetch=_fetch_factory(ans))
    assert snap["failed_symbols"] == ["Z"] and len(snap["failed_chunks"]) == 1 and snap["n_chunks"] == 1


def test_export_records_without_a_terminal_exits_1_with_a_hint(monkeypatch=None):
    saved = (os.environ.pop("TOKEN", None), ex.input if hasattr(ex, "input") else None)
    import builtins
    real_input = builtins.input
    def _eof(*a, **k):
        raise EOFError
    builtins.input = _eof
    try:
        assert ex.main(["--user", "", "--base", "http://127.0.0.1:1"]) == 1
    finally:
        builtins.input = real_input
        if saved[0] is not None:
            os.environ["TOKEN"] = saved[0]


def test_export_records_writes_through_a_part_file():
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            b = json.dumps({"total": 1, "records": [{"closed_at": "2026-11-13T10:00:00"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    old = os.environ.get("TOKEN")
    os.environ["TOKEN"] = "t"
    try:
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "rt.json")
            assert ex.main(["--base", f"http://127.0.0.1:{srv.server_port}", "--out", out]) == 0
            assert json.load(open(out))["total"] == 1 and not os.path.exists(out + ".part")
            # an unwritable target reports and exits 1 instead of a traceback
            assert ex.main(["--base", f"http://127.0.0.1:{srv.server_port}", "--out", os.path.join(d, "no", "dir", "rt.json")]) == 1
    finally:
        srv.shutdown()
        if old is None:
            os.environ.pop("TOKEN", None)
        else:
            os.environ["TOKEN"] = old


if __name__ == "__main__":
    for n, f in sorted(globals().items()):
        if n.startswith("test_"):
            f()
            print("ok", n)
