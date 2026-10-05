"""
group152 (log-audit 2026-10-05, items 3 + 6): route-registration regressions found in the open-market log.

  * fundamental/main.py: the group112 helper _md_fundamentals_symbol() was inserted between the
    @app.get("/analyze/{symbol}") decorator and analyze(), so the route served the helper (returning the plain
    string "SYMBOL.NS") and analyze() had no route. real-trade-service then logged
    "market_cap fetch failed ... ('str' object has no attribute 'get')" for every symbol.
  * event/main.py: GET /events/{symbol} was registered before GET /events/raw-feed, so "raw-feed" was treated as
    a symbol (logged as RAW-FEED.NS) and real-trade-service Tier 2 never saw {"items": [...]}.

Source-level (AST) checks so they run without FastAPI/httpx installed.
"""
import ast
import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.join(_HERE, "..")


def _routes(rel_path):
    """[(http_method, path, function_name)] in registration (source) order for module-level @app.<verb>(...) handlers."""
    with open(os.path.join(_ROOT, rel_path)) as fh:
        tree = ast.parse(fh.read())
    out = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for dec in node.decorator_list:
            if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                    and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "app"
                    and dec.args and isinstance(dec.args[0], ast.Constant)):
                out.append((dec.func.attr, dec.args[0].value, node.name))
    return out


def test_fundamental_analyze_route_is_bound_to_analyze_not_the_symbol_helper():
    routes = {path: name for _, path, name in _routes("fundamental/main.py")}
    assert routes["/analyze/{symbol}"] == "analyze"


def test_symbol_helper_is_not_a_route():
    names = [name for _, _, name in _routes("fundamental/main.py")]
    assert "_md_fundamentals_symbol" not in names


def test_no_underscore_helper_is_registered_as_a_route_in_any_service_module():
    for rel in ("fundamental/main.py", "event/main.py", "technical/main.py", "news/main.py", "sentiment/main.py"):
        if not os.path.exists(os.path.join(_ROOT, rel)):
            continue
        for _, path, name in _routes(rel):
            assert not name.startswith("_"), (rel, path, name)


def test_event_raw_feed_is_registered_before_the_symbol_catch_all():
    paths = [path for method, path, _ in _routes("event/main.py") if method == "get"]
    assert "/events/raw-feed" in paths and "/events/{symbol}" in paths
    assert paths.index("/events/raw-feed") < paths.index("/events/{symbol}")


def test_event_literal_routes_never_come_after_a_catch_all_that_would_shadow_them():
    """Any literal /events/<word> GET route must precede GET /events/{symbol}."""
    gets = [path for method, path, _ in _routes("event/main.py") if method == "get"]
    catch_all = gets.index("/events/{symbol}")
    for i, p in enumerate(gets):
        if p.startswith("/events/") and "{" not in p and p.count("/") == 2:
            assert i < catch_all, p
