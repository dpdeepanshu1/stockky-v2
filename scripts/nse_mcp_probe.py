#!/usr/bin/env python3
"""nse_mcp_probe.py - E521 (round 501): look at NSE's two MCP servers from THIS machine before anything in the project depends on them. Read-only, stdlib only.

  python3 scripts/nse_mcp_probe.py                       # both servers, RELIANCE and TCS, results in ./nse_mcp_probe_<time>/
  python3 scripts/nse_mcp_probe.py --symbols INFY --out /tmp/probe --only cmmkt

What it does (the whole run is about 20 small requests):
  1. initialize + notifications/initialized + tools/list on each server, and saves every tool's name, description and INPUT SCHEMA (tools_<server>.json). The schemas are
     the point: the argument names of the live tools are not documented on NSE's page, and the project must not guess them.
  2. calls a few read-only tools with arguments built ONLY from each tool's own schema (a tool whose required argument it cannot fill is reported as 'needs <names>',
     never called with a guess), and saves the full reply of each (call_<server>_<tool>.json).
  3. prints one line per call: ok / error / blocked, latency, and the first part of the reply, plus the age of the live data when cm_get_data_status answers.
  4. prints the signature of EVERY tool (required arguments marked *, allowed values of an enum in braces), so one paste shows all the schemas.
  5. --call TOOL --args '{"symbol": "RELIANCE", "date": "2026-11-12"}' makes ONE call you choose and prints the whole reply (still read-only).

NSE's own terms for this service: information and education only, not for real-time trading, investment advice or commercial products, and the data may not be used to train
or fine-tune models or algorithms. This script only LOOKS: it writes files, it changes nothing, and nothing in the project imports it.
Exit code: 0 when at least one server answered initialize, 1 when none did."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

SERVERS = {
    "bhavcopy": "https://mcp.nseindia.in/bhavcopy/cm/mcp",
    "cmmkt": "https://mcp.nseindia.in/cmmkt/mcp",
}
PROTOCOL = "2025-03-26"


class McpError(Exception):
    """A transport or protocol failure (the message says which); `status` is the HTTP status when there was one."""

    def __init__(self, msg: str, status: Optional[int] = None):
        super().__init__(msg)
        self.status = status


def _parse_body(ctype: str, raw: bytes, want_id: Optional[int]) -> Optional[Dict[str, Any]]:
    """A Streamable-HTTP reply is either one JSON document or an event stream of `data:` lines; return the JSON-RPC message with the wanted id (or the last one)."""
    text = raw.decode("utf-8", "replace").strip()
    if not text:
        return None
    msgs: List[Dict[str, Any]] = []
    if "text/event-stream" in (ctype or "").lower() or text.startswith(("event:", "data:", ":")):
        for block in text.replace("\r\n", "\n").split("\n\n"):
            data = "\n".join(ln[5:].lstrip() for ln in block.split("\n") if ln.startswith("data:"))
            if data:
                try:
                    msgs.append(json.loads(data))
                except ValueError:
                    continue
    else:
        try:
            doc = json.loads(text)
        except ValueError as e:
            raise McpError(f"reply is not JSON ({e}): {text[:120]!r}")
        msgs = doc if isinstance(doc, list) else [doc]
    msgs = [m for m in msgs if isinstance(m, dict)]
    for m in msgs:
        if want_id is not None and m.get("id") == want_id:
            return m
    return msgs[-1] if msgs else None


class McpClient:
    def __init__(self, url: str, timeout: float = 20.0):
        self.url, self.timeout, self.session, self._id = url, timeout, None, 0

    def _post(self, payload: Dict[str, Any], want_id: Optional[int]) -> Optional[Dict[str, Any]]:
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream", "User-Agent": "stockky-nse-mcp-probe/1"}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode("utf-8"), headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                sid = r.headers.get("Mcp-Session-Id")
                if sid:
                    self.session = sid
                return _parse_body(r.headers.get("Content-Type", ""), r.read(), want_id)
        except urllib.error.HTTPError as e:
            raise McpError(f"HTTP {e.code} {e.reason}: {e.read()[:120].decode('utf-8', 'replace')!r}", e.code)
        except (urllib.error.URLError, OSError) as e:
            raise McpError(f"cannot reach {self.url}: {e}")

    def request(self, method: str, params: Optional[Dict[str, Any]] = None) -> Any:
        self._id += 1
        msg = self._post({"jsonrpc": "2.0", "id": self._id, "method": method, "params": params or {}}, self._id)
        if msg is None:
            raise McpError(f"{method}: empty reply")
        if "error" in msg:
            err = msg["error"] if isinstance(msg["error"], dict) else {"message": str(msg["error"])}
            raise McpError(f"{method}: {err.get('message', err)}")
        return msg.get("result")

    def initialize(self) -> Dict[str, Any]:
        res = self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": {"name": "stockky-probe", "version": "1"}})
        try:
            self._post({"jsonrpc": "2.0", "method": "notifications/initialized"}, None)
        except McpError:
            pass                                    # the notification is optional for a server that does not keep state
        return res or {}

    def list_tools(self) -> List[Dict[str, Any]]:
        tools: List[Dict[str, Any]] = []
        cursor = None
        for _ in range(20):
            res = self.request("tools/list", {"cursor": cursor} if cursor else {}) or {}
            tools += [t for t in res.get("tools", []) if isinstance(t, dict)]
            cursor = res.get("nextCursor")
            if not cursor:
                break
        return tools

    def call(self, name: str, args: Dict[str, Any]) -> Tuple[bool, Any]:
        """-> (is_error, parsed reply): the text of the first content block parsed as JSON when it is JSON, else the raw text."""
        res = self.request("tools/call", {"name": name, "arguments": args}) or {}
        parts = [c.get("text", "") for c in res.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
        text = "\n".join(parts).strip()
        try:
            body: Any = json.loads(text)
        except ValueError:
            body = text
        return bool(res.get("isError")), body


def fit_args(schema: Dict[str, Any], candidates: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """Build the arguments of one call from the tool's own input schema: only names the schema declares, and only when every REQUIRED name could be filled.
    -> (args, []) or (None, [the required names that could not be filled])."""
    props = (schema or {}).get("properties") or {}
    required = list((schema or {}).get("required") or [])
    args = {k: v for k, v in candidates.items() if k in props}
    for name in required:                                      # a required name we have no candidate for: use a value the schema itself offers (first enum value, or its default), never a guess
        spec = props.get(name) if isinstance(props.get(name), dict) else {}
        if name not in args and spec.get("enum"):
            args[name] = spec["enum"][0]
        elif name not in args and "default" in spec:
            args[name] = spec["default"]
    missing = [r for r in required if r not in args]
    return (None, missing) if missing else (args, [])


def signature(tool: Dict[str, Any]) -> str:
    """name(arg*, arg{a|b}, ...): * marks a required argument; braces list up to 6 allowed values of an enum."""
    schema = tool.get("inputSchema") or {}
    props = schema.get("properties") or {}
    req = set(schema.get("required") or [])
    parts = []
    for k, v in props.items():
        enum = v.get("enum") if isinstance(v, dict) else None
        parts.append(k + ("{" + "|".join(str(e) for e in enum[:6]) + "}" if enum else "") + ("*" if k in req else ""))
    return f"{tool.get('name')}({', '.join(parts)})"


def last_weekday(today: Optional[date] = None) -> date:
    d = (today or date.today()) - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def plan(server: str, sym: str, day: date) -> List[Tuple[str, Dict[str, Any]]]:
    """(tool, candidate arguments) per server. The candidates are names the tutorials and NSE's page suggest; fit_args drops what the schema does not have."""
    ds = day.isoformat()
    cand = {"symbol": sym, "symbols": [sym], "date": ds, "endDate": ds, "fromDate": (day - timedelta(days=30)).isoformat(), "toDate": ds, "limit": 5, "n": 5, "top_n": 5, "days": 10,
            "months": 1, "indexName": "NIFTY 50"}
    if server == "cmmkt":
        return [("cm_get_data_status", cand), ("cm_get_allstocks_status", cand), ("cm_get_stock_quote", cand), ("nse_get_gainers", cand), ("nse_get_losers", cand),
                ("cm_get_live_market_data", cand)]
    return [("get_ltp_by_date", cand), ("get_52_week_high_low", cand), ("get_bulk_quote", cand), ("get_market_breadth", cand),
            ("get_corporate_actions", cand), ("get_volume_analysis", cand), ("get_index_history", cand), ("get_index_snapshot", cand), ("get_top_movers", cand)]


def _short(body: Any, n: int = 150) -> str:
    s = body if isinstance(body, str) else json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    return s if len(s) <= n else s[:n] + "..."


def _save(out: str, name: str, doc: Any) -> None:
    with open(os.path.join(out, name), "w", encoding="utf-8") as fh:
        json.dump(doc, fh, ensure_ascii=False, indent=1)


def probe_server(key: str, url: str, symbols: List[str], out: str, timeout: float, day: date) -> bool:
    print(f"\n=== {key}  {url}")
    c = McpClient(url, timeout)
    t0 = time.time()
    try:
        info = c.initialize()
    except McpError as e:
        print(f"  initialize: {'BLOCKED' if e.status in (401, 403, 429) else 'FAILED'}  {e}")
        return False
    srv = (info.get("serverInfo") or {}) if isinstance(info, dict) else {}
    print(f"  initialize: ok  {int((time.time() - t0) * 1000)} ms  server={srv.get('name', '?')} {srv.get('version', '')}  session={'yes' if c.session else 'no'}")
    try:
        tools = c.list_tools()
    except McpError as e:
        print(f"  tools/list: FAILED  {e}")
        return True
    _save(out, f"tools_{key}.json", tools)
    print(f"  tools/list: {len(tools)} tools -> tools_{key}.json")
    for t in tools:
        print(f"    * {signature(t)}")
    by_name = {t.get("name"): t for t in tools}
    for tool, cand in plan(key, symbols[0], day):
        t = by_name.get(tool)
        if t is None:
            print(f"  {tool:<26} not offered by this server")
            continue
        args, missing = fit_args(t.get("inputSchema") or {}, dict(cand, symbol=symbols[0], symbols=symbols))
        if args is None:
            print(f"  {tool:<26} needs {missing} (not guessed, not called)")
            continue
        t1 = time.time()
        try:
            is_err, body = c.call(tool, args)
        except McpError as e:
            print(f"  {tool:<26} {'BLOCKED' if e.status in (401, 403, 429) else 'FAILED'}  {e}")
            continue
        ms = int((time.time() - t1) * 1000)
        _save(out, f"call_{key}_{tool}.json", {"tool": tool, "arguments": args, "is_error": is_err, "latency_ms": ms, "reply": body})
        print(f"  {tool:<26} {'ERROR' if is_err else 'ok':<5} {ms:>5} ms  args={_short(args, 60)}  -> {_short(body)}")
    return True


def call_one(key: str, url: str, tool: str, args: Dict[str, Any], out: str, timeout: float) -> Optional[bool]:
    """--call: one explicit read-only call, the whole reply printed. -> True/False = made / failed, None = this server does not offer the tool."""
    c = McpClient(url, timeout)
    try:
        c.initialize()
        if tool not in {t.get("name") for t in c.list_tools()}:
            return None
        t1 = time.time()
        is_err, body = c.call(tool, args)
    except McpError as e:
        print(f"{key}: {'BLOCKED' if e.status in (401, 403, 429) else 'FAILED'}  {e}")
        return False
    ms = int((time.time() - t1) * 1000)
    _save(out, f"call_{key}_{tool}.json", {"tool": tool, "arguments": args, "is_error": is_err, "latency_ms": ms, "reply": body})
    print(f"{key}: {tool}({_short(args, 100)})  {'ERROR' if is_err else 'ok'}  {ms} ms\n{_short(body, 6000)}")
    return True


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Look at NSE's MCP servers from this machine (read-only).")
    ap.add_argument("--only", choices=sorted(SERVERS), default=None, help="probe one server only")
    ap.add_argument("--symbols", default="RELIANCE,TCS")
    ap.add_argument("--out", default="", help="folder for the result files (default ./nse_mcp_probe_<time>)")
    ap.add_argument("--timeout", type=float, default=20.0)
    ap.add_argument("--call", default="", help="make ONE read-only call to this tool and print the whole reply (with --args)")
    ap.add_argument("--args", default="{}", help='JSON object for --call, e.g. \'{"symbol": "RELIANCE", "date": "2026-11-12"}\'')
    ap.add_argument("--url-bhavcopy", default=SERVERS["bhavcopy"])
    ap.add_argument("--url-cmmkt", default=SERVERS["cmmkt"])
    a = ap.parse_args(argv)
    out = a.out or "nse_mcp_probe_" + datetime.now().strftime("%Y%m%d-%H%M%S")
    os.makedirs(out, exist_ok=True)
    symbols = [s.strip().upper() for s in a.symbols.split(",") if s.strip()] or ["RELIANCE"]
    urls = {"bhavcopy": a.url_bhavcopy, "cmmkt": a.url_cmmkt}
    if a.call:
        try:
            args = json.loads(a.args)
            assert isinstance(args, dict)
        except (ValueError, AssertionError):
            print("error: --args must be a JSON object", file=sys.stderr)
            return 2
        res = [call_one(k, urls[k], a.call, args, out, a.timeout) for k in sorted(urls) if a.only in (None, k)]
        if all(x is None for x in res):
            print(f"no server offers a tool named {a.call!r}")
        return 0 if any(x is True for x in res) else 1
    day = last_weekday()
    print(f"NSE MCP probe  {datetime.now():%Y-%m-%d %H:%M:%S}  symbols={symbols}  date used for history tools={day}  results -> {out}")
    oks = [probe_server(k, urls[k], symbols, out, a.timeout, day) for k in sorted(urls) if a.only in (None, k)]
    print("\nNSE's terms: information and education only; not for real-time trading, advice or commercial use; no training of models on the data. This script only looks.")
    return 0 if any(oks) else 1


if __name__ == "__main__":
    sys.exit(main())
