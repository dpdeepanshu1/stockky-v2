"""group 453: the gateway's dangerous routes need the admin session token (see admin_guard.py)."""
import re
import time
from pathlib import Path

import jwt
import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient

import admin_guard

SECRET = "x" * 40
_SERVICE = Path(__file__).resolve().parent.parent
MAIN_SRC = (_SERVICE / "main.py").read_text(encoding="utf-8")
# E81: guarded handlers moved out of main.py into gateway_support/bound_routes_*.py (decorated on their own `<handler>_router`), so the sweep reads those files too.
SUPPORT_SRC = "\n".join(p.read_text(encoding="utf-8") for p in sorted((_SERVICE / "gateway_support").glob("bound_routes*.py")))
# E150: ipo_routes.py (gateway_routers) carries guarded routes too (IPO purge)
SUPPORT_SRC += "\n" + "\n".join(p.read_text(encoding="utf-8") for p in sorted((_SERVICE / "gateway_routers").glob("*.py")))


def _token(secret=SECRET, sub="admin", exp_in=600, **extra):
    payload = {"sub": sub, "iat": time.time(), "exp": time.time() + exp_in, **extra}
    return jwt.encode(payload, secret, algorithm="HS256")


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setenv("GATEWAY_ADMIN_GUARD", "true")
    monkeypatch.setenv("SESSION_SECRET", SECRET)


@pytest.fixture
def client(guard_on):
    app = FastAPI()

    @app.post("/protected", dependencies=[Depends(admin_guard.require_admin)])
    def protected():
        return {"ok": True}

    @app.get("/open")
    def open_route():
        return {"ok": True}

    return TestClient(app)


def _bearer(token):
    return {"Authorization": f"Bearer {token}"}


def test_valid_token_passes(client):
    assert client.post("/protected", headers=_bearer(_token())).status_code == 200


def test_missing_header_is_401(client):
    assert client.post("/protected").status_code == 401


@pytest.mark.parametrize("header", ["Basic abc", "Bearer", "bearer abc", "Token abc"])
def test_wrong_scheme_is_401(client, header):
    assert client.post("/protected", headers={"Authorization": header}).status_code == 401


def test_garbage_token_is_401(client):
    assert client.post("/protected", headers=_bearer("not.a.jwt")).status_code == 401


def test_expired_token_is_401(client):
    assert client.post("/protected", headers=_bearer(_token(exp_in=-5))).status_code == 401


def test_token_signed_with_other_secret_is_401(client):
    assert client.post("/protected", headers=_bearer(_token(secret="y" * 40))).status_code == 401


def test_token_without_exp_is_401(client):
    t = jwt.encode({"sub": "admin"}, SECRET, algorithm="HS256")
    assert client.post("/protected", headers=_bearer(t)).status_code == 401


def test_token_without_sub_is_401(client):
    t = jwt.encode({"exp": time.time() + 600}, SECRET, algorithm="HS256")
    assert client.post("/protected", headers=_bearer(t)).status_code == 401


def test_alg_none_token_is_401(client):
    t = jwt.encode({"sub": "admin", "exp": time.time() + 600}, None, algorithm="none")
    assert client.post("/protected", headers=_bearer(t)).status_code == 401


def test_missing_secret_fails_closed_with_503(client, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    assert client.post("/protected", headers=_bearer(_token())).status_code == 503
    assert client.post("/protected").status_code == 503


def test_kill_switch_turns_the_guard_off(client, monkeypatch):
    monkeypatch.setenv("GATEWAY_ADMIN_GUARD", "false")
    assert client.post("/protected").status_code == 200


def test_open_routes_stay_open(client):
    assert client.get("/open").status_code == 200


def _main_protected_paths():
    """Paths whose decorator (in main.py, or in a gateway_support/bound_routes_*.py factory since E81) carries dependencies=[Depends(_require_admin)]."""
    found = set()
    pattern = r'^\s*@(?:app|[a-z0-9_]+_router)\.(?:post|delete|api_route)\("([^"]+)"([^\n]*)\)$'
    for src in (MAIN_SRC, SUPPORT_SRC):
        for m in re.finditer(pattern, src, re.M):
            if "require_admin" in m.group(2):
                found.add(m.group(1))
    return found


def test_main_protects_exactly_the_documented_routes():
    assert _main_protected_paths() == set(admin_guard.PROTECTED)


def test_every_protected_route_is_registered_on_the_app():
    import main  # heavy import, same as the other route tests
    guarded = {}
    for route in main.app.routes:
        deps = [d.dependency for d in getattr(route, "dependencies", [])]
        guarded[route.path] = admin_guard.require_admin in deps
    for path in admin_guard.PROTECTED:
        assert guarded.get(path) is True, f"{path} is not behind the admin guard"


# ---- A4b: per-service secrets -------------------------------------------------------------------------------------------------
RT_SECRET, PS_SECRET, CR_SECRET = "r" * 40, "p" * 40, "c" * 40


def _kid_token(secret, kid):
    return jwt.encode({"sub": "admin", "iat": time.time(), "exp": time.time() + 600}, secret, algorithm="HS256", headers={"kid": kid})


@pytest.fixture
def per_service(client, monkeypatch):
    monkeypatch.delenv("SESSION_SECRET", raising=False)  # legacy secret gone: only kid tokens can pass
    monkeypatch.setenv("SESSION_SECRET_REAL_TRADE", RT_SECRET)
    monkeypatch.setenv("SESSION_SECRET_POSITION_STOCKS", PS_SECRET)
    monkeypatch.setenv("SESSION_SECRET_CALIBRATION_RUNNER", CR_SECRET)  # as if env_file leaked it into the gateway
    return client


def test_a4b_tokens_from_both_login_services_pass(per_service):
    assert per_service.post("/protected", headers=_bearer(_kid_token(RT_SECRET, "real-trade"))).status_code == 200
    assert per_service.post("/protected", headers=_bearer(_kid_token(PS_SECRET, "position-stocks"))).status_code == 200


def test_a4b_runner_token_is_refused_even_if_its_secret_leaks_here(per_service):
    assert per_service.post("/protected", headers=_bearer(_kid_token(CR_SECRET, "calibration-runner"))).status_code == 401


def test_a4b_wrong_key_for_kid_and_legacy_token_are_refused(per_service):
    assert per_service.post("/protected", headers=_bearer(_kid_token(PS_SECRET, "real-trade"))).status_code == 401
    assert per_service.post("/protected", headers=_bearer(_token())).status_code == 401  # legacy token, legacy secret removed
