"""Session 72 (#9): end-to-end admin-auth check across BOTH services, in subprocesses
(each service has its own top-level `config`/`main` modules, so they can't share a process).
Proves: login works, /dhan/funds is admin-gated and maps Dhan errors to 409/502 (never 401),
and a token minted by one service is accepted by the other (same SESSION_SECRET)."""
import json, os, subprocess, sys
import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVICES = os.path.dirname(HERE)
PS, RT = HERE, os.path.join(SERVICES, "real-trade-service")


def _env():
    from argon2 import PasswordHasher
    e = dict(os.environ)
    e.update({"ADMIN_USERNAME": "admin", "ADMIN_PASSWORD_HASH": PasswordHasher().hash("s3cret-pw"),
              "SESSION_SECRET": "x" * 48, "DATABASE_URL": ""})
    return e


def _run(cwd, code, env):
    r = subprocess.run([sys.executable, "-c", code], cwd=cwd, env=env, capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-1500:] + r.stdout[-500:]
    return json.loads(r.stdout.strip().splitlines()[-1])


PS_CODE = '''
import sys, json; sys.path.insert(0, ".")
from fastapi.testclient import TestClient
import main
from execution import dhan_client
main.app.dependency_overrides[main.get_db] = lambda: None
c = TestClient(main.app)
out = {}
out["bad_login"] = c.post("/auth/login", json={"username": "admin", "password": "nope"}).status_code
r = c.post("/auth/login", json={"username": "admin", "password": "s3cret-pw"}); out["login"] = r.status_code
tok = r.json()["token"]; H = {"Authorization": f"Bearer {tok}"}
out["funds_no_token"] = c.get("/dhan/funds").status_code
dhan_client.get_funds = lambda db: {"availabelBalance": 1234.5}
out["funds_ok"] = c.get("/dhan/funds", headers=H).json()
def boom(db): raise RuntimeError("Dhan API error: DH-901 Invalid_Authentication")
dhan_client.get_funds = boom
out["funds_dhan_error"] = c.get("/dhan/funds", headers=H).status_code
def nc(db): raise dhan_client.DhanNotConnectedError("not connected")
dhan_client.get_funds = nc
out["funds_not_connected"] = c.get("/dhan/funds", headers=H).status_code
out["diag"] = c.get("/auth/config-check").json()
out["token"] = tok
print(json.dumps(out))
'''

RT_CODE = '''
import sys, json; sys.path.insert(0, ".")
from auth import admin_auth
tok = sys.argv[1] if len(sys.argv) > 1 else None
print(json.dumps({"accepted": admin_auth.decode_session_token(TOKEN), "diag": admin_auth.auth_config_diagnostics()}))
'''


def test_position_stocks_admin_auth_and_funds_route():
    out = _run(PS, PS_CODE, _env())
    assert out["bad_login"] == 401 and out["login"] == 200
    assert out["funds_no_token"] == 401
    assert out["funds_ok"] == {"availabelBalance": 1234.5}
    assert out["funds_dhan_error"] == 502          # never 401: the dashboard would drop the admin session
    assert out["funds_not_connected"] == 409
    d = out["diag"]
    assert d["admin_password_hash_looks_argon2"] and d["session_secret_fingerprint"] and "x" * 8 not in json.dumps(d)


def test_token_from_position_stocks_is_accepted_by_real_trade_service():
    env = _env()
    tok = _run(PS, PS_CODE, env)["token"]
    out = _run(RT, RT_CODE.replace("TOKEN", repr(tok)), env)
    assert out["accepted"] == "admin"
    ps_fp = _run(PS, PS_CODE, env)["diag"]["session_secret_fingerprint"]
    assert out["diag"]["session_secret_fingerprint"] == ps_fp
    env2 = dict(env, SESSION_SECRET="y" * 48)      # what a SESSION_SECRET mismatch looks like
    assert _run(RT, RT_CODE.replace("TOKEN", repr(tok)), env2)["accepted"] is None
