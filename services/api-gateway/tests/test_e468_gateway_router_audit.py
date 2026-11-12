"""E468: audit of gateway_routers/ plus the WS quote loop. Four defects, each reproduced first:
  1. sniper: a symbol from the request body went straight into the technical-service URL path ("X/../admin?a=1" reached another route).
  2. scheduler_control: `_ALLOWED.match` with `$` let "audit\\n" through (then `_METHODS["audit\\n"]` raised KeyError = HTTP 500); now fullmatch.
  3. price_alerts: a direction other than above/below was stored as "above" (a typo made an alert that fires the opposite way); now a 400.
  4. main.py quote loop: `symbols[:12]` every tick, so with more than 12 watched symbols the rest never got a live push; now round-robin.
No fastapi needed (functions are lifted from the source by AST and run against fakes). Plain asserts; runs with pytest or `python3 tests/test_e468_gateway_router_audit.py`."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import math
import os
import re
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)


def _src(rel):
    return open(os.path.join(_SERVICE, rel), encoding="utf-8").read()


def _lift(rel, name):
    src = _src(rel)
    for n in ast.parse(src).body:
        if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == name:
            return ast.get_source_segment(src, n)
    raise AssertionError(f"{name} not found in {rel}")


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SERVICE, rel))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


# ---- 1. sniper -------------------------------------------------------------------------------------------------------------------------------
class _Resp:
    status_code = 200

    def json(self):
        return {"market_structure": {"schema_version": 1, "quality": "ok", "engine_version": "t", "params_hash": "h", "trend": "bullish", "atr": 2.0,
                                     "last_close": 100.0, "data_quality": {"last_bar_ts": None},
                                     "location": {"entry_location_score": 70, "chase_atr": 3.0, "stretch_atr": 1.0, "room_atr": 3.0},
                                     "structure_rr": {"rr": 3.0, "stop": 96.0, "target": 112.0, "reward_pct": 12.0, "flags": []}}}


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def test_e468_a_symbol_cannot_steer_the_technical_service_url():
    bs = _load("buy_sniper", "buy_sniper.py")
    msp = _load("ms_passthrough", "ms_passthrough.py")
    urls = []

    async def fake_get(client, name, url, timeout=5.0, **kw):
        urls.append(url)
        return _Resp()

    g = {"asyncio": asyncio, "os": os, "httpx": types.SimpleNamespace(AsyncClient=_Client), "ms_passthrough": msp, "TECHNICAL_URL": "http://tech",
         "logger": logging.getLogger("t"), "Dict": dict, "Any": object, "_cb_get": fake_get, "buy_sniper": bs, "_structure_metric": lambda *a, **k: None}
    exec("from __future__ import annotations\n" + _lift("gateway_routers/sniper.py", "_sniper_attach_structure"), g)
    mk = lambda sym, conv: {"symbol": sym, "decision": "BUY", "conviction": conv, "price": 100.0}
    payload = {"stocks": [mk("X/../../admin?a=1", 95), mk("AAA", 90), mk("M&M", 80), mk("BAJAJ-AUTO", 70), mk("A B", 60), mk("../x", 50)]}
    run(g["_sniper_attach_structure"](payload))
    assert sorted(urls) == ["http://tech/structure/AAA", "http://tech/structure/BAJAJ-AUTO", "http://tech/structure/M%26M"], urls   # M&M is quoted; the rest never leave


# ---- 2. scheduler proxy ----------------------------------------------------------------------------------------------------------------------
def test_e468_scheduler_proxy_path_must_match_exactly():
    src = _src("gateway_routers/scheduler_control.py")
    m = re.search(r"^_ALLOWED = re\.compile\((r\"[^\"]*\")\)", src, re.M)
    rx = re.compile(ast.literal_eval(m.group(1)))
    assert "_ALLOWED.fullmatch(path)" in src and "_ALLOWED.match(" not in src
    for good in ("flags", "flags/MY_KEY_1", "preset/recommended", "audit"):
        assert rx.fullmatch(good)
    for bad in ("audit\n", "flags/ABC\n", "flags/abc", "audit/x", "flags/", "../audit", ""):
        assert not rx.fullmatch(bad), bad


# ---- 3. price alerts -------------------------------------------------------------------------------------------------------------------------
def test_e468_an_unknown_direction_is_a_400_not_a_silent_above():
    class HTTPException(Exception):
        def __init__(self, status_code, detail=None):
            self.status_code, self.detail = status_code, detail

    added = []
    fake_df = types.ModuleType("data_feed")
    fake_df.add_price_alert = lambda sym, target, direction="above", note="": added.append((sym, target, direction)) or {"symbol": sym}
    saved = sys.modules.get("data_feed")
    sys.modules["data_feed"] = fake_df

    class Req:
        def __init__(self, body):
            self._b = body

        async def json(self):
            return self._b

    class _Router:
        def post(self, *a, **k):
            return lambda f: f
    g = {"asyncio": asyncio, "math": math, "HTTPException": HTTPException, "Request": object, "router": _Router()}
    src = _src("gateway_routers/price_alerts.py")
    tree = ast.parse(src)
    fn = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "api_add_price_alert"][0]
    code = ast.get_source_segment(src, fn)
    exec("from __future__ import annotations\n" + code, g)
    f = g["api_add_price_alert"]
    try:
        for body, ok in (({"symbol": "TCS", "target_price": 100, "direction": "Below"}, True), ({"symbol": "TCS", "target_price": 100}, True),
                         ({"symbol": "TCS", "target_price": 100, "direction": "abve"}, False), ({"symbol": "TCS", "target_price": 100, "direction": "sideways"}, False)):
            added.clear()
            if ok:
                run(f(Req(body)))
                assert added and added[0][2] in ("above", "below")
            else:
                try:
                    run(f(Req(body)))
                    assert False, body
                except HTTPException as e:
                    assert e.status_code == 400 and not added
    finally:
        if saved is None:
            sys.modules.pop("data_feed", None)
        else:
            sys.modules["data_feed"] = saved


# ---- 4. quote loop round robin ---------------------------------------------------------------------------------------------------------------
def _helper():
    ns = {"_QUOTE_TICK_MAX": 12}
    exec(_lift("main.py", "_quote_tick_batch"), ns)
    return ns["_quote_tick_batch"]


def test_e468_short_lists_are_pushed_whole_every_tick():
    f = _helper()
    assert f([], 5) == ([], 0)
    assert f(["A", "B"], 7) == (["A", "B"], 0)
    assert f([f"S{i}" for i in range(12)], 3)[0] == [f"S{i}" for i in range(12)]


def test_e468_a_long_list_is_walked_round_robin_and_every_symbol_gets_a_turn():
    f = _helper()
    syms = [f"S{i:02d}" for i in range(30)]
    seen, cur = [], 0
    for _ in range(3):
        batch, cur = f(syms, cur)
        assert len(batch) == 12
        seen += batch
    assert set(seen) == set(syms) and f(syms, 0)[0] == syms[:12]


def test_e468_wrap_around_and_a_stale_cursor_after_the_list_shrinks():
    f = _helper()
    syms = [f"S{i:02d}" for i in range(14)]
    b, cur = f(syms, 10)
    assert b == syms[10:] + syms[:8] and cur == 8 and len(set(b)) == 12
    b2, cur2 = f(syms[:13], 999)
    assert len(b2) == 12 and 0 <= cur2 < 13
    assert f(syms, "junk")[0] == syms[:12] and f(syms, None)[0] == syms[:12]


def test_e468_the_loop_uses_the_helper_and_keeps_the_cap():
    src = _src("main.py")
    assert "_batch, _quote_loop_cursor = _quote_tick_batch(symbols, _quote_loop_cursor)" in src
    assert "list(symbols)[:12]" not in src and "_QUOTE_TICK_MAX = 12" in src and "global _quote_loop_cursor" in src


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(f"{n} passed")
