"""round 662 (D9): real-trade db._pool_sizes reads RT_DB_POOL_SIZE / RT_DB_MAX_OVERFLOW (default 4 / 4) and ignores the shared DB_POOL_SIZE."""
from __future__ import annotations

import ast
import os

import pytest

_SRC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db.py")
_TREE = ast.parse(open(_SRC_PATH, encoding="utf-8").read())


def _fn():
    node = next(n for n in _TREE.body if isinstance(n, ast.FunctionDef) and n.name == "_pool_sizes")
    ns: dict = {}
    exec(compile(ast.Module([node], []), _SRC_PATH, "exec"), ns)
    return ns["_pool_sizes"]


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("RT_DB_POOL_SIZE", "RT_DB_MAX_OVERFLOW", "DB_POOL_SIZE", "DB_MAX_OVERFLOW"):
        monkeypatch.delenv(k, raising=False)


def test_default_is_4_4():
    assert _fn()() == (4, 4)


def test_env_values_used(monkeypatch):
    monkeypatch.setenv("RT_DB_POOL_SIZE", "6"); monkeypatch.setenv("RT_DB_MAX_OVERFLOW", "2")
    assert _fn()() == (6, 2)


def test_shared_db_pool_size_is_ignored(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "2"); monkeypatch.setenv("DB_MAX_OVERFLOW", "0")
    assert _fn()() == (4, 4)


@pytest.mark.parametrize("raw,expect", [("", 4), ("  ", 4), ("abc", 4), ("1", 2), ("0", 2), ("-3", 2)])
def test_blank_bad_and_small_values(monkeypatch, raw, expect):
    monkeypatch.setenv("RT_DB_POOL_SIZE", raw)
    assert _fn()()[0] == expect


def test_overflow_can_be_zero_but_not_negative(monkeypatch):
    monkeypatch.setenv("RT_DB_MAX_OVERFLOW", "0")
    assert _fn()()[1] == 0
    monkeypatch.setenv("RT_DB_MAX_OVERFLOW", "-5")
    assert _fn()()[1] == 0


def test_get_engine_uses_the_helper_on_both_paths():
    src = open(_SRC_PATH, encoding="utf-8").read()
    assert "db_pool_size=_psize" in src and "pool_size=_psize" in src and "db_pool_size=4" not in src
