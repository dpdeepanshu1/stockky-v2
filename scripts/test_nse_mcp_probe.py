"""E521 (round 501): scripts/nse_mcp_probe.py against a LOCAL stub MCP server (no network). Covers the Streamable-HTTP handshake (session header), JSON and event-stream replies,
arguments built only from a tool's own schema, a required argument that cannot be filled, a tool error, and a 403 from the server."""
import json
import os
import sys
import threading
from datetime import date
from http.server import BaseHTTPRequestHandler, HTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import nse_mcp_probe as P  # noqa: E402

TOOLS = [
    {"name": "get_52_week_high_low", "description": "x", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
    {"name": "get_ltp_by_date", "description": "x", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}, "date": {"type": "string"}}, "required": ["symbol", "date"]}},
    {"name": "get_market_breadth", "description": "x", "inputSchema": {"type": "object", "properties": {"on_day": {"type": "string"}}, "required": ["on_day"]}},      # an argument name the probe cannot fill
    {"name": "get_corporate_actions", "description": "x", "inputSchema": {"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]}},
]


def _server(mode="json", block=False):
    seen = {"calls": [], "sessions": []}

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json", extra=None):
            raw = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(raw)))
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            if block:
                return self._send(403, "Access Denied", "text/html")
            msg = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
            sid = self.headers.get("Mcp-Session-Id")
            m, i = msg.get("method"), msg.get("id")
            if m == "initialize":
                return self._send(200, json.dumps({"jsonrpc": "2.0", "id": i, "result": {"serverInfo": {"name": "stub", "version": "9"}}}), extra={"Mcp-Session-Id": "S1"})
            seen["sessions"].append(sid)
            if sid != "S1":
                return self._send(400, "missing session")
            if m == "notifications/initialized":
                return self._send(202, "")
            if m == "tools/list":
                return self._send(200, json.dumps({"jsonrpc": "2.0", "id": i, "result": {"tools": TOOLS}}))
            if m == "tools/call":
                p = msg["params"]
                seen["calls"].append((p["name"], p["arguments"]))
                if p["name"] == "get_corporate_actions":
                    res = {"isError": True, "content": [{"type": "text", "text": "Symbol not found"}]}
                else:
                    res = {"content": [{"type": "text", "text": json.dumps({"tool": p["name"], "args": p["arguments"], "last_close": 101.5})}]}
                doc = json.dumps({"jsonrpc": "2.0", "id": i, "result": res})
                if mode == "sse":
                    return self._send(200, f"event: message\ndata: {doc}\n\n", "text/event-stream")
                return self._send(200, doc)
            return self._send(404, "no")

    srv = HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, seen, f"http://127.0.0.1:{srv.server_address[1]}/mcp"


def test_handshake_list_and_call_over_json():
    srv, seen, url = _server("json")
    try:
        c = P.McpClient(url, 5)
        assert c.initialize()["serverInfo"]["name"] == "stub" and c.session == "S1"
        assert [t["name"] for t in c.list_tools()][:2] == ["get_52_week_high_low", "get_ltp_by_date"]
        err, body = c.call("get_52_week_high_low", {"symbol": "TCS"})
        assert err is False and body["last_close"] == 101.5 and body["args"] == {"symbol": "TCS"}
        assert set(seen["sessions"]) == {"S1"}                          # every request after initialize carried the session id
    finally:
        srv.shutdown()


def test_event_stream_replies_are_read_too():
    srv, _, url = _server("sse")
    try:
        c = P.McpClient(url, 5)
        c.initialize()
        err, body = c.call("get_52_week_high_low", {"symbol": "TCS"})
        assert err is False and body["last_close"] == 101.5
    finally:
        srv.shutdown()


def test_a_tool_error_is_reported_not_raised():
    srv, _, url = _server("json")
    try:
        c = P.McpClient(url, 5)
        c.initialize()
        err, body = c.call("get_corporate_actions", {"symbol": "ZZZ"})
        assert err is True and body == "Symbol not found"
    finally:
        srv.shutdown()


def test_fit_args_uses_only_the_schema_and_never_guesses_a_required_name():
    cand = {"symbol": "TCS", "symbols": ["TCS"], "date": "2026-11-13", "limit": 5}
    assert P.fit_args(TOOLS[1]["inputSchema"], cand) == ({"symbol": "TCS", "date": "2026-11-13"}, [])
    assert P.fit_args(TOOLS[0]["inputSchema"], cand) == ({"symbol": "TCS"}, [])
    assert P.fit_args(TOOLS[2]["inputSchema"], cand) == (None, ["on_day"])
    assert P.fit_args({}, cand) == ({}, [])                              # a tool with no declared inputs is called with none


def test_last_weekday_skips_the_weekend():
    assert P.last_weekday(date(2026, 11, 14)) == date(2026, 11, 13)      # Saturday -> Friday
    assert P.last_weekday(date(2026, 11, 15)) == date(2026, 11, 13)      # Sunday -> Friday
    assert P.last_weekday(date(2026, 11, 16)) == date(2026, 11, 13)      # Monday -> Friday
    assert P.last_weekday(date(2026, 11, 17)) == date(2026, 11, 16)


def test_a_403_is_reported_as_blocked(tmp_path, capsys):
    srv, _, url = _server("json", block=True)
    try:
        rc = P.main(["--only", "cmmkt", "--url-cmmkt", url, "--out", str(tmp_path), "--timeout", "5"])
    finally:
        srv.shutdown()
    assert rc == 1 and "BLOCKED" in capsys.readouterr().out


def test_a_full_run_saves_schemas_and_replies_and_calls_only_what_it_can_fill(tmp_path, capsys):
    srv, seen, url = _server("json")
    try:
        rc = P.main(["--only", "bhavcopy", "--url-bhavcopy", url, "--symbols", "infy,tcs", "--out", str(tmp_path), "--timeout", "5"])
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert rc == 0 and (tmp_path / "tools_bhavcopy.json").exists()
    called = [c[0] for c in seen["calls"]]
    assert "get_52_week_high_low" in called and "get_ltp_by_date" in called and "get_corporate_actions" in called
    assert "get_market_breadth" not in called and "needs ['on_day']" in out                 # not guessed
    assert "get_bulk_quote" in out and "not offered by this server" in out
    assert dict(seen["calls"])["get_52_week_high_low"] == {"symbol": "INFY"}                # the first symbol, upper-cased, only the declared name
    saved = json.load(open(tmp_path / "call_bhavcopy_get_ltp_by_date.json"))
    assert saved["arguments"]["symbol"] == "INFY" and saved["reply"]["last_close"] == 101.5
    assert "ERROR" in out                                                                    # the corporate-actions stub answers isError: shown, not hidden


def test_fit_args_may_use_a_value_the_schema_itself_offers():
    sch = {"type": "object", "properties": {"index": {"type": "string", "enum": ["gainers", "loosers"]}, "n": {"type": "integer", "default": 5}}, "required": ["index", "n"]}
    assert P.fit_args(sch, {"symbol": "TCS"}) == ({"index": "gainers", "n": 5}, [])           # first enum value / the default, never an invented one
    assert P.fit_args(sch, {"index": "loosers"})[0]["index"] == "loosers"                    # a candidate still wins over the enum


def test_signature_marks_required_arguments_and_lists_enums():
    assert P.signature(TOOLS[1]) == "get_ltp_by_date(symbol*, date*)"
    sig = P.signature({"name": "cm_get_live_market_data", "inputSchema": {"properties": {"index": {"enum": ["gainers", "loosers"]}, "x": {}}, "required": ["index"]}})
    assert sig == "cm_get_live_market_data(index{gainers|loosers}*, x)"


def test_the_run_prints_every_tool_signature(tmp_path, capsys):
    srv, _, url = _server("json")
    try:
        P.main(["--only", "bhavcopy", "--url-bhavcopy", url, "--out", str(tmp_path), "--timeout", "5"])
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert "* get_ltp_by_date(symbol*, date*)" in out and "* get_market_breadth(on_day*)" in out


def test_call_mode_makes_one_explicit_call_and_prints_the_whole_reply(tmp_path, capsys):
    srv, seen, url = _server("json")
    try:
        rc = P.main(["--call", "get_ltp_by_date", "--args", '{"symbol": "RELIANCE", "date": "2026-11-12"}', "--only", "bhavcopy", "--url-bhavcopy", url, "--out", str(tmp_path), "--timeout", "5"])
    finally:
        srv.shutdown()
    out = capsys.readouterr().out
    assert rc == 0 and seen["calls"] == [("get_ltp_by_date", {"symbol": "RELIANCE", "date": "2026-11-12"})] and '"last_close":101.5' in out
    assert (tmp_path / "call_bhavcopy_get_ltp_by_date.json").exists()


def test_call_mode_with_an_unknown_tool_or_bad_args(tmp_path, capsys):
    srv, seen, url = _server("json")
    try:
        assert P.main(["--call", "nope", "--only", "bhavcopy", "--url-bhavcopy", url, "--out", str(tmp_path), "--timeout", "5"]) == 1
        assert "no server offers a tool named 'nope'" in capsys.readouterr().out
        assert P.main(["--call", "get_ltp_by_date", "--args", "[1]", "--out", str(tmp_path)]) == 2
    finally:
        srv.shutdown()
    assert seen["calls"] == []
