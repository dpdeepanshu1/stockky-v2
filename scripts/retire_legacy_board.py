#!/usr/bin/env python3
"""retire_legacy_board.py - group 441: retire the legacy Market Board maths in position-stocks, but only on a GO from board_parity_check.py.

  python3 scripts/retire_legacy_board.py --file board_status.json                 # dry run (default): verdict + the diff it WOULD make
  python3 scripts/retire_legacy_board.py --url http://localhost:8006/board/status [--token T]
  python3 scripts/retire_legacy_board.py --file board_status.json --apply         # only writes when the verdict is GO

What it does (and nothing else): in `services/position-stocks-service/screening/market_structure.py` the bodies of `swings()` and
`find_zones()` become one-line calls to `stockky_ms.compat_board` (same signature, same defaults, same output shape), and the private helper
`_is_displacement` is removed when nothing else in the file uses it. The edit is made on the AST line ranges, not by regex, and the result is
parsed and its signatures compared with the originals before anything is written. A backup `market_structure.py.pre_retire` is written next
to the file (do not commit it).

What it does NOT do: touch `ms_engine_switch.py`. Its legacy fallback and the A/B still call `ms.find_zones` / `ms.swings`, which after this
edit ARE the engine, so the A/B becomes vacuous (always identical) and the fallback can no longer protect the board from an engine error.
That is the point of retiring, but remove the A/B and the fallback deliberately, as a separate step, once the board has run on the retired
code for a few sessions. `BOARD_USE_MS_ENGINE=0` (the rollback switch) also stops meaning "legacy" after this edit; the rollback is the
backup file or git.

Refusals (exit 1, nothing written): verdict is not GO (NO_GO, NOT_ENOUGH_DATA, NO_INPUT), the target file is missing or is not in the
expected legacy shape, the engine adapter is not vendored in the service, or the rewritten file fails its own checks. A file that is already
retired is reported and left alone (exit 0). Stdlib only.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import board_parity_check as bpc  # noqa: E402

ROOT = HERE.parent
DEFAULT_SERVICE = ROOT / "services" / "position-stocks-service"
TARGET_REL = Path("screening") / "market_structure.py"
ADAPTER_REL = Path("stockky_ms") / "compat_board.py"
BACKUP_SUFFIX = ".pre_retire"
RETIRE = ("swings", "find_zones")          # bodies replaced by calls to the adapter
HELPERS = ("_is_displacement",)            # removed when unused afterwards
IMPORT_LINE = "from stockky_ms import compat_board as _cb"
MARK = "retired in group441"


class Refused(Exception):
    """The edit is not allowed or not possible; the message says why."""


def _top_funcs(tree: ast.Module) -> Dict[str, ast.FunctionDef]:
    return {n.name: n for n in tree.body if isinstance(n, ast.FunctionDef)}


def is_retired(src: str) -> bool:
    return MARK in src and "_cb.find_zones(" in src and "_cb.swings(" in src


def _arg_names(fn: ast.FunctionDef) -> List[str]:
    a = fn.args
    return [x.arg for x in a.posonlyargs + a.args + a.kwonlyargs]


def _signature_lines(lines: List[str], fn: ast.FunctionDef) -> List[str]:
    end = fn.body[0].lineno - 1                      # the line before the docstring / first statement
    sig = lines[fn.lineno - 1:end]
    if not sig or not sig[-1].rstrip().endswith(":"):
        raise Refused(f"cannot read the signature of {fn.name}() (unexpected layout)")
    return sig


def _delegate(lines: List[str], fn: ast.FunctionDef) -> List[str]:
    names = _arg_names(fn)
    first, rest = names[0], names[1:]
    call = f"_cb.{fn.name}({first}" + "".join(f", {n}={n}" if fn.name == "find_zones" else f", {n}" for n in rest) + ")"
    doc = ast.get_docstring(fn)
    summary = (doc or "").strip().splitlines()[0] if doc else fn.name
    summary = summary.replace('"""', "'")
    return _signature_lines(lines, fn) + [f'    """{summary} ({MARK}: now stockky_ms.compat_board.{fn.name}; the old body is in git / the .pre_retire backup)."""',
                                          f"    return {call}"]


def retire_source(src: str) -> Tuple[str, Dict[str, Any]]:
    """Pure. Returns (new source, report). Raises Refused when the file is not in the expected legacy shape."""
    if is_retired(src):
        raise Refused("already retired")
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        raise Refused(f"the target does not parse: {e}")
    funcs = _top_funcs(tree)
    missing = [n for n in RETIRE if n not in funcs]
    if missing:
        raise Refused(f"not the expected legacy file: no top-level {', '.join(missing)}()")
    for n in RETIRE:
        if len(funcs[n].body) < 2 and not ast.get_docstring(funcs[n]):
            raise Refused(f"{n}() already looks like a stub, not a legacy body")
    lines = src.split("\n")
    edits: List[Tuple[int, int, List[str]]] = []       # (first line idx, last line idx exclusive, replacement)
    for n in RETIRE:
        fn = funcs[n]
        edits.append((fn.lineno - 1, fn.end_lineno, _delegate(lines, fn)))
    removed: List[str] = []
    # the helper goes only when nothing outside the two retired functions and itself uses it
    retired_nodes = [funcs[n] for n in RETIRE]
    for h in HELPERS:
        fn = funcs.get(h)
        if fn is None:
            continue
        outside = [x for x in ast.walk(tree) if isinstance(x, ast.Name) and x.id == h
                   and not any(x in set(ast.walk(r)) for r in retired_nodes + [fn])]
        if not outside:
            end = fn.end_lineno
            while end < len(lines) and lines[end].strip() == "":
                end += 1                                  # swallow the blank lines after it
            edits.append((fn.lineno - 1, end, []))
            removed.append(h)
    edits.sort(key=lambda e: e[0], reverse=True)
    for a, b, rep in edits:
        lines[a:b] = rep
    # the adapter import, right after the typing import
    idx = next((i for i, ln in enumerate(lines) if ln.startswith("from typing import")), None)
    if idx is None:
        raise Refused("no `from typing import` line to anchor the adapter import")
    lines.insert(idx + 1, IMPORT_LINE)
    new = "\n".join(lines)
    try:
        new_tree = ast.parse(new)
        compile(new, "market_structure.py", "exec")
    except SyntaxError as e:
        raise Refused(f"internal check failed, the rewritten file does not parse: {e}")
    nf = _top_funcs(new_tree)
    for n in RETIRE:
        if _arg_names(nf[n]) != _arg_names(funcs[n]) or ast.dump(nf[n].args) != ast.dump(funcs[n].args):
            raise Refused(f"internal check failed: the signature of {n}() changed")
    for n in funcs:
        if n not in RETIRE and n not in removed and (n not in nf or ast.dump(nf[n]) != ast.dump(funcs[n])):
            raise Refused(f"internal check failed: {n}() was altered")
    old_len, new_len = len(src.split("\n")), len(new.split("\n"))
    return new, {"retired": list(RETIRE), "removed_helpers": removed, "lines_before": old_len, "lines_after": new_len}


def run(service_dir: Path, status: Any, apply: bool, min_compared: int, min_days: int, out=print) -> int:
    """0 = done / dry run with a GO / already retired; 1 = refused. Never writes unless apply and verdict GO."""
    verdict = bpc.evaluate(status, min_compared, min_days)
    out(bpc.render(verdict))
    go = verdict["verdict"] == bpc.GO
    target, adapter = service_dir / TARGET_REL, service_dir / ADAPTER_REL
    if not target.is_file():
        out(f"REFUSED: {target} not found (pass --service-dir)")
        return 1
    if not adapter.is_file():
        out(f"REFUSED: the engine adapter {ADAPTER_REL} is not vendored in {service_dir}: nothing to delegate to")
        return 1
    src = target.read_text(encoding="utf-8")
    try:
        new, rep = retire_source(src)
    except Refused as e:
        if str(e) == "already retired":
            out("Already retired: nothing to do.")
            return 0
        out(f"REFUSED: {e}")
        return 1
    diff = "".join(difflib.unified_diff(src.splitlines(True), new.splitlines(True), "market_structure.py (legacy)", "market_structure.py (retired)", n=1))
    out(f"\nWould replace the bodies of {', '.join(rep['retired'])} with calls to stockky_ms.compat_board; "
        f"removes {', '.join(rep['removed_helpers']) or 'no helper'}; {rep['lines_before']} -> {rep['lines_after']} lines.")
    if not go:
        out("\nNOT ALLOWED: the verdict above is not GO, so --apply would refuse. Diff shown for review only.\n")
        out(diff)
        return 1
    if not apply:
        out("\nDRY RUN (nothing written). Re-run with --apply to make this edit.\n")
        out(diff)
        return 0
    backup = target.with_name(target.name + BACKUP_SUFFIX)
    backup.write_text(src, encoding="utf-8")
    target.write_text(new, encoding="utf-8")
    out(f"\nWritten: {target}\nBackup:  {backup} (do not commit)")
    out("Next: run services/position-stocks-service tests (group305 / 349 / 362 / 365), keep the golden test, redeploy, watch /board/status "
        "for a few sessions, then remove the A/B and the fallback in screening/ms_engine_switch.py as a separate change.")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", default="")
    ap.add_argument("--file", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--service-dir", default=str(DEFAULT_SERVICE))
    ap.add_argument("--min-compared", type=int, default=200)
    ap.add_argument("--min-days", type=int, default=3)
    ap.add_argument("--apply", action="store_true", help="write the edit (still refuses unless the parity verdict is GO)")
    a = ap.parse_args(argv)
    try:
        if not (a.url or a.file):
            raise ValueError("give --url or --file (the /board/status JSON of position-stocks)")
        status = bpc._load(a)
    except Exception as e:  # noqa: BLE001
        print(f"REFUSED: could not read the status: {type(e).__name__}: {e}")
        return 1
    return run(Path(a.service_dir), status, a.apply, a.min_compared, a.min_days)


if __name__ == "__main__":
    sys.exit(main())
