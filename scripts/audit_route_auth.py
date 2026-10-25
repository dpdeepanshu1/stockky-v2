"""E149: api-gateway route-auth matrix (READ-ONLY, stdlib). Plan check 6 (security): for every mutating gateway route (POST / PUT / PATCH / DELETE)
say whether it needs the admin login, and for the ones that do not, who calls it (frontend, scripts, workflows, other services).
  python3 scripts/audit_route_auth.py            matrix of the routes WITHOUT the admin login, with callers
  python3 scripts/audit_route_auth.py --all      every mutating route
  python3 scripts/audit_route_auth.py --json
  python3 scripts/audit_route_auth.py --check    exit 1 when an unguarded route is neither triaged in docs/ROUTE_AUTH_TRIAGE.json nor guarded
A route counts as guarded when its decorator carries `require_admin` / `_require_admin`, when its router was built with
`APIRouter(dependencies=[Depends(require_admin)])`, or when it is listed in TRIAGE_INLINE (the /training/* proxy checks inside the handler).
"""
import json, re, sys
from pathlib import Path

R = Path(__file__).resolve().parent.parent
GW = R / "services" / "api-gateway"
TRIAGE_FILE = R / "docs" / "ROUTE_AUTH_TRIAGE.json"
DECOR = re.compile(r'@(\w+)\.(post|put|delete|patch|api_route)\(\s*f?["\']([^"\']+)["\']')
ROUTER_DEP = re.compile(r'(\w+)\s*=\s*APIRouter\([^)]*dependencies\s*=\s*\[[^\]]*require_admin')
TRIAGE_INLINE = {"/training/{path:path}"}  # E148: training_path_needs_admin() runs inside the handler
SKIP_DIRS = {"node_modules", "archive", ".git", "tests", "venv", ".venv", "dist"}


def _files(base, exts):
    for p in base.rglob("*"):
        if p.is_file() and p.suffix in exts and not (set(p.parts) & SKIP_DIRS) and not p.name.startswith("test_") and ".test." not in p.name:
            yield p


def mutating_routes():
    out = []
    for p in GW.rglob("*.py"):
        if "tests" in p.parts:
            continue
        txt = p.read_text(encoding="utf-8", errors="ignore")
        router_guarded = set(ROUTER_DEP.findall(txt))
        lines = txt.splitlines()
        for i, line in enumerate(lines):
            m = DECOR.search(line)
            if not m:
                continue
            blk = " ".join(lines[i:i + 3])
            verb = m.group(2)
            if verb == "api_route":
                mm = re.search(r"methods=\[([^\]]*)\]", blk)
                verb = mm.group(1).replace('"', "").replace("'", "") if mm else "?"
                if not re.search(r"POST|PUT|DELETE|PATCH", verb.upper()):
                    continue
            guarded = "require_admin" in blk or m.group(1) in router_guarded or m.group(3) in TRIAGE_INLINE
            out.append({"path": m.group(3), "verb": verb.upper().replace(" ", ""), "guarded": guarded, "where": f"{p.relative_to(R)}:{i + 1}"})
    return out


def _corpus():
    docs = []
    for base, exts in ((R / "frontend" / "src", {".ts", ".tsx"}), (R / "scripts", {".py", ".sh"}), (R / ".github", {".yml", ".yaml"}),
                       (R / "deploy", {".sh", ".yml", ".yaml", ".conf", ".service", ".timer"}), (R / "services", {".py"})):
        if not base.exists():
            continue
        for p in _files(base, exts):
            if p.name == "audit_route_auth.py" or str(p.relative_to(R)) == "services/api-gateway/main.py" or "/gateway_support/bound_routes" in str(p) or "/gateway_routers/" in str(p):
                continue
            docs.append((str(p.relative_to(R)), p.read_text(encoding="utf-8", errors="ignore")))
    return docs


def callers(path, docs):
    stem = path.split("{")[0].rstrip("/") or path
    if stem.startswith("/api/"):  # the gateway also answers under /api/...; callers use the bare path with a base URL in front
        stem = stem[4:]
    rx = re.compile(re.escape(stem) + r"(?![A-Za-z0-9_-])")
    kinds = {"frontend": set(), "script/workflow": set(), "service": set()}
    for name, txt in docs:
        if not rx.search(txt):
            continue
        k = "frontend" if name.startswith("frontend/") else "service" if name.startswith("services/") else "script/workflow"
        kinds[k].add(name)
    return {k: sorted(v) for k, v in kinds.items() if v}


def main(argv):
    rows = mutating_routes()
    docs = _corpus()
    uniq = {}
    for r in rows:
        key = (r["path"], r["verb"])
        e = uniq.setdefault(key, {**r, "wheres": []})
        e["wheres"].append(r["where"])
        e["guarded"] = e["guarded"] and r["guarded"]
    items = sorted(uniq.values(), key=lambda e: (e["guarded"], e["path"]))
    for e in items:
        e["callers"] = {} if e["guarded"] else callers(e["path"], docs)
    triage = json.loads(TRIAGE_FILE.read_text()) if TRIAGE_FILE.exists() else {}
    if "--check" in argv:
        bad = [e["path"] for e in items if not e["guarded"] and e["path"] not in triage]
        for p in bad:
            print("UNTRIAGED unguarded mutating route:", p)
        stale = [p for p in triage if p not in {e["path"] for e in items if not e["guarded"]}]
        for p in stale:
            print("STALE triage entry (route is now guarded or gone):", p)
        print(f"audit_route_auth --check: {len(bad)} untriaged, {len(stale)} stale")
        return 1 if bad or stale else 0
    show = items if "--all" in argv else [e for e in items if not e["guarded"]]
    if "--json" in argv:
        print(json.dumps(show, indent=1))
        return 0
    g = sum(e["guarded"] for e in items)
    print(f"{len(items)} mutating routes (distinct path + verb), {g} need the admin login, {len(items) - g} do not\n")
    for e in show:
        c = e["callers"]
        who = "; ".join(f"{k}: {len(v)}" for k, v in c.items()) or "NO CALLER FOUND"
        print(f"{'ADMIN ' if e['guarded'] else 'OPEN  '} {e['verb']:<18} {e['path']:<48} {who}  [{triage.get(e['path'], {}).get('class', '-')}]")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
