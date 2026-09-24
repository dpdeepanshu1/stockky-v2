"""
tests/test_config.py — direct unit tests for config.py's two remaining
untested pieces (coverage plan, real-trade-service round: config.py was
90%, missing lines 84-88 and 457-466).

Two separate things live here, tested two different ways:

1. The ADMIN_PASSWORD_HASH_B64 -> ADMIN_PASSWORD_HASH decode at module
   import time (lines 84-88). This code only runs ONCE, at import, off
   whatever ADMIN_PASSWORD_HASH_B64/ADMIN_PASSWORD_HASH env vars were set
   at that moment — no function wraps it. The only way to exercise both
   branches (decode succeeds / decode raises) is to set the env vars and
   importlib.reload(config) so the module body runs again. Every test that
   does this restores the original env and reloads config back to its
   normal state in a finally-block, so config's attributes are exactly
   as every other test file left them by the time this file's tests are
   done — no cross-test pollution.

2. startup_config_errors() (lines 457-466) — every existing caller
   (test_main_routes_core.py) monkeypatches this function away entirely
   rather than calling the real thing, so its own body has never run
   under test. This part needs no reload: it's an ordinary function that
   reads config's module globals at call time, so monkeypatch.setattr on
   the individual globals is enough.

Pure stdlib (config.py imports only `os`) — runs with plain python3, no
sqlalchemy/pytest required to hand-verify the logic.

Run from services/real-trade-service:
    python -m pytest tests/test_config.py -v
"""
from __future__ import annotations

import base64
import importlib
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config


# ── Part 1: ADMIN_PASSWORD_HASH_B64 decode at import time ──────────────────

@pytest.fixture()
def _restore_config_env():
    """Snapshot the two relevant env vars + config's live attribute values,
    yield, then put both back exactly as they were and reload config so
    later test files see the same config module state they always have."""
    saved_env = {
        "ADMIN_PASSWORD_HASH_B64": os.environ.get("ADMIN_PASSWORD_HASH_B64"),
        "ADMIN_PASSWORD_HASH": os.environ.get("ADMIN_PASSWORD_HASH"),
    }
    yield
    for key, val in saved_env.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    importlib.reload(config)


def test_b64_hash_is_decoded_when_set_and_plain_hash_is_not(_restore_config_env):
    raw_hash = "$argon2id$v=19$m=65536,t=3,p=4$saltsalt$hashhash"
    os.environ["ADMIN_PASSWORD_HASH_B64"] = base64.b64encode(raw_hash.encode("utf-8")).decode("ascii")
    os.environ.pop("ADMIN_PASSWORD_HASH", None)

    importlib.reload(config)

    assert config.ADMIN_PASSWORD_HASH == raw_hash


def test_invalid_b64_hash_falls_back_to_empty_string(_restore_config_env):
    """Garbage that base64/utf-8 decoding can't handle must not crash the
    whole service at import — it degrades to an empty hash (which
    startup_config_errors() then reports as a blocking error), not an
    unhandled exception on boot."""
    os.environ["ADMIN_PASSWORD_HASH_B64"] = "not-valid-base64-!!!"
    os.environ.pop("ADMIN_PASSWORD_HASH", None)

    importlib.reload(config)

    assert config.ADMIN_PASSWORD_HASH == ""


def test_plain_hash_env_var_takes_priority_over_b64(_restore_config_env):
    """Per the module docstring: 'Either input is accepted;
    ADMIN_PASSWORD_HASH takes priority if both are set.'"""
    plain_hash = "$argon2id$plain$hash"
    os.environ["ADMIN_PASSWORD_HASH"] = plain_hash
    os.environ["ADMIN_PASSWORD_HASH_B64"] = base64.b64encode(b"should-be-ignored").decode("ascii")

    importlib.reload(config)

    assert config.ADMIN_PASSWORD_HASH == plain_hash


def test_neither_env_var_set_gives_empty_hash(_restore_config_env):
    os.environ.pop("ADMIN_PASSWORD_HASH_B64", None)
    os.environ.pop("ADMIN_PASSWORD_HASH", None)

    importlib.reload(config)

    assert config.ADMIN_PASSWORD_HASH == ""


# ── Part 2: startup_config_errors() ─────────────────────────────────────────

class TestStartupConfigErrors:
    """Direct calls to the real function — no monkeypatching it away."""

    def test_all_four_checks_pass_returns_empty_list(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "$argon2id$ok")
        monkeypatch.setattr(config, "SESSION_SECRET", "a-real-secret")
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "a-real-key")
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", False)
        monkeypatch.setattr(config, "DHAN_TOTP_SECRET", "")

        assert config.startup_config_errors() == []

    def test_missing_admin_password_hash_is_reported(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
        monkeypatch.setattr(config, "SESSION_SECRET", "ok")
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "ok")
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", False)

        errors = config.startup_config_errors()
        assert any("ADMIN_PASSWORD_HASH is not set" in e for e in errors)
        assert len(errors) == 1

    def test_missing_session_secret_is_reported(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "ok")
        monkeypatch.setattr(config, "SESSION_SECRET", "")
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "ok")
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", False)

        errors = config.startup_config_errors()
        assert any("SESSION_SECRET is not set" in e for e in errors)
        assert len(errors) == 1

    def test_missing_dhan_credential_enc_key_is_reported(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "ok")
        monkeypatch.setattr(config, "SESSION_SECRET", "ok")
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "")
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", False)

        errors = config.startup_config_errors()
        assert any("DHAN_CREDENTIAL_ENC_KEY is not set" in e for e in errors)
        assert len(errors) == 1

    def test_totp_enabled_without_secret_is_reported(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "ok")
        monkeypatch.setattr(config, "SESSION_SECRET", "ok")
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "ok")
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", True)
        monkeypatch.setattr(config, "DHAN_TOTP_SECRET", "")

        errors = config.startup_config_errors()
        assert any("DHAN_TOTP_ENABLED=true but DHAN_TOTP_SECRET is not set" in e for e in errors)
        assert len(errors) == 1

    def test_totp_enabled_with_secret_set_is_not_reported(self, monkeypatch):
        """TOTP-enabled is fine as long as a secret is actually present."""
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "ok")
        monkeypatch.setattr(config, "SESSION_SECRET", "ok")
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "ok")
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", True)
        monkeypatch.setattr(config, "DHAN_TOTP_SECRET", "a-real-totp-secret")

        assert config.startup_config_errors() == []

    def test_everything_missing_reports_all_four(self, monkeypatch):
        monkeypatch.setattr(config, "ADMIN_PASSWORD_HASH", "")
        monkeypatch.setattr(config, "SESSION_SECRET", "")
        monkeypatch.setattr(config, "DHAN_CREDENTIAL_ENC_KEY", "")
        monkeypatch.setattr(config, "DHAN_TOTP_ENABLED", True)
        monkeypatch.setattr(config, "DHAN_TOTP_SECRET", "")

        errors = config.startup_config_errors()
        assert len(errors) == 4


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
