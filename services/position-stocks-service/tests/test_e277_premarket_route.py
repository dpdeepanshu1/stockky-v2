"""E277: GET /premarket/plan (the pre-market plan status for the Position Stocks page).
Plain-function runner: python3 tests/test_e277_premarket_route.py (also collected by pytest). main.py cannot be imported here (httpx / sqlalchemy), so the route
function is cut out of the source and run against the real premarket_plan module; the wiring is checked by source text."""
from __future__ import annotations

import ast
import os
import sys
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
_SRC = open(os.path.join(ROOT, "main.py"), encoding="utf-8").read()


def _route(enabled=True):
    fn = next(n for n in ast.parse(_SRC).body if isinstance(n, ast.FunctionDef) and n.name == "premarket_plan_route")
    fn.decorator_list = []
    ns = {"config": SimpleNamespace(PREMARKET_PLAN_ENABLED=enabled), "Depends": lambda x=None: None, "require_admin": None}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "main.py", "exec"), ns)
    return ns["premarket_plan_route"]


def test_route_returns_status_fields():
    out = _route()(admin="x")
    assert out["ok"] is True and out["enabled"] is True
    for k in ("day", "built_at", "builds", "risk_off", "vix", "events", "notes"):
        assert k in out, k


def test_switch_value_is_reported():
    assert _route(enabled=False)(admin="x")["enabled"] is False


def test_wiring_is_read_only_and_admin_gated():
    i = _SRC.index('@app.get("/premarket/plan")')
    block = _SRC[i:i + 900]
    assert "Depends(require_admin)" in block
    assert "premarket_plan" in block and "db." not in block and ".post(" not in block


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
    print("test_e277_premarket_route: ok")
