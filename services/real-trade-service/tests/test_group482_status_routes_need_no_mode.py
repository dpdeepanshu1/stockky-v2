"""group 482c: GET /review-fixes/status (and kronos / range52w / timesfm status) answered 422 'query.mode Field required' on the VM, because they used
require_admin_if_real(mode, ...) on a path with no {mode}: FastAPI turned `mode` into a required query parameter nobody sends. They use require_admin now
(read-only status, admin session needed). This guard reads main.py's source (no fastapi needed) so the same mistake cannot come back on any route."""
import ast
import os

MAIN = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "main.py")
VERBS = ("get", "post", "put", "delete", "patch")


def _routes():
    tree = ast.parse(open(MAIN, encoding="utf-8").read())
    for n in ast.walk(tree):
        if not isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)):
            continue
        path = None
        for d in n.decorator_list:
            if isinstance(d, ast.Call) and getattr(d.func, "attr", "") in VERBS and d.args and isinstance(d.args[0], ast.Constant):
                path = d.args[0].value
        if path is not None:
            yield path, n


def test_no_route_uses_require_admin_if_real_without_a_mode():
    bad = []
    for path, fn in _routes():
        defaults = [a for a in list(fn.args.defaults) + list(fn.args.kw_defaults) if a is not None]
        uses = any("require_admin_if_real" in ast.dump(a) for a in defaults)
        has_mode = "{mode}" in path or "mode" in [a.arg for a in fn.args.args + fn.args.kwonlyargs]
        if uses and not has_mode:
            bad.append(path)
    assert bad == [], f"routes whose require_admin_if_real needs a mode that the route does not carry: {bad}"


def test_the_four_status_routes_use_plain_admin():
    want = {"/review-fixes/status", "/kronos/status", "/range52w/status", "/timesfm/status"}
    got = {}
    for path, fn in _routes():
        if path in want:
            dumps = [ast.dump(a) for a in fn.args.defaults]
            got[path] = any("require_admin" in d and "require_admin_if_real" not in d for d in dumps)
    assert set(got) == want and all(got.values()), got
