"""E142: tab-vs-backend audit (READ-ONLY, stdlib). For Auto Trade, Position Stocks, Real Money Calibration and Paper Trades: every API path the tab code uses must exist as a route of its service
(prefix-built paths such as /control/*, /scheduler-control/*, /api/universe/* are reported as NO ROUTE and must be confirmed by hand), and every backend route that no frontend file uses is listed.
  python3 scripts/audit_tabs.py [--json]
"""
import re, sys, json
from pathlib import Path
R = Path(__file__).resolve().parent.parent; FE = R/"frontend/src"; SV = R/"services"
ROUTE = re.compile(r'@\s*(?:\w+)\.(get|post|put|delete|patch)\(\s*[rf]?["\']([^"\']*)["\']')

def norm(p):
    p = p.split("?")[0]
    p = re.sub(r"\$\{[^}]*\}", "{}", p)
    p = re.sub(r"\{[^}/]*\}", "{}", p)
    return p.rstrip("/") or "/"

def routes(paths, extra_prefix=None):
    out = {}
    for base in paths:
        for f in Path(base).rglob("*.py"):
            s = str(f)
            if "/tests/" in s or "/test_" in f.name or "venv" in s: continue
            txt = f.read_text(encoding="utf-8", errors="ignore")
            for m in ROUTE.finditer(txt):
                p = m.group(2)
                if f.parent.name == "control" and f.name == "kit.py": p = "/control" + p
                out.setdefault(norm(p), set()).add(f"{f.relative_to(R)}")
    return out

RT = routes([SV/"real-trade-service"]); PS = routes([SV/"position-stocks-service"]); RUN = routes([SV/"calibration-runner-service"])
GW = routes([SV/"api-gateway"]); DPT = routes([SV/"decision-prediction-service"/"training"])
DP = routes([SV/"decision-prediction-service"])
# frontend reaches the training app under /training via the gateway/decision service
DPT_pref = {norm("/training"+p): v for p, v in DPT.items()}

def match(path, table):
    if path in table: return True
    rx_cache = []
    for t in table:
        rx = "^" + re.escape(t).replace(re.escape("{}"), "[^/]+") + "$"
        if re.match(rx, path): return True
        # frontend segment may be a template: allow {} to match literal segment on backend side
        rx2 = "^" + re.escape(path).replace(re.escape("{}"), "[^/]+") + "$"
        if re.match(rx2, t): return True
    return False

LIT = re.compile(r'''["'`](/[A-Za-z0-9_\-./{}$]*[A-Za-z0-9_}](?:\?[^"'`]*)?)["'`]''')
def fe_paths(files):
    out = {}
    for f in files:
        txt = f.read_text(encoding="utf-8", errors="ignore")
        for m in LIT.finditer(txt):
            p = m.group(1)
            if p.startswith(("//", "/*")) or "." in p.split("/")[-1] and not p.endswith(".zip"): 
                if not p.endswith(".zip"): continue
            if len(p) < 3 or p.count("/") == 0: continue
            if re.search(r"^/(src|assets|node_modules|vite|api\b)$", p): continue
            out.setdefault(norm(p), set()).add(f.name)
    return out

def allf(*globs):
    s = []
    for g in globs:
        for f in FE.glob(g):
            if f.is_file() and f.suffix in (".ts", ".tsx") and ".test." not in f.name: s.append(f)
    return sorted(set(s))

TABS = {
 "Auto Trade (Real Auto Trade)": dict(files=allf("realTradeApi.ts","components/RealAutoTrade.tsx","components/realAuto/**/*.ts*"), tables=[RT]),
 "Position Stocks": dict(files=allf("positionStocksApi.ts","components/PositionStocksTab.tsx","components/board/**/*.ts*"), tables=[PS, RT]),
 "Real Money Calibration": dict(files=allf("calibrationApi.ts","calibrationPlan.ts","calibrationTrades.ts","components/CalibrationControlTab.tsx","components/calibration/**/*.ts*"), tables=[RUN, RT, PS]),
 "Paper Trades": dict(files=allf("components/Trades.tsx","paperNet.ts","api.ts"), tables=[GW, DPT_pref, DP]),
}
res = {}
allfe = {}
for name, t in TABS.items():
    fp = fe_paths(t["files"]); allfe.update(fp)
    unmatched = {p: sorted(v) for p, v in fp.items() if not any(match(p, tb) for tb in t["tables"])}
    res[name] = dict(files=[str(f.relative_to(FE)) for f in t["files"]], n_paths=len(fp), unmatched=unmatched)
# every frontend path in the whole app (for backend->frontend reverse check)
whole = fe_paths([f for f in FE.rglob("*") if f.is_file() and f.suffix in (".ts",".tsx") and ".test." not in f.name])
def unused(tbl):
    return sorted(p for p in tbl if not any(match(fp, {p: 1}) for fp in whole))
res["_unused"] = {"real-trade-service": unused(RT), "position-stocks-service": unused(PS), "calibration-runner-service": unused(RUN)}
res["_counts"] = {"RT": len(RT), "PS": len(PS), "RUN": len(RUN), "GW": len(GW), "DPT": len(DPT)}
if "--json" in sys.argv: print(json.dumps(res, indent=1, default=list))
for k, v in res.items():
    if k.startswith("_"): continue
    print(f"\n== {k}: {len(v['files'])} files, {v['n_paths']} api paths, {len(v['unmatched'])} with no backend route")
    for p, fs in v["unmatched"].items(): print("   NO ROUTE:", p, fs)
print("\ncounts", res["_counts"])
for s, l in res["_unused"].items(): print(f"\n{s}: {len(l)} routes not referenced by any frontend file")
