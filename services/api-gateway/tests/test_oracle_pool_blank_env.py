"""group103: blank pool-size env values no longer abort Oracle engine creation.

`oracle_engine_kwargs` did int(os.getenv(NAME, default)); a variable that is SET but EMPTY (compose `${VAR:-}`,
a bare `VAR=` line in .env) returns "" not the default, so int("") raised ValueError and the durable cache layer
silently fell back to memory only. Each value now resolves override -> env var -> default, skipping blank entries; a non-blank
non-numeric value still raises. Also guards that all copies of oracle_compat.py stay byte-identical.
"""
import glob
import hashlib
import os

import pytest

import oracle_compat as oc

_SERVICES = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("DB_POOL_SIZE", "DB_MAX_OVERFLOW", "DB_POOL_RECYCLE", "DB_POOL_TIMEOUT"):
        monkeypatch.delenv(k, raising=False)


def test_defaults_unchanged_when_nothing_is_set():
    kw = oc.oracle_engine_kwargs(True)
    assert (kw["pool_size"], kw["max_overflow"], kw["pool_recycle"], kw["pool_timeout"]) == (3, 2, 300, 30)


def test_numeric_override_wins():
    kw = oc.oracle_engine_kwargs(True, db_pool_size="7", db_max_overflow=4)
    assert kw["pool_size"] == 7 and kw["max_overflow"] == 4


@pytest.mark.parametrize("blank", ["", "   ", None])
def test_blank_override_falls_back_to_default(blank):
    kw = oc.oracle_engine_kwargs(True, db_pool_size=blank, db_pool_timeout=blank)
    assert kw["pool_size"] == 3 and kw["pool_timeout"] == 30


def test_blank_override_falls_back_to_env_var_before_default(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "6")
    assert oc.oracle_engine_kwargs(True, db_pool_size="")["pool_size"] == 6


def test_blank_env_var_uses_default(monkeypatch):
    monkeypatch.setenv("DB_POOL_SIZE", "")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "  ")
    kw = oc.oracle_engine_kwargs(True)
    assert kw["pool_size"] == 3 and kw["max_overflow"] == 2


def test_non_blank_non_numeric_value_still_raises(monkeypatch):
    # a real typo stays loud (pinned in test_oracle_compat.py); only blank means "unset"
    with pytest.raises(ValueError):
        oc.oracle_engine_kwargs(True, db_pool_size="many")
    monkeypatch.setenv("DB_POOL_RECYCLE", "abc")
    with pytest.raises(ValueError):
        oc.oracle_engine_kwargs(True)


def test_kv_cache_oracle_branch_reads_blank_safe():
    src = open(os.path.join(os.path.dirname(__file__), "..", "kv_cache.py"), encoding="utf-8").read()
    assert 'os.getenv("CACHE_DB_POOL_SIZE_ORACLE", os.getenv(' not in src
    assert '(os.getenv("CACHE_DB_POOL_SIZE_ORACLE") or "").strip() or (os.getenv("CACHE_DB_POOL_SIZE") or "").strip() or "5"' in src


def test_all_oracle_compat_copies_are_identical():
    paths = glob.glob(os.path.join(_SERVICES, "**", "oracle_compat.py"), recursive=True)
    assert len(paths) >= 8
    digests = {hashlib.sha256(open(p, "rb").read()).hexdigest() for p in paths}
    assert len(digests) == 1, "oracle_compat.py copies have drifted apart"
