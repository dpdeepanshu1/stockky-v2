#!/usr/bin/env python3
"""check_control_catalog.py - group 442: static guard for the Real Money Calibration switches (services/*/control/catalog.py).

  python3 scripts/check_control_catalog.py                       # check both catalogs (CI)
  python3 scripts/check_control_catalog.py --probe real-trade-service config:KRONOS_GATE_MODE candidate_engine.candidates:RANGE_52W_LOG_ONLY
                                                                 # how is this attribute consumed? (use while writing a catalog entry)

A switch can only take effect immediately when every consumer reads the value at CALL time (`config.X` inside a function, or the defining
module's own global inside its functions). A consumer that copies it at import (`from config import X`, `Y = config.X` at module level,
a default argument, a class body) keeps the old value until the service restarts. Marking such a switch `live=True` would show "applied" on the page
while the trading code still used the old value, which is worse than saying "restart needed". So this check fails when:
  * a catalog entry is malformed (duplicate key, bad kind / choices / range, recommended or shipped value that does not parse);
  * a target (module, attribute) is not assigned at the top level of that module in the service;
  * `live=True` and some consumer copies the value at import, or nothing reads it at call time (dead switch).
Stdlib only, read-only.
"""
from __future__ import annotations

import argparse
import ast
import importlib.util
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

ROOT = Path(__file__).resolve().parent.parent
SERVICES = ("real-trade-service", "position-stocks-service")
SKIP_DIRS = {"tests", "test", "__pycache__", "stockky_ms", "scripts", ".venv", "node_modules", "control"}


def _py_files(svc: Path) -> List[Path]:
    out = []
    for p in sorted(svc.rglob("*.py")):
        rel = p.relative_to(svc).parts
        if any(part in SKIP_DIRS for part in rel[:-1]) or p.name.startswith("test_"):
            continue
        out.append(p)
    return out


def _modname(svc: Path, p: Path) -> str:
    parts = list(p.relative_to(svc).with_suffix("").parts)
    if parts and parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


class _Scope(ast.NodeVisitor):
    """Collects, for one file, where a given attribute is read: at import time (module / class level, default args, decorators) or at call time."""

    def __init__(self, module: str, target_mod: str, attr: str, aliases: Set[str]):
        self.module, self.target_mod, self.attr, self.aliases = module, target_mod, attr, aliases
        self.depth = 0                     # >0 inside a function / lambda body
        self.import_time: List[int] = []
        self.call_time: List[int] = []
        self.imports_name: List[int] = []  # `from <target_mod> import attr`
        self.defined_here = False
        self.own = module == target_mod

    def _hit(self, node: ast.AST) -> None:
        (self.call_time if self.depth > 0 else self.import_time).append(getattr(node, "lineno", 0))

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if (node.module or "") == self.target_mod and node.level == 0:
            for a in node.names:
                if a.name == self.attr:
                    self.imports_name.append(node.lineno)

    def _func(self, node: Any) -> None:
        for d in node.decorator_list:
            self.visit(d)
        for d in list(node.args.defaults) + [x for x in node.args.kw_defaults if x is not None]:
            self.visit(d)
        self.depth += 1
        for st in node.body:
            self.visit(st)
        self.depth -= 1

    visit_FunctionDef = _func
    visit_AsyncFunctionDef = _func

    def visit_Lambda(self, node: ast.Lambda) -> None:
        for d in node.args.defaults:
            self.visit(d)
        self.depth += 1
        self.visit(node.body)
        self.depth -= 1

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == self.attr and isinstance(node.value, ast.Name) and node.value.id in self.aliases and isinstance(node.ctx, ast.Load):
            self._hit(node)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        # the defining module reads its own global
        if self.own and node.id == self.attr and isinstance(node.ctx, ast.Load):
            self._hit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # getattr(config, "ATTR", default)
        f = node.func
        # the services' own small helpers: _cfg("ATTR", default) = getattr(config, "ATTR", default) evaluated where it is called
        if isinstance(f, ast.Name) and f.id.startswith("_cfg") and node.args and isinstance(node.args[0], ast.Constant) \
                and node.args[0].value == self.attr and "config" in self.aliases:
            self._hit(node)
        if isinstance(f, ast.Name) and f.id == "getattr" and len(node.args) >= 2:
            a0, a1 = node.args[0], node.args[1]
            if isinstance(a0, ast.Name) and a0.id in self.aliases and isinstance(a1, ast.Constant) and a1.value == self.attr:
                self._hit(node)
        self.generic_visit(node)


def _aliases_for(tree: ast.Module, target_mod: str) -> Set[str]:
    """Names this file binds to the target module (`import config`, `import a.b as x`, `from a import b`)."""
    out: Set[str] = set()
    last = target_mod.split(".")[-1]
    pkg = ".".join(target_mod.split(".")[:-1])
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for a in n.names:
                if a.name == target_mod:
                    out.add(a.asname or a.name)
        elif isinstance(n, ast.ImportFrom) and n.level == 0 and pkg and (n.module or "") == pkg:
            for a in n.names:
                if a.name == last:
                    out.add(a.asname or a.name)
    return out


def _module_file(svc: Path, module: str) -> Optional[Path]:
    rel = Path(*module.split("."))
    for cand in (svc / rel.with_suffix(".py"), svc / rel / "__init__.py"):
        if cand.is_file():
            return cand
    return None


def _assigns(tree: ast.Module, attr: str) -> bool:
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == attr for t in n.targets):
            return True
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.target.id == attr:
            return True
    # also inside top-level if / try blocks
    for n in ast.walk(tree):
        if isinstance(n, (ast.If, ast.Try)):
            for sub in ast.walk(n):
                if isinstance(sub, ast.Assign) and any(isinstance(t, ast.Name) and t.id == attr for t in sub.targets):
                    return True
    return False


def probe(svc: Path, module: str, attr: str, allow_import_time_files: Tuple[str, ...] = ()) -> Dict[str, Any]:
    """Where is `module.attr` of this service read? -> {defined, call_time: [(file,line)], import_time: [...], copies: [...]}"""
    res: Dict[str, Any] = {"defined": False, "call_time": [], "import_time": [], "copies": []}
    mf = _module_file(svc, module)
    if mf is None:
        res["error"] = f"module {module} not found in {svc.name}"
        return res
    res["defined"] = _assigns(ast.parse(mf.read_text(encoding="utf-8")), attr)
    for p in _py_files(svc):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        mod = _modname(svc, p)
        sc = _Scope(mod, module, attr, _aliases_for(tree, module))
        sc.visit(tree)
        rel = str(p.relative_to(svc))
        res["call_time"] += [(rel, ln) for ln in sc.call_time]
        res["import_time"] += [(rel, ln) for ln in sc.import_time if rel not in allow_import_time_files]
        res["copies"] += [(rel, ln) for ln in sc.imports_name]
    return res


def _load_catalog(svc: Path):
    """Import `control.catalog` and `control.kit` of ONE service (both services have a package called `control`, so the module cache is cleared around it)."""
    import importlib

    def _purge() -> None:
        for m in [k for k in sys.modules if k == "control" or k.startswith("control.")]:
            del sys.modules[m]

    _purge()
    sys.path.insert(0, str(svc))
    try:
        cat = importlib.import_module("control.catalog")
        kit = importlib.import_module("control.kit")
    finally:
        sys.path.remove(str(svc))
    return cat, kit


def check_service(svc: Path) -> List[str]:
    problems: List[str] = []
    mod, kit = _load_catalog(svc)
    flags = list(mod.CATALOG)
    seen: Set[str] = set()
    for f in flags:
        tag = f"{svc.name}: {f.key}"
        if f.key in seen:
            problems.append(f"{tag}: duplicate key")
        seen.add(f.key)
        for msg in kit.validate_flag(f):
            problems.append(f"{tag}: {msg}")
        for (m, a) in f.targets:
            r = probe(svc, m, a, tuple(f.import_time_ok_files))
            if r.get("error"):
                problems.append(f"{tag}: {r['error']}")
                continue
            if not r["defined"]:
                problems.append(f"{tag}: {m}.{a} is not assigned at the top level of that module")
            if f.live:
                if r["copies"]:
                    problems.append(f"{tag}: live=True but copied at import by `from {m} import {a}` at {r['copies'][:3]}")
                if r["import_time"]:
                    problems.append(f"{tag}: live=True but read at import time at {r['import_time'][:3]} (use call-time reads, or live=False, or list the file in import_time_ok_files when it only normalises / derives a DEMO default)")
                if not r["call_time"]:
                    problems.append(f"{tag}: live=True but nothing reads {m}.{a} at call time (dead switch?)")
        if f.live and not f.targets:
            problems.append(f"{tag}: live=True needs targets")
    return problems


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--probe", nargs="+", metavar="SERVICE_OR_ARGS", help="SERVICE module:attr [module:attr ...]")
    a = ap.parse_args(argv)
    if a.probe:
        svc = ROOT / "services" / a.probe[0]
        for spec in a.probe[1:]:
            m, _, at = spec.partition(":")
            r = probe(svc, m, at)
            verdict = "LIVE-OK" if r["defined"] and r["call_time"] and not r["import_time"] and not r["copies"] else "RESTART"
            print(f"{verdict:8} {m}:{at} defined={r['defined']} call_time={len(r['call_time'])} import_time={r['import_time'][:4]} copies={r['copies'][:4]} {r.get('error', '')}")
        return 0
    bad: List[str] = []
    for s in SERVICES:
        bad += check_service(ROOT / "services" / s)
    for p in bad:
        print("FAIL", p)
    print(f"checked {len(SERVICES)} catalogs: {'FAILED, ' + str(len(bad)) + ' problem(s)' if bad else 'ok'}")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
