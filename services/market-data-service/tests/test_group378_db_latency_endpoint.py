"""group378: GET /ops/db-latency exists in this service and serves oracle_compat.latency_snapshot (main.py checked as source - it needs fastapi)."""
from __future__ import annotations

import ast
import os

_MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")


def test_ops_db_latency_route_is_wired_to_the_latency_snapshot():
    src = open(_MAIN, encoding="utf-8").read()
    fn = next(n for n in ast.walk(ast.parse(src)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "ops_db_latency")
    routes = [ast.get_source_segment(src, d) for d in fn.decorator_list]
    assert any('"/ops/db-latency"' in r and ".get(" in r for r in routes)
    body = ast.get_source_segment(src, fn)
    assert "latency_snapshot" in body and "except Exception" in body     # never a 500: a failure answers {"enabled": False, ...}
    assert src.count('"/ops/db-latency"') == 1
