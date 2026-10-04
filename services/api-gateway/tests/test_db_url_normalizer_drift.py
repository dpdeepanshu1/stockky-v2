"""Drift guard: every service's copy of the Neon/Postgres URL normaliser must collapse the doubled '&'
that stripping a MIDDLE `channel_binding=` param leaves behind.

"?a=1&channel_binding=require&b=2" used to become "?a=1&&b=2", which libpq rejects (empty key). It was
fixed in real-trade-service (session97) and position-stocks-service (session112) but the gateway,
market-data, analysis-intelligence, decision-prediction and notification copies were missed. The
copies are separate files (not imports), so this test reads them as source rather than importing
them (importing would pull in each module's dependencies and side effects).
"""
import ast
import os
import re

import pytest

SERVICES = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
MID = "postgresql://h/db?a=1&channel_binding=require&b=2"


def _py_files():
    for dirpath, dirnames, filenames in os.walk(SERVICES):
        dirnames[:] = [d for d in dirnames if d not in ("tests", "__pycache__", "node_modules", ".git", ".venv", "venv", "site-packages", ".tox", "build", "dist")]
        for fn in filenames:
            if fn.endswith(".py"):
                yield os.path.join(dirpath, fn)


def _files_stripping_channel_binding():
    out = []
    for path in _py_files():
        try:
            with open(path, encoding="utf-8") as fh:
                src = fh.read()
        except (OSError, UnicodeDecodeError):
            continue
        if 'channel_binding=[^&]*' in src:
            out.append((path, src))
    return out


def _rel(path):
    return os.path.relpath(path, SERVICES).replace(os.sep, "/")


FILES = _files_stripping_channel_binding()


def test_the_guard_actually_found_the_copies():
    # 16 known copies at the time of writing; a drop below that means the search broke.
    assert len(FILES) >= 16, [_rel(p) for p, _ in FILES]


@pytest.mark.parametrize("path, src", FILES, ids=[_rel(p) for p, _ in FILES])
def test_copy_collapses_the_doubled_ampersand(path, src):
    assert re.search(r'&\{2,\}', src), f"{_rel(path)} strips channel_binding but never collapses '&&'"


def _normalizers():
    out = []
    for path, src in FILES:
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, ast.FunctionDef) and node.name in ("_normalize_db_url", "_normalize_pg_url"):
                out.append((path, ast.get_source_segment(src, node)))
    return out


NORMALIZERS = _normalizers()


def test_the_guard_found_standalone_normalizer_functions():
    assert len(NORMALIZERS) >= 12, [_rel(p) for p, _ in NORMALIZERS]


@pytest.mark.parametrize("path, fn_src", NORMALIZERS, ids=[_rel(p) for p, _ in NORMALIZERS])
def test_standalone_normalizer_produces_a_valid_query_for_a_middle_channel_binding(path, fn_src):
    ns = {"re": re}
    exec(fn_src, ns)  # noqa: S102 - our own source, no imports needed beyond re
    fn = ns.get("_normalize_db_url") or ns["_normalize_pg_url"]
    out = fn(MID)
    assert "&&" not in out and "channel_binding" not in out, f"{_rel(path)} -> {out}"
    assert out == "postgresql://h/db?a=1&b=2&sslmode=require", f"{_rel(path)} -> {out}"
