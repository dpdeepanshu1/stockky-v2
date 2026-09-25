"""
tests/test_config_getters.py

Covers config.py's two remaining gaps in the env-var coercion helpers
(missing lines 31-32, 38-39): the `except (TypeError, ValueError)` fallback
branch in `_get_float` and `_get_int`. Every existing test that touches
config.py exercises these only via well-formed env vars (or none at all),
so the "someone put garbage in the .env" fallback path was never hit.

Both helpers are pure and read `os.getenv` directly, so these are plain
monkeypatch.setenv tests against the already-imported `config` module —
no reimport/reload needed, unlike the ADMIN_PASSWORD_HASH_B64 module-level
block later in the file.

That module-level block (lines 471-475) only runs ONCE, at import, off
whatever ADMIN_PASSWORD_HASH_B64/ADMIN_PASSWORD_HASH env vars were set at
that moment — no function wraps it. The only way to exercise both branches
(decode succeeds / decode raises) is to set the env vars and
importlib.reload(config) so the module body runs again — same approach
already used in real-trade-service's tests/test_config.py for the
byte-for-byte identical block there. Every test that reloads restores the
original env and reloads config back to its normal state in a
finally-equivalent fixture teardown, so config's attributes are exactly as
every other test file in this suite left them by the time this file's
tests are done — no cross-test pollution.
"""
import base64
import importlib
import os

import pytest

import config


class TestGetFloat:
    def test_valid_value_parses(self, monkeypatch):
        monkeypatch.setenv("SOME_FLOAT", "1.5")
        assert config._get_float("SOME_FLOAT", 0.0) == 1.5

    def test_missing_env_returns_default(self, monkeypatch):
        monkeypatch.delenv("SOME_FLOAT", raising=False)
        assert config._get_float("SOME_FLOAT", 2.5) == 2.5

    def test_garbage_value_falls_back_to_default(self, monkeypatch):
        # Hits the `except (TypeError, ValueError)` branch (lines 31-32):
        # float("not-a-number") raises ValueError.
        monkeypatch.setenv("SOME_FLOAT", "not-a-number")
        assert config._get_float("SOME_FLOAT", 3.25) == 3.25

    def test_empty_string_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("SOME_FLOAT", "")
        assert config._get_float("SOME_FLOAT", 4.0) == 4.0


class TestGetInt:
    def test_valid_value_parses(self, monkeypatch):
        monkeypatch.setenv("SOME_INT", "42")
        assert config._get_int("SOME_INT", 0) == 42

    def test_missing_env_returns_default(self, monkeypatch):
        monkeypatch.delenv("SOME_INT", raising=False)
        assert config._get_int("SOME_INT", 7) == 7

    def test_garbage_value_falls_back_to_default(self, monkeypatch):
        # Hits the `except (TypeError, ValueError)` branch (lines 38-39):
        # int("not-a-number") raises ValueError.
        monkeypatch.setenv("SOME_INT", "not-a-number")
        assert config._get_int("SOME_INT", 9) == 9

    def test_float_string_falls_back_to_default(self, monkeypatch):
        # int("3.5") also raises ValueError (int() doesn't parse decimals).
        monkeypatch.setenv("SOME_INT", "3.5")
        assert config._get_int("SOME_INT", 11) == 11


# ── ADMIN_PASSWORD_HASH_B64 decode at import time (lines 471-475) ──────────

@pytest.fixture()
def _preexisting_admin_hash_env():
    """session112 round 30: `_restore_config_env`'s own teardown (below) has
    a branch nothing was hitting — `os.environ[key] = val` for a `val` that
    ISN'T None (line 93). Every test in this file starts from a clean
    environment (neither ADMIN_PASSWORD_HASH_B64 nor ADMIN_PASSWORD_HASH set
    before the fixture snapshots them), so `saved_env`'s values were always
    None and teardown always took the `pop` branch instead.

    This fixture sets ADMIN_PASSWORD_HASH_B64 to a real value *before*
    `_restore_config_env` runs and snapshots it, so that fixture's teardown
    has a genuine non-None value to restore. Requested ahead of
    `_restore_config_env` in the test's parameter list so its setup runs
    first (pytest instantiates same-scope, non-dependent fixtures in
    left-to-right parameter order) and its own teardown runs last (LIFO) —
    after `_restore_config_env` has already put the pre-existing value back
    and reloaded `config` once. Reloads `config` again here so the module's
    final state matches the truly-original environment this fixture itself
    restores, not the intermediate "pre-existing" value `_restore_config_env`
    leaves it at — keeping this file's isolation promise intact for whatever
    test runs next.

    Deliberately doesn't mirror `_restore_config_env`'s own
    save-whatever-was-there-first/restore-it pattern: every test in this
    suite (this one included) starts with ADMIN_PASSWORD_HASH_B64 genuinely
    unset, so an "if it had a prior value, restore it" branch here would
    just be a second, permanently-unreachable copy of the exact coverage
    gap this fixture exists to close on the fixture below it. A plain
    unconditional pop is both correct for this file's actual starting state
    and free of that problem.
    """
    os.environ["ADMIN_PASSWORD_HASH_B64"] = base64.b64encode(b"pre-existing").decode("ascii")
    yield
    os.environ.pop("ADMIN_PASSWORD_HASH_B64", None)
    importlib.reload(config)


@pytest.fixture()
def _restore_config_env():
    """Snapshot the two relevant env vars, yield, then put both back exactly
    as they were and reload config so later test files see the same config
    module state they always have."""
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


class TestAdminHashB64Decode:
    def test_b64_hash_is_decoded_when_set_and_plain_hash_is_not(self, _restore_config_env):
        raw_hash = "$argon2id$v=19$m=65536,t=3,p=4$saltsalt$hashhash"
        os.environ["ADMIN_PASSWORD_HASH_B64"] = base64.b64encode(raw_hash.encode("utf-8")).decode("ascii")
        os.environ.pop("ADMIN_PASSWORD_HASH", None)

        importlib.reload(config)

        assert config.ADMIN_PASSWORD_HASH == raw_hash

    def test_invalid_b64_hash_falls_back_to_empty_string(self, _restore_config_env):
        """Garbage that base64/utf-8 decoding can't handle must not crash the
        whole service at import — it degrades to an empty hash, not an
        unhandled exception on boot."""
        os.environ["ADMIN_PASSWORD_HASH_B64"] = "not-valid-base64-!!!"
        os.environ.pop("ADMIN_PASSWORD_HASH", None)

        importlib.reload(config)

        assert config.ADMIN_PASSWORD_HASH == ""

    def test_plain_hash_env_var_takes_priority_over_b64(self, _restore_config_env):
        """Per the module comment: either input is accepted, but
        ADMIN_PASSWORD_HASH wins if both are set."""
        plain_hash = "$argon2id$plain$hash"
        os.environ["ADMIN_PASSWORD_HASH"] = plain_hash
        os.environ["ADMIN_PASSWORD_HASH_B64"] = base64.b64encode(b"should-be-ignored").decode("ascii")

        importlib.reload(config)

        assert config.ADMIN_PASSWORD_HASH == plain_hash

    def test_neither_env_var_set_gives_empty_hash(self, _restore_config_env):
        os.environ.pop("ADMIN_PASSWORD_HASH_B64", None)
        os.environ.pop("ADMIN_PASSWORD_HASH", None)

        importlib.reload(config)

        assert config.ADMIN_PASSWORD_HASH == ""

    def test_teardown_restores_a_preexisting_b64_env_value(
        self, _preexisting_admin_hash_env, _restore_config_env
    ):
        """Covers _restore_config_env's own teardown restore branch (line 93:
        `os.environ[key] = val` for a non-None val) — see
        `_preexisting_admin_hash_env`'s docstring above for why every other
        test in this class could never reach it."""
        os.environ["ADMIN_PASSWORD_HASH_B64"] = base64.b64encode(b"temporary-value").decode("ascii")
        os.environ.pop("ADMIN_PASSWORD_HASH", None)

        importlib.reload(config)

        assert config.ADMIN_PASSWORD_HASH == "temporary-value"
