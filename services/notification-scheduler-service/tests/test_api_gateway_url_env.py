"""
tests/test_api_gateway_url_env.py — API_GATEWAY_URL normalisation in scheduler/*.py.

fundamentals_batch.py, weekend_hydrator.py and overnight_orchestrator.py read it with a default;
run_once.py requires it (os.environ[...], fail fast). An EMPTY or whitespace-only value bypassed the
default (os.getenv only defaults when unset) or passed the "required" check, giving URLs like
"/scan/batch". Now blank / whitespace-only / slash-only -> default (or the same fail-fast KeyError as
unset for run_once); padded real URLs are trimmed. Each module reads the env at import, so every case
loads it fresh from its file under a unique name.

Run from services/notification-scheduler-service:  python -m pytest tests -v
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import sys

import pytest

_SCHED = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler")
_counter = itertools.count()
_CASES = [
    (None, None),                      # unset            -> default
    ("", None),                        # empty (env_file `API_GATEWAY_URL=`) -> default
    ("   ", None),                     # whitespace only  -> default
    ("\t\n", None),
    ("/", None),                       # slash only       -> default
    ("  /  ", None),
    ("http://gw:1", "http://gw:1"),
    ("http://gw:1/", "http://gw:1"),
    ("http://gw:1///", "http://gw:1"),
    ("  http://gw:1  ", "http://gw:1"),
    (" http://gw:1/ \n", "http://gw:1"),
]


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    before = set(sys.modules)
    yield
    for k in set(sys.modules) - before:
        if k.startswith("_gwurl_"):
            sys.modules.pop(k, None)


def _load(filename, raw, monkeypatch):
    if raw is None:
        monkeypatch.delenv("API_GATEWAY_URL", raising=False)
    else:
        monkeypatch.setenv("API_GATEWAY_URL", raw)
    name = f"_gwurl_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, os.path.join(_SCHED, filename))
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("filename,default", [
    ("fundamentals_batch.py", "https://api-gateway.onrender.com"),
    ("weekend_hydrator.py", "http://localhost:8000"),
    ("overnight_orchestrator.py", "http://api-gateway:8000"),
])
@pytest.mark.parametrize("raw,expected", _CASES)
def test_default_modules_normalise(filename, default, raw, expected, monkeypatch):
    mod = _load(filename, raw, monkeypatch)
    assert mod.API_GATEWAY_URL == (expected or default)


@pytest.mark.parametrize("raw,expected", [c for c in _CASES if c[1]])
def test_run_once_trims_real_values(raw, expected, monkeypatch):
    assert _load("run_once.py", raw, monkeypatch).API_GATEWAY_URL == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "\t\n", "/", "  /  "])
def test_run_once_blank_or_unset_fails_fast(raw, monkeypatch):
    with pytest.raises(KeyError):
        _load("run_once.py", raw, monkeypatch)


def test_overnight_orchestrator_helpers_use_normalised_base(monkeypatch):
    # _post/_get default their base= to the module constant at definition time; a padded env value
    # must therefore already be clean when the module is imported.
    mod = _load("overnight_orchestrator.py", "  http://gw:1/ ", monkeypatch)
    assert mod._post.__defaults__ is not None and "http://gw:1" in mod._post.__defaults__
    assert mod._get.__defaults__ is not None and "http://gw:1" in mod._get.__defaults__
