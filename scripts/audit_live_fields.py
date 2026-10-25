#!/usr/bin/env python3
"""audit_live_fields.py - E143: do the live JSON bodies carry the fields each tab's TypeScript types expect? READ-ONLY (GET only), stdlib only.

  TOKEN=<admin token> python3 scripts/audit_live_fields.py [--rt http://127.0.0.1:8005] [--ps http://127.0.0.1:8006] [--runner http://127.0.0.1:8012] [--gw http://127.0.0.1:8000]
                                                           [--tab "Auto Trade"] [--skip REGEX] [--json]

For every typed GET call in frontend/src/{realTradeApi,positionStocksApi,calibrationApi,api}.ts (api.ts: the Paper Trades `/training/api/trades` calls only) it fetches the endpoint the way the page does,
then compares the REQUIRED fields of the TypeScript type (no `?`) with the keys of the JSON (first element for a list). MISSING = the page reads a field the service did not send: it renders blank or NaN.
Not checked: nested fields, field types, calls with other path parameters or POST/PUT/DELETE (listed as skipped), `any` responses. An empty list or a 4xx/5xx is reported, not guessed at.
Exit code 1 when any REQUIRED field is missing.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
FE = ROOT / "frontend" / "src"

CALL = re.compile(r"\b(rtRequest|psRequest|R|C|request)<([A-Za-z]\w*)(\[\])?(?:\s*&\s*\{[^}]*\})?>\(\s*(?:svc,\s*)?([`\"'])(.*?)\4", re.S)


def _members(body: str) -> List[str]:
    out, depth, cur = [], 0, []
    for ch in body:
        if ch in "{[(<":
            depth += 1
        elif ch in "}])>":
            depth -= 1
        if depth == 0 and ch in ";,\n":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(ch)
    out.append("".join(cur))
    return [m for m in out if m.strip()]


def parse_interfaces(text: str) -> Dict[str, Dict[str, Any]]:
    """name -> {"req": set, "opt": set, "ext": [names]} for `interface X {..}` and `type X = {..}` (object types only)."""
    reg: Dict[str, Dict[str, Any]] = {}
    for m in re.finditer(r"(?:export\s+)?(?:interface|type)\s+(\w+)(?:<[^>{]*>)?\s*(?:extends\s+([^{]+?))?\s*=?\s*\{", text):
        i, depth, j = m.end(), 1, m.end()
        while j < len(text) and depth:
            depth += {"{": 1, "}": -1}.get(text[j], 0)
            j += 1
        req, opt = set(), set()
        for mem in _members(text[i:j - 1]):
            pm = re.match(r"\s*(?:readonly\s+)?([A-Za-z_]\w*)(\?)?\s*:", re.sub(r"^\s*(//[^\n]*)?", "", mem))
            if pm:
                (opt if pm.group(2) else req).add(pm.group(1))
        ext = [e.strip().split("<")[0] for e in (m.group(2) or "").split(",") if e.strip()]
        reg[m.group(1)] = {"req": req, "opt": opt, "ext": ext}
    return reg


def required(reg: Dict[str, Dict[str, Any]], name: str, seen: Optional[set] = None) -> Optional[set]:
    seen = seen or set()
    if name not in reg or name in seen:
        return None if name not in reg else set()
    seen.add(name)
    out = set(reg[name]["req"])
    for e in reg[name]["ext"]:
        r = required(reg, e, seen)
        if r:
            out |= r
    return out


def parse_calls(text: str, fname: str) -> List[Dict[str, Any]]:
    """Typed calls: {fn, type, is_list, path, method, line}. fname picks the service/tab."""
    out = []
    for m in CALL.finditer(text):
        fn, typ, lst, _q, path = m.group(1), m.group(2), bool(m.group(3)), m.group(4), m.group(5)
        # the call's OWN argument list: from its "(" to the matching ")" (the next call, on the next line, must not leak in)
        op = text.index("(", m.start() + len(fn))
        depth, k = 0, op
        while k < len(text):
            depth += {"(": 1, ")": -1}.get(text[k], 0)
            k += 1
            if depth == 0:
                break
        meth = re.search(r"method:\s*[\"'](POST|PUT|DELETE|PATCH)[\"']", text[op:k])
        out.append({"fn": fn, "type": typ, "is_list": lst, "path": path, "method": meth.group(1) if meth else "GET",
                    "line": text[: m.start()].count("\n") + 1})
    return out


def targets(call: Dict[str, Any], fname: str, urls: Dict[str, str]) -> List[Tuple[str, str, str]]:
    """-> [(tab, base_url, path)] or [] when the call is skipped (path parameter other than mode, not a GET)."""
    path = call["path"].split("?")[0]
    if call["method"] != "GET":
        return []
    if fname == "api.ts":
        return [("Paper Trades", urls["gw"], path)] if path.startswith("/training/api/trades") and "${" not in path else []
    if "${" in path:
        if re.fullmatch(r"[^$]*\$\{mode\}[^$]*", path) and fname == "realTradeApi.ts":
            return [("Auto Trade", urls["rt"], path.replace("${mode}", m)) for m in ("DEMO", "REAL")]
        return []
    if fname == "realTradeApi.ts":
        return [("Auto Trade", urls["rt"], path)]
    if fname == "positionStocksApi.ts":
        return [("Position Stocks", urls["ps"], path)]
    if fname == "calibrationApi.ts":
        if call["fn"] == "C":
            return [("Real Money Calibration (rt /control)", urls["rt"], "/control" + path), ("Real Money Calibration (ps /control)", urls["ps"], "/control" + path)]
        return [("Real Money Calibration", urls["runner"], path)]
    return []


def fetch(url: str, token: str) -> Tuple[int, Any]:
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"} if token else {})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:  # noqa: S310 - operator-supplied URL
            return r.status, json.loads(r.read().decode() or "null")
    except urllib.error.HTTPError as e:
        return e.code, None
    except Exception as e:  # noqa: BLE001
        return 0, f"{type(e).__name__}: {str(e)[:80]}"


def compare(body: Any, need: set, is_list: bool) -> Dict[str, Any]:
    """MISSING = required fields absent from the body; shape = what came back."""
    if isinstance(body, list):
        if not is_list:
            return {"shape": "list-but-type-is-object", "missing": []}
        if not body:
            return {"shape": "empty-list", "missing": []}
        row = body[0]
        return {"shape": "list", "missing": sorted(need - set(row)) if isinstance(row, dict) else ["<element is not an object>"]}
    if isinstance(body, dict):
        if is_list:
            return {"shape": "object-but-type-is-list", "missing": []}
        return {"shape": "object", "missing": sorted(need - set(body))}
    return {"shape": type(body).__name__, "missing": ["<body is not JSON object/list>"]}


def run(urls: Dict[str, str], token: str, tab: str = "", skip: str = "", getter=fetch) -> List[Dict[str, Any]]:
    rows = []
    for fname in ("realTradeApi.ts", "positionStocksApi.ts", "calibrationApi.ts", "api.ts"):
        text = (FE / fname).read_text(encoding="utf-8", errors="ignore")
        reg = parse_interfaces(text)
        for call in parse_calls(text, fname):
            need = required(reg, call["type"])
            tg = targets(call, fname, urls)
            if need is None or not tg:
                continue
            for tname, base, path in tg:
                if (tab and tab.lower() not in tname.lower()) or (skip and re.search(skip, path)):
                    continue
                status, body = getter(base.rstrip("/") + path, token if "gw" not in tname.lower() and tname != "Paper Trades" else "")
                row = {"tab": tname, "path": path, "type": call["type"] + ("[]" if call["is_list"] else ""), "http": status, "need": len(need), "file": f"{fname}:{call['line']}"}
                row.update(compare(body, need, call["is_list"]) if status == 200 else {"shape": "no-body", "missing": []})
                rows.append(row)
    return rows


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--rt", default="http://127.0.0.1:8005")
    ap.add_argument("--ps", default="http://127.0.0.1:8006")
    ap.add_argument("--runner", default="http://127.0.0.1:8012")
    ap.add_argument("--gw", default="http://127.0.0.1:8000")
    ap.add_argument("--tab", default="")
    ap.add_argument("--skip", default="")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args(argv)
    rows = run({"rt": a.rt, "ps": a.ps, "runner": a.runner, "gw": a.gw}, os.getenv("TOKEN", ""), a.tab, a.skip)
    if a.json:
        print(json.dumps(rows, indent=1))
    bad = [r for r in rows if r["missing"]]
    print(f"\nchecked {len(rows)} endpoints: {len([r for r in rows if r['http'] == 200 and not r['missing']])} ok, {len(bad)} with MISSING required fields, "
          f"{len([r for r in rows if r['shape'] == 'empty-list'])} empty lists (cannot check), {len([r for r in rows if r['http'] != 200])} not HTTP 200")
    for r in bad:
        print(f"MISSING  [{r['tab']}] {r['path']}  type {r['type']} ({r['file']}): {', '.join(r['missing'])}")
    for r in rows:
        if r["http"] != 200:
            print(f"HTTP {r['http']:<3}  [{r['tab']}] {r['path']}  type {r['type']}")
        elif r["shape"] not in ("object", "list", "empty-list"):
            print(f"SHAPE    [{r['tab']}] {r['path']}: {r['shape']} (type {r['type']})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
