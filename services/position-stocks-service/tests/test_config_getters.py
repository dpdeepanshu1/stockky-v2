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
"""
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
