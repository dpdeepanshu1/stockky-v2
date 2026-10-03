"""group 73: Oracle-first database URL resolution in the training service.

docker-compose passes `DATABASE_URL: ${DATABASE_URL:-}` (an EMPTY string) on the Oracle VM, and ORACLE_DSN decides
the backend. Before this group:
  * trades.py / evaluate.py / app.py used `os.environ.get(NAME, default)`, which returns "" for a blank variable;
  * app.py reported `db_backend="sqlite"` / `db_durable=False` ("Using local SQLite (ephemeral)") for a process
    that was really writing to Oracle, so the Trades/Training pages showed a false durability warning.

Run: cd services/decision-prediction-service/training && python3 -m pytest tests/test_oracle_first_db_url.py -q
"""
import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import models  # noqa: E402

SQLITE = "sqlite:///./training.db"
_VARS = ("TRAINING_DATABASE_URL", "DATABASE_URL", "ORACLE_DSN")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for v in _VARS:
        monkeypatch.delenv(v, raising=False)


@pytest.mark.parametrize("blank", ["", " ", "   ", "\t\n"])
def test_blank_variables_count_as_unset(monkeypatch, blank):
    monkeypatch.setenv("DATABASE_URL", blank)
    monkeypatch.setenv("TRAINING_DATABASE_URL", blank)
    assert models.resolve_database_url() == SQLITE
    assert models.resolve_database_url("DATABASE_URL") == SQLITE


def test_training_url_wins_then_database_url_and_values_are_trimmed(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "  postgresql://db/x \n")
    assert models.resolve_database_url() == "postgresql://db/x"
    monkeypatch.setenv("TRAINING_DATABASE_URL", " postgresql://train/y ")
    assert models.resolve_database_url() == "postgresql://train/y"
    assert models.resolve_database_url("DATABASE_URL") == "postgresql://db/x"      # explicit order respected


def test_blank_training_url_falls_through_to_database_url(monkeypatch):
    monkeypatch.setenv("TRAINING_DATABASE_URL", "")
    monkeypatch.setenv("DATABASE_URL", "postgresql://db/x")
    assert models.resolve_database_url() == "postgresql://db/x"


def test_backend_name_sqlite_postgres_oracle(monkeypatch):
    assert models.db_backend_name(SQLITE) == "sqlite"
    assert models.db_backend_name("") == "sqlite"
    assert models.db_backend_name("postgres://u@h/db") == "postgres"
    assert models.db_backend_name("postgresql://u@h/db") == "postgres"
    assert models.db_backend_name("oracle+oracledb://u:p@h/svc") == "oracle"
    monkeypatch.setenv("ORACLE_DSN", "stockkydb_high")
    assert models.db_backend_name(SQLITE) == "oracle"          # the Oracle VM: no DATABASE_URL, ORACLE_DSN set
    assert models.db_backend_name("postgresql://u@h/db") == "oracle"   # same rule get_engine() applies


@pytest.mark.parametrize("dsn", ["", "   "])
def test_blank_oracle_dsn_does_not_switch_to_oracle(monkeypatch, dsn):
    monkeypatch.setenv("ORACLE_DSN", dsn)
    assert models.db_backend_name(SQLITE) == "sqlite"
    assert models.db_backend_name("postgresql://u@h/db") == "postgres"


def test_backend_name_agrees_with_get_engine_choice(monkeypatch):
    for url in (SQLITE, "postgresql://u@h/db", "oracle+oracledb://u:p@h/s"):
        for dsn in ("", "  ", "x"):
            monkeypatch.setenv("ORACLE_DSN", dsn)
            assert (models.db_backend_name(url) == "oracle") == models._oracle_is_configured(url)


def _probe(code, **env):
    full = {k: v for k, v in os.environ.items() if k not in _VARS}
    full.update(env)
    out = subprocess.run([sys.executable, "-c", code], cwd=HERE, env=full, capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-1500:]
    return out.stdout.strip().splitlines()[-1]


@pytest.mark.parametrize("mod", ["evaluate", "trades"])
def test_module_database_url_is_blank_safe(mod):
    got = _probe(f"import {mod} as m; print(m.DATABASE_URL)", DATABASE_URL="  ", TRAINING_DATABASE_URL="")
    assert got == SQLITE


def test_app_reports_oracle_not_sqlite_when_oracle_dsn_is_set():
    code = "import app; print(app._db_backend, app._db_connection_info()['db_durable'], app._db_connection_info()['db_provider'])"
    # the Oracle engine itself is not reachable here, so only the reported backend is asserted
    got = _probe(code, DATABASE_URL="", ORACLE_DSN="stockkydb_high")
    assert got.split()[:3] == ["oracle", "True", "oracle"], got


def test_app_still_reports_sqlite_and_postgres_without_oracle():
    assert _probe("import app; print(app._db_backend, app._db_connection_info()['db_durable'])",
                  DATABASE_URL="").split() == ["sqlite", "False"]
    got = _probe("import app; print(app._db_backend, app._db_connection_info()['db_durable'])",
                 DATABASE_URL="postgresql://u:p@127.0.0.1:1/x")
    assert got.split() == ["postgres", "True"]
