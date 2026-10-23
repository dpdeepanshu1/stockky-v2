"""E51: gateway proxy for the scheduler alert switches - admin session required, only switch paths, key forwarded, fail closed."""
import time

import jwt
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from gateway_routers import scheduler_control as sc

SECRET = "x" * 40
KEY = "k" * 20


def _tok(sub="deep"):
    return jwt.encode({"sub": sub, "iat": time.time(), "exp": time.time() + 600}, SECRET, algorithm="HS256")


class _Resp:
    status_code = 200
    text = "{}"

    def __init__(self, data):
        self._d = data

    def json(self):
        return self._d


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv("GATEWAY_ADMIN_GUARD", "true")
    monkeypatch.setenv("SESSION_SECRET", SECRET)
    monkeypatch.setenv("SCHEDULER_CONTROL_KEY", KEY)
    monkeypatch.setenv("SCHEDULER_CONTROL_URL", "http://sched/control")
    seen = {}

    class FakeClient:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, method, url, params=None, content=None, headers=None):
            seen.update(method=method, url=url, params=params, content=content, headers=headers)
            return _Resp({"ok": True, "echo": url})

    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)
    app = FastAPI()
    app.include_router(sc.scheduler_control_router)
    c = TestClient(app)
    c.seen = seen
    return c


def test_no_admin_session_is_401_and_nothing_is_forwarded(env):
    assert env.get("/scheduler-control/flags").status_code == 401
    assert env.post("/scheduler-control/flags/PATTERN_ALERTS_TIME", json={"value": "17:00"}).status_code == 401
    assert env.seen == {}


def test_get_flags_forwards_key_and_admin_name(env):
    r = env.get("/scheduler-control/flags", headers={"Authorization": f"Bearer {_tok('deep')}"})
    assert r.status_code == 200
    assert env.seen["url"] == "http://sched/control/flags" and env.seen["method"] == "GET"
    assert env.seen["headers"]["X-Control-Key"] == KEY and env.seen["headers"]["X-Admin-User"] == "deep"


def test_post_flag_body_is_forwarded_and_api_alias_works(env):
    h = {"Authorization": f"Bearer {_tok()}"}
    r = env.post("/api/scheduler-control/flags/WATCHLIST_MOVE_ALERT_PCT", headers=h, json={"value": 7})
    assert r.status_code == 200 and env.seen["url"].endswith("/flags/WATCHLIST_MOVE_ALERT_PCT")
    assert b'"value"' in env.seen["content"]


@pytest.mark.parametrize("path,method", [("flags/../../health", "GET"), ("secrets", "GET"), ("flags/lower_case", "POST"), ("flags/A/B", "POST")])
def test_only_switch_paths_are_forwarded(env, path, method):
    r = env.request(method, f"/scheduler-control/{path}", headers={"Authorization": f"Bearer {_tok()}"})
    assert r.status_code == 404 and env.seen == {}


def test_wrong_method_is_405(env):
    h = {"Authorization": f"Bearer {_tok()}"}
    assert env.delete("/scheduler-control/flags", headers=h).status_code == 405
    assert env.get("/scheduler-control/flags/PATTERN_ALERTS_TIME", headers=h).status_code == 405
    assert env.get("/scheduler-control/preset/recommended", headers=h).status_code == 405


def test_missing_or_short_key_is_503(env, monkeypatch):
    h = {"Authorization": f"Bearer {_tok()}"}
    monkeypatch.setenv("SCHEDULER_CONTROL_KEY", "short")
    assert env.get("/scheduler-control/flags", headers=h).status_code == 503
    monkeypatch.delenv("SCHEDULER_CONTROL_KEY")
    assert env.get("/scheduler-control/flags", headers=h).status_code == 503


def test_unreachable_scheduler_is_502(env, monkeypatch):
    import httpx

    class Boom:
        def __init__(self, **kw): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *a): return False
        async def request(self, *a, **k): raise OSError("down")

    monkeypatch.setattr(httpx, "AsyncClient", Boom)
    assert env.get("/scheduler-control/flags", headers={"Authorization": f"Bearer {_tok()}"}).status_code == 502


def test_every_route_in_the_router_carries_the_admin_guard():
    from admin_guard import require_admin
    assert any(d.dependency is require_admin for d in sc.scheduler_control_router.dependencies)
