"""group 357 (WS-4): the find-buys route's structure enrichment (`_sniper_attach_structure` in main.py). main.py needs fastapi, so the helper is
lifted out of the source by AST and run against fakes for the technical service - no network, no fastapi. Plain asserts; runs with pytest or
`python3 tests/test_group357_sniper_route_enrich.py`."""
from __future__ import annotations

import ast
import asyncio
import importlib.util
import logging
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
_KEYS = ("SNIPER_STRUCTURE_MODE", "SNIPER_STRUCTURE_FETCH_LIMIT", "SNIPER_STRUCTURE_FETCH_TIMEOUT_S", "STRUCTURE_PASSTHROUGH",
         "SNIPER_MIN_CONVICTION", "SNIPER_MIN_PRICE", "MAX_PRICE", "STRUCTURE_MAX_CHASE_ATR", "STRUCTURE_MAX_STRETCH_ATR",
         "STRUCTURE_MIN_ROOM_ATR", "STRUCTURE_MIN_RR", "STRUCTURE_BEARISH_BLOCK", "STRUCTURE_BEARISH_BOS_BARS", "STRUCTURE_MAX_AGE_DAYS",
         "STRUCTURE_MAX_PRICE_DEV_PCT", "STRUCTURE_REJECT_STOP_WIDE")
_MODS = ("buy_sniper", "ms_passthrough")
_ORIG_PATH = list(sys.path)
for _k in _KEYS:  # clean slate for the plain `python3` runner; the pytest fixture below restores everything afterwards
    os.environ.pop(_k, None)
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

try:
    import pytest

    @pytest.fixture(autouse=True)
    def _restore_process_state():
        saved_env = {k: os.environ.get(k) for k in _KEYS}
        saved_mods = {k: sys.modules.get(k) for k in _MODS}
        yield
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        for k, v in saved_mods.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
except ImportError:  # pragma: no cover
    pass


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SERVICE, rel))
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


buy_sniper = _load("buy_sniper", "buy_sniper.py")
ms_passthrough = _load("ms_passthrough", "ms_passthrough.py")

BLOCK = {"schema_version": 1, "quality": "ok", "engine_version": "t", "params_hash": "h", "trend": "bullish", "atr": 2.0, "last_close": 100.0,
         "data_quality": {"last_bar_ts": None},
         "location": {"entry_location_score": 70, "chase_atr": 3.0, "stretch_atr": 1.0, "room_atr": 3.0},
         "structure_rr": {"rr": 3.0, "stop": 96.0, "target": 112.0, "reward_pct": 12.0, "flags": []}}


def _helper_source() -> str:
    src = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
    tree = ast.parse(src)
    for n in tree.body:
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "_sniper_attach_structure":
            return ast.get_source_segment(src, n)
    raise AssertionError("_sniper_attach_structure not found in main.py")


class _Resp:
    def __init__(self, status, data):
        self.status_code, self._d = status, data

    def json(self):
        return self._d


class _Client:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


def _env(fake_get):
    httpx = types.SimpleNamespace(AsyncClient=_Client)
    g = {"asyncio": asyncio, "os": os, "httpx": httpx, "ms_passthrough": ms_passthrough, "TECHNICAL_URL": "http://tech",
         "logger": logging.getLogger("t"), "Dict": dict, "Any": object, "_cb_get": fake_get, "buy_sniper": buy_sniper,
         "_structure_metric": lambda *a, **k: None}  # group360: counter helper, a no-op here
    sys.modules["buy_sniper"] = buy_sniper
    exec("from __future__ import annotations\n" + _helper_source(), g)
    return g["_sniper_attach_structure"]


def _rows():
    mk = lambda sym, conv: {"symbol": sym, "decision": "BUY", "conviction": conv, "price": 100.0}
    return [mk("AAA", 90), mk("BBB", 80), mk("CCC", 70)]


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def test_attaches_summary_and_gate_then_blocks_the_chaser():
    calls = []

    async def fake_get(client, name, url, timeout=5.0, **kw):
        calls.append(url)
        return _Resp(200, {"market_structure": BLOCK})

    fn = _env(fake_get)
    payload = {"stocks": _rows()}
    assert run(fn(payload)) == 3 and len(calls) == 3 and calls[0].startswith("http://tech/structure/")
    assert all(r["structure_summary"]["ms_quality"] == "ok" for r in payload["stocks"])
    out = buy_sniper.suggestions_from_scan_payload(payload)
    assert out["count"] == 0 and len(out["structure_rejected"]) == 3  # chase 3.0 ATR > 1.5 -> every card blocked


def test_failures_are_fail_open():
    async def boom(client, name, url, timeout=5.0, **kw):
        raise RuntimeError("down")

    async def bad(client, name, url, timeout=5.0, **kw):
        return _Resp(500, {})

    async def junk(client, name, url, timeout=5.0, **kw):
        return _Resp(200, {"market_structure": {"schema_version": 99}})

    for g in (boom, bad, junk):
        payload = {"stocks": _rows()}
        assert run(_env(g)(payload)) == 0
        assert all("structure_summary" not in r for r in payload["stocks"])
        assert buy_sniper.suggestions_from_scan_payload(payload)["count"] == 3  # no opinion = unchanged behaviour


def test_rows_with_a_summary_are_not_refetched_and_limit_is_honoured():
    calls = []

    async def fake_get(client, name, url, timeout=5.0, **kw):
        calls.append(url)
        return _Resp(200, {"market_structure": BLOCK})

    rows = _rows()
    rows[0]["structure_summary"] = {"ms_quality": "ok", "marker": 1}
    os.environ["SNIPER_STRUCTURE_FETCH_LIMIT"] = "1"
    try:
        assert run(_env(fake_get)({"stocks": rows})) == 1
    finally:
        os.environ.pop("SNIPER_STRUCTURE_FETCH_LIMIT", None)
    assert len(calls) == 1 and calls[0].endswith("/BBB") and rows[0]["structure_summary"]["marker"] == 1


def test_off_and_passthrough_disabled_fetch_nothing():
    calls = []

    async def fake_get(client, name, url, timeout=5.0, **kw):
        calls.append(url)
        return _Resp(200, {"market_structure": BLOCK})

    for k, v in (("SNIPER_STRUCTURE_MODE", "off"), ("STRUCTURE_PASSTHROUGH", "0")):
        os.environ[k] = v
        try:
            assert run(_env(fake_get)({"stocks": _rows()})) == 0
        finally:
            os.environ.pop(k, None)
    assert calls == []


def test_slow_upstream_is_cut_off_by_the_budget():
    async def slow(client, name, url, timeout=5.0, **kw):
        await asyncio.sleep(30)

    os.environ["SNIPER_STRUCTURE_FETCH_TIMEOUT_S"] = "1"
    try:
        import time
        t0 = time.monotonic()
        assert run(_env(slow)({"stocks": _rows()})) == 0
        assert time.monotonic() - t0 < 5
    finally:
        os.environ.pop("SNIPER_STRUCTURE_FETCH_TIMEOUT_S", None)


def test_route_calls_the_helper_before_the_filter():
    src = open(os.path.join(_SERVICE, "main.py"), encoding="utf-8").read()
    i = src.index("async def find_actionable_buys")
    body = src[i:i + 2500]
    assert body.index("_sniper_attach_structure(payload)") < body.index("suggestions_from_scan_payload(payload)")


if __name__ == "__main__":
    n = 0
    for k, v in sorted(globals().items()):
        if k.startswith("test_") and callable(v):
            v()
            n += 1
    print(f"{n} passed 0 failed")
