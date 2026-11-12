"""E470: direct `_redis_get` / `_redis_set` (kv_cache: a Neon round trip on a miss of a durable key) and `_graceful_shutdown_commit` /
`_drop_cache_keys` calls made straight from async router handlers, which stalled the event loop while Neon was slow. They now go through
`asyncio.to_thread`. AST guard (no fastapi needed): in gateway_support/bound_routes_*.py, no `ref.<blocking helper>(...)` call may sit
directly in an `async def` body (a nested sync def, such as a thread job, is fine)."""
from __future__ import annotations

import ast
import glob
import os

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_BLOCKING = {"_redis_get", "_redis_set", "_graceful_shutdown_commit", "_drop_cache_keys", "_neon_keepalive_ping", "_fetch_price_from_quote"}
# async def -> sync helper that is only ever handed to to_thread; listed so a future direct call is still caught
_ALLOWED = set()


def _direct_calls(tree):
    out = []

    def walk(node, in_async, fname):
        for c in ast.iter_child_nodes(node):
            if isinstance(c, ast.AsyncFunctionDef):
                walk(c, True, c.name)
            elif isinstance(c, ast.FunctionDef):
                walk(c, False, c.name)      # a sync def (thread job / callback) is its own context
            else:
                if in_async and isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute) \
                        and isinstance(c.func.value, ast.Name) and c.func.value.id == "ref" and c.func.attr in _BLOCKING:
                    out.append((fname, c.lineno, c.func.attr))
                walk(c, in_async, fname)
    walk(tree, False, "<module>")
    return out


def test_no_blocking_kv_or_ping_call_sits_directly_in_an_async_router_handler():
    bad = []
    for fn in sorted(glob.glob(os.path.join(_SERVICE, "gateway_support", "bound_routes_*.py"))):
        for name, line, attr in _direct_calls(ast.parse(open(fn, encoding="utf-8").read())):
            if (os.path.basename(fn), name, attr) not in _ALLOWED:
                bad.append(f"{os.path.basename(fn)}:{line} {name} -> ref.{attr}")
    assert not bad, bad


if __name__ == "__main__":
    test_no_blocking_kv_or_ping_call_sits_directly_in_an_async_router_handler()
    print("1 passed")
