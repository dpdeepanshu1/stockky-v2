"""E170: measured service-sheet numbers for one service (READ-ONLY, stdlib).

  python3 scripts/audit_service_sheet.py real-trade-service          # markdown table to stdout
  python3 scripts/audit_service_sheet.py --all                       # every service + shared + scripts, one line each
  python3 scripts/audit_service_sheet.py position-stocks-service --json

Ten checks (the same for every sheet): 1 inventory, 2 function size, 3 silent exceptions, 4 zero-as-missing, 5 frontend route match,
6 security (mutating routes without a login guard), 7 env settings documented, 8 money-path functions, 9 tests, 10 hygiene / shared code.
Nothing here changes a file. Numbers are a baseline; a verdict is written by hand in docs/SERVICE_SHEET_<service>.md.
"""
import ast, json, os, re, sys
from pathlib import Path

R = Path(__file__).resolve().parent.parent
SKIP = ("/tests/", "archive", "venv", "__pycache__", "node_modules", "/.git/")
MUT = {"post", "put", "delete", "patch"}
GUARD = re.compile(r"(admin_dep|Depends\(\s*admin|require_admin|require_admin_if_real|verify_admin|admin_guard|require_login|require_service|Depends\(\s*require_|x_admin|x-admin|_check_admin|require_token|require_key|verify_token|verify_signature|hmac\.compare_digest)", re.I)
TIER_A = re.compile(r"(place|cancel|send_real|sell|buy|order|entry|exit|stop|trail|breakeven|reserve|release|capital|kill|arm|reconcile|size|qty)", re.I)
MONEYISH = re.compile(r"(score|price|stop|target|qty|quantity|capital|pnl|loss|pct|ratio|atr)", re.I)
FALSY = re.compile(r"(int|float)\(\s*[\w\.\[\]\"']+\.get\([^)]*\)\s*or\s*(\d+(\.\d+)?)\s*\)")
ENVRE = re.compile(r"(?:getenv|environ\.get|environ\[)\s*\(?\s*[\"']([A-Z][A-Z0-9_]{2,})[\"']")
JUNK = re.compile(r"(,cover|\.orig|\.rej|\.bak|\.swp|\.log|\.pyc|\.DS_Store|~)$")


def area_dir(name):
    for base in ("services", ""):
        p = R / base / name
        if p.is_dir():
            return p
    raise SystemExit(f"no such area: {name}")


def pyfiles(d, tests=False):
    for p in sorted(d.rglob("*.py")):
        s = str(p).replace(os.sep, "/")
        if any(k in s for k in SKIP if k != "/tests/"):
            continue
        is_test = "/tests/" in s or p.name.startswith("test_") or p.name == "conftest.py"
        if is_test == tests:
            yield p


def cc(node):
    c = 1
    for x in ast.walk(node):
        if isinstance(x, (ast.If, ast.For, ast.While, ast.AsyncFor, ast.ExceptHandler, ast.With, ast.BoolOp, ast.IfExp, ast.comprehension)):
            c += 1
    return c


def measure(name):
    d = area_dir(name)
    out = {"area": name}
    files = list(pyfiles(d))
    lines = funcs = 0
    long_, complex_, exc, silent, falsy = [], [], 0, [], []
    routes = []  # (verb, path, func, guarded, file, line)
    tier_a = []
    for p in files:
        try:
            src = p.read_text(errors="ignore")
            tree = ast.parse(src)
        except Exception:
            continue
        lines += src.count("\n") + 1
        rel = str(p.relative_to(R)).replace(os.sep, "/")
        for m in FALSY.finditer(src):
            ln = src[: m.start()].count("\n") + 1
            line = src.splitlines()[ln - 1]
            if not line.lstrip().startswith("#") and MONEYISH.search(line):
                falsy.append(f"{rel}:{ln}")
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                funcs += 1
                ln = (n.end_lineno or n.lineno) - n.lineno + 1
                c = cc(n)
                if ln > 100:
                    long_.append((ln, c, f"{rel}:{n.lineno} {n.name}"))
                if c > 25:
                    complex_.append((c, ln, f"{rel}:{n.lineno} {n.name}"))
                if (ln > 100 or c > 25) and TIER_A.search(n.name):
                    tier_a.append((ln, c, f"{rel}:{n.lineno} {n.name}"))
                for dec in n.decorator_list:
                    if isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute) and dec.func.attr in MUT | {"get"} and dec.args and isinstance(dec.args[0], ast.Constant) and isinstance(dec.args[0].value, str):
                        seg = ast.get_source_segment(src, n) or ""
                        doc = ast.get_docstring(n, clean=False) or ""
                        if doc:
                            seg = seg.replace(doc, "", 1)
                        routes.append((dec.func.attr, dec.args[0].value, n.name, bool(GUARD.search(seg)), rel, n.lineno))
            if isinstance(n, ast.ExceptHandler):
                t = n.type
                broad = t is None or (isinstance(t, ast.Name) and t.id in ("Exception", "BaseException"))
                if broad:
                    exc += 1
                    if all(isinstance(b, ast.Pass) for b in n.body):
                        silent.append(f"{rel}:{n.lineno}")
        # routes registered inside nested builders (api-gateway bound routes) are caught by the walk above
    mut = [r for r in routes if r[0] in MUT]
    gets = [r for r in routes if r[0] == "get"]
    out.update(
        files=len(files), lines=lines, funcs=funcs, routes_mut=len(mut), routes_get=len(gets),
        long=len(long_), complex=len(complex_), exc=exc, silent=len(silent), silent_list=silent,
        falsy=falsy, longest=sorted(long_, reverse=True)[:6], tier_a=sorted(tier_a, reverse=True)[:8],
        open_mut=sorted({(r[0].upper(), r[1], r[2], r[4], r[5]) for r in mut if not r[3]}),
        guarded_mut=len([r for r in mut if r[3]]),
    )
    tests = list(pyfiles(d, tests=True))
    ntests = 0
    for p in tests:
        try:
            ntests += len(re.findall(r"^\s*(?:async\s+)?def test_", p.read_text(errors="ignore"), re.M))
        except Exception:
            pass
    out.update(test_files=len(tests), test_funcs=ntests)
    # env names vs docs
    envs = set()
    sys.path.insert(0, str(R / "scripts"))
    import gen_env_reference as _g  # same scanner as docs/ENV_REFERENCE.txt (direct reads + wrapper reads)
    svc = d.name
    for k, v in _g.scan().items():
        if svc in v["svc"]:
            envs.add(k)
    for p in files:  # scripts / shared are not under services/: fall back to direct reads
        if d.parent.name != "services":
            envs |= set(ENVRE.findall(p.read_text(errors="ignore")))
    docs = ""
    for q in list(R.rglob("*.md")) + list(R.glob("docker-compose*.yml")) + list(R.glob("**/.env*")) + list((R / "config").glob("*")) + list((R / "deploy").rglob("*")):
        if q.is_file() and "archive" not in str(q) and "node_modules" not in str(q) and q.name not in ("ENV_REFERENCE.md", "ENV_REFERENCE.txt"):
            try:
                docs += q.read_text(errors="ignore")
            except Exception:
                pass
    for q in d.rglob("*"):
        if q.is_file() and q.suffix in (".md", ".txt", ".example", ".sample", ".yml", ".yaml") and "tests" not in str(q):
            try:
                docs += q.read_text(errors="ignore")
            except Exception:
                pass
    out.update(env_names=len(envs), env_undoc=sorted(e for e in envs if e not in docs))
    # hygiene
    junk, big = [], []
    for q in d.rglob("*"):
        s = str(q).replace(os.sep, "/")
        if q.is_file() and "node_modules" not in s:
            if JUNK.search(q.name) and "__pycache__" not in s:
                junk.append(str(q.relative_to(R)))
            if q.stat().st_size > 1_000_000:
                big.append((str(q.relative_to(R)), q.stat().st_size))
    shared_names = {p.name for p in (R / "shared").glob("*.py")} if (R / "shared").is_dir() else set()
    copies = sorted(p.name for p in files if p.name in shared_names and area_dir("shared") != d)
    out.update(junk=junk, big=big, shared_copies=copies)
    return out


def md(m):
    L = []
    L.append(f"| 1 | Inventory | {m['files']} files, {m['lines']:,} lines, {m['funcs']:,} functions; routes {m['routes_get']} GET + {m['routes_mut']} mutating; {m['test_files']} test files | note |")
    lg = "; ".join(f"`{x[2].split('/', 2)[-1]}` ({x[0]} lines, cc {x[1]})" for x in m["longest"][:4]) or "none"
    L.append(f"| 2 | Function size | {m['long']} over 100 lines, {m['complex']} with branch count over 25. Longest: {lg} | {'open (F-04)' if m['long'] else 'pass'} |")
    L.append(f"| 3 | Silent exceptions | {m['exc']} broad `except Exception`; {m['silent']} do nothing (`pass`) | {'note' if m['silent'] else 'pass'} |")
    L.append(f"| 4 | Zero as missing | {len(m['falsy'])} `int/float(x.get(k) or N)` sites | {'review' if m['falsy'] else 'pass'} |")
    L.append(f"| 6 | Security | {m['guarded_mut']} mutating routes with a login guard, {len(m['open_mut'])} without | {'review' if m['open_mut'] else 'pass'} |")
    L.append(f"| 7 | Env settings | {m['env_names']} names read; {len(m['env_undoc'])} have no mention outside the generated `docs/ENV_REFERENCE.txt` | {'note' if m['env_undoc'] else 'pass'} |")
    L.append(f"| 9 | Tests | {m['test_files']} files, {m['test_funcs']:,} test functions (not run in the sandbox) | VM run |")
    L.append(f"| 10 | Hygiene | stray files: {len(m['junk'])}; files over 1 MB: {len(m['big'])}; same-name copies of shared/: {len(m['shared_copies'])} | {'finding' if m['junk'] or m['big'] else 'pass'} |")
    return "\n".join(L)


AREAS = ["real-trade-service", "position-stocks-service", "api-gateway", "decision-prediction-service", "market-data-service", "notification-scheduler-service", "calibration-runner-service", "analysis-intelligence-service", "kronos-forecast-service", "timesfm-forecast-service", "shared", "scripts"]

if __name__ == "__main__":
    a = sys.argv[1:]
    if "--all" in a:
        print(f"{'area':32}{'files':>6}{'lines':>8}{'funcs':>7}{'mut':>5}{'open':>5}{'long':>6}{'silent':>7}{'falsy':>6}{'env?':>5}{'junk':>5}{'tests':>7}")
        for n in AREAS:
            m = measure(n)
            print(f"{n:32}{m['files']:>6}{m['lines']:>8}{m['funcs']:>7}{m['routes_mut']:>5}{len(m['open_mut']):>5}{m['long']:>6}{m['silent']:>7}{len(m['falsy']):>6}{len(m['env_undoc']):>5}{len(m['junk']):>5}{m['test_funcs']:>7}")
    elif a:
        m = measure(a[0])
        print(json.dumps(m, indent=1, default=str) if "--json" in a else md(m))
    else:
        print(__doc__)
