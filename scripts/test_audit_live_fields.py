"""E143: audit_live_fields.py works offline: parser, extends, list/object shapes, a body built from the real client types passes, a dropped field is caught, and the HTTP path works. Plain script or pytest."""
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit_live_fields as al  # noqa: E402

TS = """
export interface Base { id: number; note?: string }
export interface Row extends Base { sym: string; nested: { a: number; b?: string }; tags: string[]; when?: string | null }
export type Doc = { ok: boolean; rows: Row[]; meta?: { x: number } }
const a = rtRequest<Row[]>(`/rows/${mode}`);
const b = rtRequest<Doc>("/doc", { method: "POST" });
"""


def test_parser_required_optional_and_extends():
    reg = al.parse_interfaces(TS)
    assert reg["Row"]["req"] == {"sym", "nested", "tags"} and reg["Row"]["opt"] == {"when"} and reg["Row"]["ext"] == ["Base"]
    assert al.required(reg, "Row") == {"id", "sym", "nested", "tags"}          # nested.a / nested.b are not top-level fields
    assert al.required(reg, "Doc") == {"ok", "rows"} and al.required(reg, "Nope") is None


def test_calls_methods_and_targets():
    calls = al.parse_calls(TS, "realTradeApi.ts")
    assert [(c["type"], c["is_list"], c["method"]) for c in calls] == [("Row", True, "GET"), ("Doc", False, "POST")]
    urls = {"rt": "http://rt", "ps": "http://ps", "runner": "http://rn", "gw": "http://gw"}
    t = al.targets(calls[0], "realTradeApi.ts", urls)
    assert [x[2] for x in t] == ["/rows/DEMO", "/rows/REAL"] and al.targets(calls[1], "realTradeApi.ts", urls) == []


def test_compare_shapes():
    need = {"id", "sym"}
    assert al.compare([{"id": 1, "sym": "A", "z": 1}], need, True) == {"shape": "list", "missing": []}
    assert al.compare([{"id": 1}], need, True)["missing"] == ["sym"]
    assert al.compare([], need, True)["shape"] == "empty-list"
    assert al.compare({"id": 1, "sym": "A"}, need, False)["missing"] == []
    assert al.compare([{"id": 1}], need, False)["shape"] == "list-but-type-is-object"
    assert al.compare({"id": 1}, need, True)["shape"] == "object-but-type-is-list"


def _synth_getter(drop=None):
    """Serve, for every endpoint of the REAL client files, a body made from that type's required fields (optionally dropping one field)."""
    bodies = {}
    for fname in ("realTradeApi.ts", "positionStocksApi.ts", "calibrationApi.ts", "api.ts"):
        text = (al.FE / fname).read_text(encoding="utf-8", errors="ignore")
        reg = al.parse_interfaces(text)
        urls = {"rt": "http://rt", "ps": "http://ps", "runner": "http://rn", "gw": "http://gw"}
        for c in al.parse_calls(text, fname):
            need = al.required(reg, c["type"])
            for _tab, base, path in (al.targets(c, fname, urls) if need is not None else []):
                body = {k: 1 for k in need if k != drop}
                bodies[base + path] = [body] if c["is_list"] else body
    return lambda url, token: (200, bodies[url]) if url in bodies else (404, None)


def test_a_body_built_from_the_real_types_passes_everywhere_and_covers_all_four_tabs():
    rows = al.run({"rt": "http://rt", "ps": "http://ps", "runner": "http://rn", "gw": "http://gw"}, "t", getter=_synth_getter())
    assert len(rows) >= 30 and all(r["http"] == 200 and not r["missing"] for r in rows)
    tabs = {r["tab"].split(" (")[0] for r in rows}
    assert tabs == {"Auto Trade", "Position Stocks", "Real Money Calibration", "Paper Trades"}


def test_a_dropped_field_is_caught():
    reg = al.parse_interfaces((al.FE / "api.ts").read_text(encoding="utf-8", errors="ignore"))
    field = sorted(al.required(reg, "PortfolioSummary"))[0]
    rows = al.run({"rt": "http://rt", "ps": "http://ps", "runner": "http://rn", "gw": "http://gw"}, "t", tab="Paper Trades", getter=_synth_getter(drop=field))
    hit = [r for r in rows if r["type"] == "PortfolioSummary"]
    assert hit and hit[0]["missing"] == [field], (field, hit)
    assert not [r for r in rows if r["type"] != "PortfolioSummary" and field not in al.required(reg, r["type"].rstrip("[]")) and r["missing"]]


class _H(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        code, body = (200, {"ok": True}) if self.path == "/ok" else (503, {})
        self.send_response(code); self.send_header("Content-Type", "application/json"); self.end_headers()
        self.wfile.write(json.dumps(body).encode())

    def log_message(self, *a):  # silence
        pass


def test_fetch_over_real_http_ok_and_error_status():
    srv = HTTPServer(("127.0.0.1", 0), _H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        assert al.fetch(base + "/ok", "tok") == (200, {"ok": True})
        assert al.fetch(base + "/nope", "")[0] == 503
        assert al.fetch("http://127.0.0.1:1/x", "")[0] == 0
    finally:
        srv.shutdown()


if __name__ == "__main__":
    for k, v in list(globals().items()):
        if k.startswith("test_"):
            v()
            print("ok", k)
