"""E148: function-level audit baseline (READ-ONLY, stdlib). Walks services/ and shared/ (skips tests, archive, venvs) and prints
per-service counts of functions, long functions, high-complexity functions, `except Exception`, missing docstrings / return types,
plus the longest and most complex functions and the "tier A" names (order / capital / stop words) that are also long or complex.

  python3 scripts/audit_functions.py [--json] [--top N]
  python3 scripts/audit_functions.py --falsy    # int/float(x.get("k") or N) on money-like names: a real 0 is replaced by N
  python3 scripts/audit_functions.py --silent   # `except Exception: pass` handlers, one file:line per row (review the order / capital ones first)

Nothing here changes a file. The counts are a baseline for the function audit in the audit plan (tier A = order / capital / stop code gets a
line-by-line read, the rest is checked by these signals). Complexity is a simple branch count, not the radon number.
"""
import ast
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

R = Path(__file__).resolve().parent.parent
SKIP = ("/tests/", "/test_", "archive", "venv", "__pycache__", "node_modules")
TIER_A = re.compile(r"(place|cancel|send_real|sell|buy|order|entry|exit|stop|trail|breakeven|reserve|release|capital|kill|arm|reconcile|size|qty)", re.I)
# int(x.get("k") or 5) / float(d["k"] or 0.0) style defaults on names that carry money, price, score or size
FALSY = re.compile(r"(int|float)\(\s*[\w\.\[\]\"']+\.get\([^)]*\)\s*or\s*(\d+(\.\d+)?)\s*\)")
MONEYISH = re.compile(r"(score|price|stop|target|qty|quantity|capital|pnl|loss|pct|ratio|atr)", re.I)
CRITICAL = ("entry", "exit", "execution", "risk_engine", "capital", "orders", "reconcile")


def _skipped(path: Path) -> bool:
    s = str(path).replace(os.sep, "/")
    return any(k in s for k in SKIP) or path.name.startswith("test_")


def _cc(node) -> int:
    c = 1
    for x in ast.walk(node):
        if isinstance(x, (ast.If, ast.For, ast.While, ast.AsyncFor, ast.ExceptHandler, ast.With, ast.BoolOp, ast.IfExp, ast.comprehension)):
            c += 1
    return c


def _source_files(base: Path):
    for p in sorted(base.rglob("*.py")):
        if not _skipped(p):
            yield p


def _areas():
    for area in sorted(os.listdir(R / "services")) + ["shared"]:
        base = R / "services" / area if area != "shared" else R / "shared"
        if base.is_dir():
            yield area, base


def scan(base: Path):
    out = {"files": 0, "funcs": 0, "long": 0, "complex": 0, "broad_except": 0, "bare_except": 0, "no_doc": 0, "no_ret": 0}
    rows = []
    for p in _source_files(base):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
        except (SyntaxError, ValueError):
            continue
        out["files"] += 1
        for x in ast.walk(tree):
            if isinstance(x, ast.ExceptHandler):
                if x.type is None:
                    out["bare_except"] += 1
                elif isinstance(x.type, ast.Name) and x.type.id == "Exception":
                    out["broad_except"] += 1
            if isinstance(x, (ast.FunctionDef, ast.AsyncFunctionDef)):
                length = x.end_lineno - x.lineno + 1
                c = _cc(x)
                out["funcs"] += 1
                out["long"] += length > 100
                out["complex"] += c > 25
                out["no_doc"] += ast.get_docstring(x) is None
                out["no_ret"] += x.returns is None
                rows.append({"file": str(p.relative_to(R)), "name": x.name, "line": x.lineno, "len": length, "cc": c, "tier_a": bool(TIER_A.search(x.name))})
    return out, rows


def falsy_candidates():
    hits = []
    for _area, base in _areas():
        for p in _source_files(base):
            for i, line in enumerate(p.read_text(encoding="utf-8", errors="ignore").splitlines(), 1):
                if FALSY.search(line) and MONEYISH.search(line):
                    hits.append((str(p.relative_to(R)), i, line.strip()[:120]))
    return hits


def silent_excepts():
    """`except Exception: pass` with nothing else in the body: (file, line) of the handler."""
    hits = []
    for _area, base in _areas():
        for p in _source_files(base):
            try:
                tree = ast.parse(p.read_text(encoding="utf-8", errors="ignore"))
            except (SyntaxError, ValueError):
                continue
            for n in ast.walk(tree):
                if (isinstance(n, ast.ExceptHandler) and isinstance(n.type, ast.Name) and n.type.id == "Exception"
                        and len(n.body) == 1 and isinstance(n.body[0], ast.Pass)):
                    hits.append((str(p.relative_to(R)), n.lineno))
    return hits


def main():
    args = sys.argv[1:]
    top = int(args[args.index("--top") + 1]) if "--top" in args else 15
    if "--falsy" in args:
        hits = falsy_candidates()
        print("falsy-zero candidates (int/float(x.get() or N) on money-like names): %d" % len(hits))
        for f, i, t in hits:
            print("  %s:%d  %s" % (f, i, t))
        return
    if "--silent" in args:
        hits = silent_excepts()
        crit = [h for h in hits if any(k in h[0] for k in CRITICAL)]
        print("silent `except Exception: pass` = %d, in order / capital / exit / reconcile paths = %d" % (len(hits), len(crit)))
        by = Counter("/".join(h[0].split("/")[1:3]) for h in hits)
        for k, v in by.most_common(8):
            print("  %4d %s" % (v, k))
        print("\nin order / capital / exit / reconcile paths (file:line):")
        for f, i in crit:
            print("%s:%d" % (f, i))
        return
    report, allrows = {}, []
    for area, base in _areas():
        o, r = scan(base)
        report[area] = o
        allrows += r
    longest = sorted(allrows, key=lambda r: -r["len"])[:top]
    complexest = sorted(allrows, key=lambda r: -r["cc"])[:top]
    tier_a = sorted((r for r in allrows if r["tier_a"] and (r["len"] > 100 or r["cc"] > 25)), key=lambda r: -r["cc"])
    if "--json" in args:
        print(json.dumps({"summary": report, "longest": longest, "complex": complexest, "tier_a_long_or_complex": tier_a[:top]}, indent=1))
        return
    print("%-32s %5s %6s %5s %5s %7s %6s %7s" % ("area", "files", "funcs", "long", "cc25", "except", "nodoc", "noret"))
    for k, v in report.items():
        print("%-32s %5d %6d %5d %5d %7d %6d %7d" % (k, v["files"], v["funcs"], v["long"], v["complex"], v["broad_except"], v["no_doc"], v["no_ret"]))
    print("\nLongest functions:")
    for r in longest:
        print("  %5d lines cc=%-3d %s:%d %s" % (r["len"], r["cc"], r["file"], r["line"], r["name"]))
    print("\nMost complex:")
    for r in complexest:
        print("  cc=%-4d %5d lines %s:%d %s" % (r["cc"], r["len"], r["file"], r["line"], r["name"]))
    print("\nTier-A names (money / order / stop words) that are also long or complex: %d" % len(tier_a))
    for r in tier_a[:top]:
        print("  %5d lines cc=%-3d %s:%d %s" % (r["len"], r["cc"], r["file"], r["line"], r["name"]))


if __name__ == "__main__":
    main()
