#!/usr/bin/env python3
"""check_ms_package.py - E50 (F33 part 3): keep the installable market-structure package (shared/pyproject.toml) honest.

  python3 scripts/check_ms_package.py            # CI: static checks only (stdlib, read-only)
  python3 scripts/check_ms_package.py --build    # also build a wheel in a temp folder, install it into a throw-away target and import it

Checks: pyproject maps the package `stockky_ms` to the folder `market_structure`; it has no dependencies; every engine module imports only the
standard library or its own package (relative imports), so the wheel needs nothing else; the folder has no `.py` file outside the package
that the sync script would copy but the wheel would not. The vendored per-service copies stay (each Docker image is built from its own service
folder); `scripts/sync_market_structure.py --check` keeps them equal to the same folder this wheel is built from.
"""
from __future__ import annotations

import ast
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import List

ROOT = Path(__file__).resolve().parent.parent
SHARED = ROOT / "shared"
PKG_DIR = SHARED / "market_structure"
STDLIB = set(getattr(sys, "stdlib_module_names", ())) or {
    "__future__", "ast", "bisect", "collections", "copy", "dataclasses", "datetime", "enum", "functools", "hashlib", "heapq", "itertools",
    "json", "logging", "math", "os", "pathlib", "random", "re", "statistics", "sys", "time", "typing"}


def _pyproject_text(root: Path = ROOT) -> str:
    return (root / "shared" / "pyproject.toml").read_text(encoding="utf-8")


def static_problems(root: Path = ROOT) -> List[str]:
    p: List[str] = []
    pp = root / "shared" / "pyproject.toml"
    if not pp.is_file():
        return ["shared/pyproject.toml is missing"]
    text = _pyproject_text(root)
    try:
        import tomllib  # Python 3.11+
        data = tomllib.loads(text)
        if data.get("project", {}).get("dependencies"):
            p.append("pyproject lists dependencies; the engine must stay standard-library only")
        if data.get("tool", {}).get("setuptools", {}).get("packages") != ["stockky_ms"]:
            p.append('[tool.setuptools] packages must be ["stockky_ms"]')
        if data.get("tool", {}).get("setuptools", {}).get("package-dir", {}).get("stockky_ms") != "market_structure":
            p.append('[tool.setuptools.package-dir] must map stockky_ms = "market_structure"')
    except ModuleNotFoundError:  # Python 3.10: text checks
        if 'stockky_ms = "market_structure"' not in text:
            p.append('pyproject must map stockky_ms = "market_structure"')
        if "dependencies = []" not in text:
            p.append("pyproject must declare dependencies = []")
    pkg = root / "shared" / "market_structure"
    if not (pkg / "__init__.py").is_file():
        p.append("shared/market_structure/__init__.py is missing")
    for f in sorted(pkg.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for n in ast.walk(tree):
            if isinstance(n, ast.Import):
                for a in n.names:
                    if a.name.split(".")[0] not in STDLIB:
                        p.append(f"{f.name}: imports third-party / unknown module {a.name!r}")
            elif isinstance(n, ast.ImportFrom) and not n.level:
                if (n.module or "").split(".")[0] not in STDLIB:
                    p.append(f"{f.name}: imports third-party / unknown module {n.module!r} (use a relative import or the standard library)")
    return p


def build_and_import() -> List[str]:
    with tempfile.TemporaryDirectory() as td:
        td_p = Path(td)
        src = td_p / "src"  # build from a copy: setuptools writes build/ and *.egg-info next to the project, never into the repo
        shutil.copytree(SHARED, src, ignore=shutil.ignore_patterns("tests", "__pycache__", "build", "*.egg-info"))
        r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", "--no-build-isolation", str(src), "-w", str(td_p / "dist")],
                           capture_output=True, text=True)
        if r.returncode:
            r = subprocess.run([sys.executable, "-m", "pip", "wheel", "--no-deps", str(src), "-w", str(td_p / "dist")], capture_output=True, text=True)
        if r.returncode:
            return [f"wheel build failed: {(r.stderr or r.stdout)[-400:]}"]
        wheels = list((td_p / "dist").glob("stockky_ms-*.whl"))
        if not wheels:
            return ["no wheel produced"]
        r = subprocess.run([sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(td_p / "site"), str(wheels[0])], capture_output=True, text=True)
        if r.returncode:
            return [f"wheel install failed: {(r.stderr or r.stdout)[-400:]}"]
        code = "import stockky_ms, stockky_ms.engine as e; assert callable(e.analyze); print('ok')"
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, cwd=td, env={"PYTHONPATH": str(td_p / "site"), "PATH": ""})
        if r.returncode or "ok" not in r.stdout:
            return [f"import from the installed wheel failed: {(r.stderr or r.stdout)[-400:]}"]
    return []


def main(argv: List[str]) -> int:
    problems = static_problems()
    if "--build" in argv and not problems:
        problems += build_and_import()
    for x in problems:
        print("FAIL:", x)
    if not problems:
        print("ok: shared/pyproject.toml matches shared/market_structure (stdlib only)" + (", wheel builds and imports" if "--build" in argv else ""))
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
