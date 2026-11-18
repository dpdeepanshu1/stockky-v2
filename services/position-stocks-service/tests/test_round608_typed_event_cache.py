"""Round 608: the quality gate asks the event feed's /events/{symbol}/active once per TYPED_EVENT_CACHE_S per symbol (screening/quality_gate.py).

Plain runner: `python tests/test_round608_typed_event_cache.py` (also collected by pytest). Where httpx / sqlalchemy are missing (a bare sandbox) they are
stubbed so the module imports; on the VM the real ones are used."""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import types

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))


def _stub_missing(*names):
    for n in names:
        try:
            importlib.import_module(n)
        except Exception:  # noqa: BLE001
            parts = n.split(".")
            for i in range(1, len(parts) + 1):
                m = ".".join(parts[:i])
                if m not in sys.modules:
                    mod = types.ModuleType(m)
                    mod.__getattr__ = lambda attr: type(attr, (), {})      # any name imported from a stub is a dummy class
                    sys.modules[m] = mod


try:
    import sqlalchemy  # noqa: F401
    _REAL_SQLA = True
except Exception:  # noqa: BLE001
    _REAL_SQLA = False
_stub_missing("httpx", "sqlalchemy", "sqlalchemy.orm")
if not _REAL_SQLA:      # the ORM models cannot be built without sqlalchemy, and this test does not touch the database
    _m = types.ModuleType("models")
    _m.__getattr__ = lambda attr: type(attr, (), {})
    sys.modules["models"] = _m
import config  # noqa: E402
from screening import quality_gate as qg  # noqa: E402

_CALLS: list = []


class _Resp:
    def __init__(self, status, body):
        self.status_code, self._b = status, body

    def json(self):
        return self._b


class _Client:
    plan: list = []          # responses handed out in order; the last one repeats

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url, timeout=None):
        _CALLS.append(url)
        i = min(len(_CALLS) - 1, len(_Client.plan) - 1)
        r = _Client.plan[i]
        if isinstance(r, Exception):
            raise r
        return r


def _setup(ttl, plan):
    config.TYPED_EVENT_CACHE_S = ttl
    config.EVENT_URL = "http://ev"
    config.QUALITY_GATE_TIMEOUT_S = 1.0
    qg.reset_runtime_state()
    _CALLS.clear()
    _Client.plan = plan
    qg.httpx.AsyncClient = _Client


def _ok_body():
    return {"ok": True, "events": [{"type": "results", "published": "2026-11-18T04:00:00Z", "polarity": 1, "strength": 0.8}]}


def _run(c):
    return asyncio.run(c)


def test_second_call_inside_the_ttl_does_not_ask_the_feed_again():
    _setup(45.0, [_Resp(200, _ok_body())])
    a = _run(qg._active_body("ABC"))
    b = _run(qg._active_body("ABC"))
    assert a == b and len(_CALLS) == 1


def test_typed_event_and_results_time_share_one_request():
    _setup(45.0, [_Resp(200, _ok_body())])
    _run(qg._fetch_typed_event("ABC"))
    out = _run(qg._fetch_result_out_at("ABC"))
    assert len(_CALLS) == 1 and out is not None


def test_a_different_symbol_is_asked_separately():
    _setup(45.0, [_Resp(200, _ok_body())])
    _run(qg._active_body("ABC"))
    _run(qg._active_body("XYZ"))
    assert len(_CALLS) == 2


def test_ttl_expiry_asks_again():
    _setup(45.0, [_Resp(200, _ok_body())])
    _run(qg._active_body("ABC"))
    ts, body = qg._active_cache["ABC"]
    qg._active_cache["ABC"] = (ts - 46.0, body)
    _run(qg._active_body("ABC"))
    assert len(_CALLS) == 2


def test_failures_and_not_ok_bodies_are_not_kept():
    _setup(45.0, [_Resp(500, {}), _Resp(200, {"ok": False}), _Resp(200, _ok_body())])
    assert _run(qg._fetch_typed_event("ABC")) is None          # 500
    assert _run(qg._fetch_typed_event("ABC")) is None          # ok:false
    assert "ABC" not in qg._active_cache
    assert _run(qg._fetch_typed_event("ABC")) is not None      # third answer is good and is kept
    _run(qg._fetch_typed_event("ABC"))
    assert len(_CALLS) == 3


def test_transport_error_never_raises_and_is_not_kept():
    _setup(45.0, [RuntimeError("down"), _Resp(200, _ok_body())])
    assert _run(qg._fetch_typed_event("ABC")) is None
    assert "ABC" not in qg._active_cache
    assert _run(qg._fetch_result_out_at("ABC")) is not None     # asked again at once, the second answer is good
    assert len(_CALLS) == 2 and "ABC" in qg._active_cache


def test_ttl_zero_is_the_old_behaviour_ask_every_time():
    _setup(0.0, [_Resp(200, _ok_body())])
    _run(qg._active_body("ABC"))
    _run(qg._active_body("ABC"))
    assert len(_CALLS) == 2 and not qg._active_cache


def test_reset_runtime_state_clears_it_and_config_default_is_45():
    _setup(45.0, [_Resp(200, _ok_body())])
    _run(qg._active_body("ABC"))
    assert qg._active_cache
    qg.reset_runtime_state()
    assert not qg._active_cache
    src = open(os.path.join(os.path.dirname(_HERE), "config.py"), encoding="utf-8").read()
    assert '_get_float("TYPED_EVENT_CACHE_S", 45.0)' in src


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
