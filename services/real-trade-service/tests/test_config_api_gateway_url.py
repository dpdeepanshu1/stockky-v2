"""
tests/test_config_api_gateway_url.py — config.API_GATEWAY_URL normalisation (real-trade-service).

`os.getenv(name, default)` only falls back to the default when the variable is UNSET. An empty
value (env_file line `API_GATEWAY_URL=`, an unset CI secret) or a whitespace-only one came through
as-is, so every upstream URL became "/stockky-hot", "   /market/indices", ... Blank, whitespace-only
and slash-only values now fall back to the default; padded real URLs are trimmed.

config.py reads the env once at import, so each case reloads it and restores everything afterwards.
Run from services/real-trade-service:  python3 -m pytest tests/test_config_api_gateway_url.py -q
"""
from __future__ import annotations

import contextlib
import importlib
import os

import pytest

import config

_DEFAULT = "https://stockky-api-gateway.onrender.com"
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


@contextlib.contextmanager
def _reloaded(raw):
    saved = os.environ.get("API_GATEWAY_URL")
    try:
        if raw is None:
            os.environ.pop("API_GATEWAY_URL", None)
        else:
            os.environ["API_GATEWAY_URL"] = raw
        yield importlib.reload(config)
    finally:
        if saved is None:
            os.environ.pop("API_GATEWAY_URL", None)
        else:
            os.environ["API_GATEWAY_URL"] = saved
        importlib.reload(config)


@pytest.mark.parametrize("raw,expected", _CASES)
def test_api_gateway_url_normalised(raw, expected):
    with _reloaded(raw) as cfg:
        assert cfg.API_GATEWAY_URL == (expected or _DEFAULT)


def test_config_restored_after_reload_cases():
    # The helper above must leave config exactly as it found it.
    before = config.API_GATEWAY_URL
    with _reloaded("  http://other:9/ "):
        pass
    assert config.API_GATEWAY_URL == before
