"""
tests/test_config_api_gateway_url.py — config._API_GATEWAY_URL / MARKET_INDICES_URL (position-stocks-service).

Same bug class as real-trade-service: an EMPTY or whitespace-only API_GATEWAY_URL bypassed the
os.getenv default and produced a MARKET_INDICES_URL of "/market/indices". Blank, whitespace-only and
slash-only values now fall back to the default; padded real URLs are trimmed. An explicit
MARKET_INDICES_URL still wins (unchanged).
config.py reads the env once at import, so each case reloads it and restores everything afterwards.
"""
from __future__ import annotations

import contextlib
import importlib
import os

import pytest

import config

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


@contextlib.contextmanager
def _reloaded(raw, indices=None):
    names = ("API_GATEWAY_URL", "MARKET_INDICES_URL")
    saved = {n: os.environ.get(n) for n in names}
    try:
        for n, v in (("API_GATEWAY_URL", raw), ("MARKET_INDICES_URL", indices)):
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        yield importlib.reload(config)
    finally:
        for n, v in saved.items():
            if v is None:
                os.environ.pop(n, None)
            else:
                os.environ[n] = v
        importlib.reload(config)


@pytest.mark.parametrize("raw,expected", _CASES)
def test_gateway_base_and_indices_url_normalised(raw, expected):
    base = expected or _DEFAULT
    with _reloaded(raw) as cfg:
        assert cfg._API_GATEWAY_URL == base
        assert cfg.MARKET_INDICES_URL == f"{base}/market/indices"


def test_explicit_market_indices_url_still_wins():
    with _reloaded("   ", indices="http://custom/idx") as cfg:
        assert cfg.MARKET_INDICES_URL == "http://custom/idx"


def test_config_restored_after_reload_cases():
    before = (config._API_GATEWAY_URL, config.MARKET_INDICES_URL)
    with _reloaded("  http://other:9/ "):
        pass
    assert (config._API_GATEWAY_URL, config.MARKET_INDICES_URL) == before
