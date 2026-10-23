"""gateway_support - two helpers for route handlers that moved OUT of main.py: MainRef (E55, below) and the bind() hooks (E54, at the end of this file).

Original E55 description: lets route handlers move OUT of main.py even when they use helpers that tests patch as `main.<name>` (E55).

The gateway_routers/ rule 1 ("a moved handler uses no main.py helper") stopped the split after E53: every remaining handler needs something that lives in
main.py (`_cb_get`, `TECHNICAL_URL`, `all_snapshots`, `qstash_client`...) and tests replace those on the module (`monkeypatch.setattr(gw, "name", fake)`).

How this package keeps both: a moved handler never holds its own copy of such a helper. It reads it from the LIVE main module at call time through a
`MainRef`, so a patch on `main.<name>` is seen exactly as before. The handlers are built by a factory that main.py calls once with its own `globals()`:

    _routes = build_bound_routes(MainRef(globals()))        # main.py, at the place the routes used to be

* one MainRef per loaded copy of main.py (a test that loads main.py under another name gets its own handlers; nothing is shared between copies);
* attribute reads (`ref.name`) are `globals()[name]` at call time; attribute writes (`ref.name = v`) assign the module global, the same as a `global` statement;
* a name that main.py does not define raises AttributeError naming it, the same failure a missing global gave (NameError).

Rules (checked by tests/test_gateway_support_split.py, from the source, so they hold where fastapi is not installed):
  1. a factory module here never imports `main`;
  2. inside a handler, a main.py name is written `ref.<name>`, never a bare name;
  3. main.py keeps `<handler name> = <routes>.<handler name>` so `gw.<handler>()` calls and `monkeypatch.setattr(gw, "<handler>", ...)` still work;
  4. each moved route gets its own router, included in main.py at the SAME place the route was declared, so registration order is unchanged.
"""
from __future__ import annotations

from typing import Any, Callable, Dict


class MainRef:
    """Late-bound view of one main.py module's globals dict."""

    __slots__ = ("_g",)

    def __init__(self, module_globals: Dict[str, Any]) -> None:
        object.__setattr__(self, "_g", module_globals)

    def __getattr__(self, name: str) -> Any:
        try:
            return self._g[name]
        except KeyError:
            raise AttributeError(f"main.py has no global {name!r}") from None

    def __setattr__(self, name: str, value: Any) -> None:
        self._g[name] = value

    def __contains__(self, name: str) -> bool:
        return name in self._g


# --- E54 hooks (merged in from the E54 gateway_support.py module, which this package replaces) --------------------------------------------------------
# gateway_routers/sniper.py reaches main-owned helpers through these call-time trampolines instead of importing main (circular; tests patch names on main).
# main calls bind(...) once, before it imports a router that needs them. The registered callables look the real helper up in main at call time, so
# `monkeypatch.setattr(main, "_cb_get", fake)` still reaches routes that now live in gateway_routers/.
technical_url: str = ""


async def _unbound(*args: Any, **kwargs: Any):
    raise RuntimeError("gateway_support.cb_get is not bound (main.py binds it at import)")


def _unbound_metric(*args: Any, **kwargs: Any) -> None:
    return None


cb_get: Callable[..., Any] = _unbound
structure_metric: Callable[..., Any] = _unbound_metric


def bind(**hooks: Any) -> None:
    unknown = set(hooks) - {"cb_get", "structure_metric", "technical_url"}
    if unknown:
        raise KeyError(f"unknown gateway_support hook(s): {sorted(unknown)}")
    globals().update(hooks)
