"""E498: a read of `gateway_support/bound_routes.py`, `_b`, `_e` and `_g` in full (E475 had only pattern-swept them).

1. `POST /ops/qstash/publish`: JSON `Infinity` / `1e999` parses to inf and `int(inf)` raised OverflowError (bare 500); now the same 400 as any bad delay.
2. Notification-service proxies (`GET /notifications/config`, `POST /notifications/config`, `DELETE /notifications/config/{channel}`, `/notifications/call/me`,
   `POST /notifications/test`): a 200 with a non-JSON body raised ValueError (bare 500); now 502 like `/notifications/health` already answered.
3. `GET /scan/status/{task_id}`: a null / text counter on a running task raised TypeError, an inf elapsed gave an inf estimate, a truthy non-dict record raised
   AttributeError, and NaN in the record failed the JSON write (all 500).
4. `GET /scan/last`, `GET /stockky-hot/result`, `GET /notifications/config`: NaN / inf in a stored result failed the response (500); now null (as E497 did for /surprise/scan).
5. `POST /data-feed/hard-reset`: `store.set_job` / `store.set_meta` (durable DB reads + writes) ran on the event loop inside an async route; now a worker thread.

No fastapi / httpx needed (handlers lifted from the source by AST, with a stand-in `ref`).
Runs with pytest or `python3 tests/test_e498_route_proxies_scan_status_hard_reset.py`.
"""
from __future__ import annotations

import ast
import asyncio
import json
import math
import os
import sys
import threading
import types

_SERVICE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SERVICE not in sys.path:
    sys.path.insert(0, _SERVICE)

from json_safe import sanitize  # noqa: E402

NAN, INF = float("nan"), float("inf")
_SUP = os.path.join(_SERVICE, "gateway_support")


class HTTPException(Exception):
    def __init__(self, status_code, detail=None):
        super().__init__(f"{status_code}: {detail}")
        self.status_code, self.detail = status_code, detail


class _JSONResponse:
    def __init__(self, content, status_code=200):
        self.content, self.status_code = content, status_code


class _HTTPError(Exception):
    pass


class _Timeout(_HTTPError):
    pass


def _handler(fname, factory, name, ref):
    tree = ast.parse(open(os.path.join(_SUP, fname), encoding="utf-8").read())
    build = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == factory)
    fn = next(n for n in build.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name)
    fn.decorator_list = []
    ns = {"ref": ref, "JSONResponse": _JSONResponse, "Request": object, "Response": object, "asyncio": asyncio}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), fname, "exec"), ns)
    return ns[name]


def _strict(obj):
    return json.dumps(obj, allow_nan=False)


class _Resp:
    def __init__(self, body=None, bad_json=False, status=200):
        self._body, self._bad, self.status_code = body, bad_json, status
        self.text = "<html>oops</html>"

    def raise_for_status(self):
        return None

    def json(self):
        if self._bad:
            raise json.JSONDecodeError("Expecting value", "<html>", 0)
        return self._body


def _ref(**kw):
    httpx = types.SimpleNamespace(HTTPError=_HTTPError, TimeoutException=_Timeout)
    base = dict(httpx=httpx, HTTPException=HTTPException, NOTIFICATION_URL="http://n", asyncio=asyncio, math=math,
                _json_sanitize=sanitize, logger=types.SimpleNamespace(debug=lambda *a, **k: None, warning=lambda *a, **k: None,
                                                                      exception=lambda *a, **k: None),
                Optional=object, Request=object, BackgroundTasks=object, NotificationChannelUpdate=object)
    base.update(kw)
    return types.SimpleNamespace(**base)


def _run(coro_or_val):
    return asyncio.run(coro_or_val) if asyncio.iscoroutine(coro_or_val) else coro_or_val


# ── 1. qstash delay ──────────────────────────────────────────────────────────

class _Req:
    def __init__(self, body):
        self._b = body

    async def json(self):
        return self._b


class _Q:
    def __init__(self):
        self.calls = []

    def enabled(self):
        return True

    def schedule_gateway_tick(self, delay_seconds=0, body=None):
        self.calls.append(("tick", delay_seconds))
        return {"ok": True}

    def publish(self, dest, payload, delay_seconds=0):
        self.calls.append(("pub", dest, delay_seconds))
        return {"ok": True}


def _qstash(body):
    q = _Q()
    ref = _ref(qstash_client=q)
    h = _handler("bound_routes.py", "build_bound_routes", "ops_qstash_publish", ref)
    return asyncio.run(h(_Req(body))), q


def test_qstash_infinite_delay_is_400_not_500():
    for bad in (INF, -INF, 1e999, NAN, "abc"):
        res, q = _qstash({"delay_seconds": bad})
        assert res.status_code == 400 and q.calls == []


def test_qstash_good_delay_unchanged():
    res, q = _qstash({"delay_seconds": 7})
    assert res == {"ok": True} and q.calls == [("tick", 7)]
    res, q = _qstash({"url": "http://x", "delay_seconds": "3"})
    assert q.calls == [("pub", "http://x", 3)]


# ── 2. notification proxies: non-JSON 200 body ───────────────────────────────

def _raises_502(fn, *a, **k):
    try:
        _run(fn(*a, **k))
    except HTTPException as e:
        return e.status_code == 502
    return False


def _http(resp, bad_methods=("get", "post", "delete")):
    def mk(_):
        return lambda *a, **k: resp
    return types.SimpleNamespace(HTTPError=_HTTPError, TimeoutException=_Timeout, **{m: mk(m) for m in bad_methods})


def test_notification_config_get_non_json_is_502():
    ref = _ref(httpx=_http(_Resp(bad_json=True)))
    assert _raises_502(_handler("bound_routes_b.py", "build_bound_routes_b", "get_notification_config", ref))


def test_notification_config_get_nan_is_null_and_good_body_unchanged():
    ref = _ref(httpx=_http(_Resp({"a": NAN, "b": [INF, 1], "ok": True})))
    out = _handler("bound_routes_b.py", "build_bound_routes_b", "get_notification_config", ref)()
    assert out == {"a": None, "b": [None, 1], "ok": True}
    _strict(out)
    ref = _ref(httpx=_http(_Resp({"telegram": {"enabled": True}})))
    assert _handler("bound_routes_b.py", "build_bound_routes_b", "get_notification_config", ref)() == {"telegram": {"enabled": True}}


class _Upd:
    def model_dump(self, exclude_none=True):
        return {"channel": "x"}


def test_notification_g_proxies_non_json_is_502():
    ref = _ref(httpx=_http(_Resp(bad_json=True)))
    g = lambda n: _handler("bound_routes_g.py", "build_bound_routes_g", n, ref)  # noqa: E731
    assert _raises_502(g("set_notification_config"), _Upd())
    assert _raises_502(g("delete_notification_channel"), "telegram")
    assert _raises_502(g("notifications_call_me"), None, "hi")
    assert _raises_502(g("test_notification_channels"))


def test_notification_g_proxies_good_body_unchanged():
    ref = _ref(httpx=_http(_Resp({"ok": True})))
    g = lambda n: _handler("bound_routes_g.py", "build_bound_routes_g", n, ref)  # noqa: E731
    assert g("set_notification_config")(_Upd()) == {"ok": True}
    assert g("delete_notification_channel")("telegram") == {"ok": True}
    assert g("notifications_call_me")(None, "hi") == {"ok": True}
    assert g("test_notification_channels")() == {"ok": True}


# ── 3. scan status ───────────────────────────────────────────────────────────

def _status(stored):
    ref = _ref(_redis_get=lambda k: stored, SCAN_TASK_PREFIX="scan:")
    return _handler("bound_routes_e.py", "build_bound_routes_e", "get_scan_status", ref)("t1")


def test_scan_status_good_running_estimate_unchanged():
    out = _status({"status": "running", "processed": 10, "total": 50, "elapsed": 20})
    assert out["estimated_remaining"] == 80.0
    out = _status({"status": "running", "processed": 0, "total": 50, "elapsed": 0})
    assert out["estimated_remaining"] is None
    assert _status({"status": "done", "processed": 5})["status"] == "done"


def test_scan_status_unreadable_counters_do_not_500():
    for rec in ({"status": "running", "processed": None, "total": 5, "elapsed": 3},
                {"status": "running", "processed": "abc", "total": 5, "elapsed": 3},
                {"status": "running", "processed": 5, "total": None, "elapsed": 3},
                {"status": "running", "processed": 5, "total": 50, "elapsed": INF},
                {"status": "running", "processed": NAN, "total": 50, "elapsed": 3}):
        out = _status(rec)
        assert out["estimated_remaining"] is None
        _strict(out)


def test_scan_status_non_dict_record_is_the_soft_unknown_response():
    for rec in ("running", [1, 2], 7):
        out = _status(rec)
        assert out["status"] == "unknown" and out["ok"] is False


def test_scan_status_nan_in_record_is_null():
    out = _status({"status": "done", "processed": 3, "avg": NAN})
    assert out["avg"] is None
    _strict(out)


# ── 4. stored results with NaN ───────────────────────────────────────────────

def test_last_scan_nan_in_stored_result_is_null():
    stored = {"task_id": "t", "scanned_at": "x", "processed": 2, "total": 2, "result": {"rows": [{"price": NAN, "chg": INF, "s": "A"}]}}
    ref = _ref(_redis_get=lambda k: stored, LAST_FULL_SCAN_KEY="k")
    out = _handler("bound_routes_e.py", "build_bound_routes_e", "get_last_scan", ref)()
    assert out["result"]["rows"][0] == {"price": None, "chg": None, "s": "A"} and out["ok"] is True
    _strict(out)


def test_last_scan_good_result_unchanged():
    stored = {"task_id": "t", "scanned_at": "x", "processed": 2, "total": 2, "result": {"rows": [{"price": 1.5}]}}
    ref = _ref(_redis_get=lambda k: stored, LAST_FULL_SCAN_KEY="k")
    out = _handler("bound_routes_e.py", "build_bound_routes_e", "get_last_scan", ref)()
    assert out == {"ok": True, "task_id": "t", "scanned_at": "x", "partial": False, "processed": 2, "total": 2, "result": {"rows": [{"price": 1.5}]}}


def test_hot_result_nan_is_null_and_good_unchanged():
    stored = {"generated_at": "g", "picks": [{"score": NAN, "sym": "A"}, {"score": 7.5, "sym": "B"}]}
    ref = _ref(_redis_get=lambda k: stored, HOT_RESULT_KEY="a", HOT_STOCKS_CACHE_KEY="b")
    out = _handler("bound_routes_e.py", "build_bound_routes_e", "stockky_hot_result", ref)()
    assert out["picks"] == [{"score": None, "sym": "A"}, {"score": 7.5, "sym": "B"}] and out["ok"] and out["cached"]
    _strict(out)


# ── 5. hard reset: store writes off the event loop ───────────────────────────

def test_hard_reset_store_writes_run_in_a_worker_thread():
    main_tid = threading.get_ident()
    seen = []

    class _Store:
        def set_job(self, **kw):
            seen.append(("job", threading.get_ident(), kw["status"]))

        def set_meta(self, **kw):
            seen.append(("meta", threading.get_ident(), kw["stock_count"]))

    kv = types.ModuleType("kv_cache")
    kv.hard_reset_stockky_kv = lambda preserve_days=7: {"status": "success"}
    df = types.ModuleType("data_feed")
    df.clear_local_data_feed_caches = df.request_data_feed_stop = df.clear_data_feed_stop = lambda: None
    saved = {k: sys.modules.get(k) for k in ("kv_cache", "data_feed")}
    sys.modules["kv_cache"], sys.modules["data_feed"] = kv, df
    try:
        ref = _ref(SCAN_UNIVERSE_KEY="u", _kv_cache=None, _redis=None, _feed_store=lambda: _Store())
        out = asyncio.run(_handler("bound_routes_g.py", "build_bound_routes_g", "hard_reset_database", ref)(7))
    finally:
        for k, v in saved.items():
            if v is None:
                sys.modules.pop(k, None)
            else:
                sys.modules[k] = v
    assert [s[0] for s in seen] == ["job", "meta"]          # same order as before
    assert all(s[1] != main_tid for s in seen)              # not on the event-loop thread
    assert seen[0][2] == "idle" and seen[1][2] == 0         # same values as before
    assert out["status"] == "success" and "ghosts_cleared" in out


if __name__ == "__main__":
    fails = 0
    for _n, _f in sorted(globals().items()):
        if _n.startswith("test_") and callable(_f):
            try:
                _f()
                print("PASS", _n)
            except Exception as _e:  # noqa: BLE001
                fails += 1
                print("FAIL", _n, type(_e).__name__, str(_e)[:160])
    sys.exit(1 if fails else 0)
