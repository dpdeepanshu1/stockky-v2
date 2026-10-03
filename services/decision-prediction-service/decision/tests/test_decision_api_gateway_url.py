"""
tests/test_decision_api_gateway_url.py — decision/main.py::API_GATEWAY_URL.

It used `os.getenv("API_GATEWAY_URL", default)` with no rstrip at all, so a trailing slash produced
"//market/indices" and an empty / whitespace-only value produced "/market/indices". Blank,
whitespace-only and slash-only values now fall back to the default; padded real URLs are trimmed.
main.py reads the env at import, so each case loads it fresh by path (its sibling modules are made
importable for the duration and everything is undone afterwards).

Run from services/decision-prediction-service/decision:  python3 -m pytest tests -q
"""
from __future__ import annotations

import importlib.util
import itertools
import os
import sys

import pytest

_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_counter = itertools.count()
_DEFAULT = "https://api-gateway-puwd.onrender.com"
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
def _isolate():
    path_before, mods_before = list(sys.path), set(sys.modules)
    sys.path.insert(0, _DIR)
    yield
    sys.path[:] = path_before
    for k in set(sys.modules) - mods_before:
        sys.modules.pop(k, None)


def _load(raw, monkeypatch):
    if raw is None:
        monkeypatch.delenv("API_GATEWAY_URL", raising=False)
    else:
        monkeypatch.setenv("API_GATEWAY_URL", raw)
    spec = importlib.util.spec_from_file_location(f"_decision_main_{next(_counter)}", os.path.join(_DIR, "main.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("raw,expected", _CASES)
def test_api_gateway_url_normalised(raw, expected, monkeypatch):
    assert _load(raw, monkeypatch).API_GATEWAY_URL == (expected or _DEFAULT)
