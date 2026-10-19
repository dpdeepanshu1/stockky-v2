"""group 460: "Change password" on the login page (auth/admin_override.py). Real Argon2id, no mocking of the crypto.

  - feature OFF                  -> nothing is read, login is the old env-hash check
  - feature ON, row stored       -> the stored password works, the env one stops working
  - any lookup problem           -> falls back to the env hash (never a lock-out)
  - ADMIN_IGNORE_PASSWORD_OVERRIDE=true -> lock-out recovery, the stored row is ignored
  - new-password rules and the wrong-attempt limiter
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest
from argon2 import PasswordHasher
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

import config
import db
import models
from auth import admin_auth, admin_override

_FAST = PasswordHasher(time_cost=1, memory_cost=8, parallelism=1)
ENV_PW = "env-password-1234"
NEW_PW = "brand-new-pass-99"


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(config, "ADMIN_USERNAME", "admin", raising=False)
    monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", _FAST.hash(ENV_PW), raising=False)
    monkeypatch.setattr(config, "ADMIN_PASSWORD_CHANGE_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "ADMIN_IGNORE_PASSWORD_OVERRIDE", False, raising=False)
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    models.SharedAdminCredential.__table__.create(eng)
    factory = sessionmaker(bind=eng, autoflush=False, autocommit=False, future=True)
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    admin_override.reset_rate_limit()
    yield factory


def _change(cur=ENV_PW, new=NEW_PW):
    admin_override.change_password("admin", cur, new, admin_auth.verify_admin_password)


def test_default_is_env_hash_only(_setup):
    assert admin_auth.verify_admin_password("admin", ENV_PW) is True
    assert admin_auth.verify_admin_password("admin", NEW_PW) is False


def test_change_then_new_works_and_old_stops(_setup):
    _change()
    assert admin_auth.verify_admin_password("admin", NEW_PW) is True
    assert admin_auth.verify_admin_password("admin", ENV_PW) is False


def test_change_again_needs_the_current_stored_password(_setup):
    _change()
    with pytest.raises(admin_override.ChangeRejected) as e:
        _change(cur=ENV_PW, new="another-long-pass-1")
    assert e.value.status == 401
    _change(cur=NEW_PW, new="another-long-pass-1")
    assert admin_auth.verify_admin_password("admin", "another-long-pass-1") is True


def test_stored_value_is_an_argon2_hash_not_the_password(_setup):
    _change()
    s = _setup()
    row = s.get(models.SharedAdminCredential, "admin")
    assert row.password_hash.startswith("$argon2") and NEW_PW not in row.password_hash
    s.close()


def test_feature_off_rejects_and_ignores_row(monkeypatch, _setup):
    _change()
    monkeypatch.setattr(config, "ADMIN_PASSWORD_CHANGE_ENABLED", False)
    assert admin_auth.verify_admin_password("admin", ENV_PW) is True
    assert admin_auth.verify_admin_password("admin", NEW_PW) is False
    with pytest.raises(admin_override.ChangeRejected) as e:
        _change(new="x" * 12)
    assert e.value.status == 403


def test_lockout_recovery_flag_ignores_row(monkeypatch, _setup):
    _change()
    monkeypatch.setattr(config, "ADMIN_IGNORE_PASSWORD_OVERRIDE", True)
    assert admin_auth.verify_admin_password("admin", ENV_PW) is True
    assert admin_auth.verify_admin_password("admin", NEW_PW) is False


def test_lookup_failure_falls_back_to_env(monkeypatch, _setup):
    _change()
    def boom():
        raise RuntimeError("db down")
    monkeypatch.setattr(db, "get_session_factory", boom)
    assert admin_override.stored_hash() is None
    assert admin_auth.verify_admin_password("admin", ENV_PW) is True


def test_missing_table_falls_back_to_env(monkeypatch):
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    factory = sessionmaker(bind=eng, future=True)
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    assert admin_override.stored_hash() is None
    assert admin_auth.verify_admin_password("admin", ENV_PW) is True


def test_garbage_row_is_ignored(_setup):
    s = _setup()
    s.add(models.SharedAdminCredential(key="admin", username="admin", password_hash="not-a-hash"))
    s.commit(); s.close()
    assert admin_auth.verify_admin_password("admin", ENV_PW) is True


@pytest.mark.parametrize("new,status", [("short", 422), (ENV_PW, 422), (" leading-space-pw1", 422), ("x" * 200, 422)])
def test_new_password_rules(new, status, _setup):
    with pytest.raises(admin_override.ChangeRejected) as e:
        _change(new=new)
    assert e.value.status == status
    assert admin_auth.verify_admin_password("admin", ENV_PW) is True


def test_wrong_current_is_rate_limited(_setup):
    for _ in range(5):
        with pytest.raises(admin_override.ChangeRejected) as e:
            _change(cur="wrong")
        assert e.value.status == 401
    with pytest.raises(admin_override.ChangeRejected) as e:
        _change()  # even the right current password is refused while limited
    assert e.value.status == 429


def test_save_failure_keeps_old_password(monkeypatch, _setup):
    factory = _setup
    class Bad:
        def __call__(self):
            s = factory()
            def boom(): raise RuntimeError("write failed")
            s.commit = boom
            return s
    calls = {"n": 0}
    def fac():
        calls["n"] += 1
        return Bad() if calls["n"] > 1 else factory  # first call (verify -> stored_hash) is a normal read
    monkeypatch.setattr(db, "get_session_factory", fac)
    with pytest.raises(admin_override.ChangeRejected) as e:
        _change()
    assert e.value.status == 503
    monkeypatch.setattr(db, "get_session_factory", lambda: factory)
    assert admin_auth.verify_admin_password("admin", ENV_PW) is True


def test_route_requires_admin_and_changes(monkeypatch, _setup):
    from fastapi.testclient import TestClient
    import main
    monkeypatch.setattr(config, "SESSION_SECRET", "s" * 40, raising=False)
    c = TestClient(main.app)
    r = c.post("/auth/change-password", json={"current_password": ENV_PW, "new_password": NEW_PW})
    assert r.status_code == 401
    token, _ = admin_auth.issue_session_token("admin")
    h = {"Authorization": f"Bearer {token}"}
    bad = c.post("/auth/change-password", json={"current_password": "nope", "new_password": NEW_PW}, headers=h)
    assert bad.status_code == 401
    ok = c.post("/auth/change-password", json={"current_password": ENV_PW, "new_password": NEW_PW}, headers=h)
    assert ok.status_code == 200 and ok.json() == {"ok": True}
    assert admin_auth.verify_admin_password("admin", NEW_PW) is True
