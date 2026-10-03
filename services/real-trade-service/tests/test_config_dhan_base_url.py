"""tests/test_config_dhan_base_url.py — group 65.

DHAN_SANDBOX_URL / DHAN_LIVE_URL used os.getenv(name, default), which only falls back when the
variable is UNSET: a blank value (`DHAN_LIVE_URL=` in an env_file) gave "" and every order call
went to "/orders". Blank / whitespace / slash-only values now use the official base, padded values
are trimmed, and anything that is not https:// is refused (the access token travels on this URL).
A blank DHAN_ENV used to raise KeyError at import; it now means "sandbox". A non-blank value that is
neither "sandbox" nor "live" still fails fast, with a clear message.

config.py imports only `os`, so each case loads a fresh copy by path under a unique name.

Run from services/real-trade-service:
    python3 -m pytest tests/test_config_dhan_base_url.py -v
"""
from __future__ import annotations

import importlib.util
import itertools
import os

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_CONFIG = os.path.join(os.path.dirname(_HERE), "config.py")
_OFFICIAL = "https://api.dhan.co/v2"
_counter = itertools.count()
_KEYS = ("DHAN_ENV", "DHAN_SANDBOX_URL", "DHAN_LIVE_URL")


def _load(monkeypatch, **env):
    for k in _KEYS:
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    spec = importlib.util.spec_from_file_location(f"_rt_config_dhan_{next(_counter)}", _CONFIG)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("raw", ["", "   ", "\t", " \n ", "/", "  /  "])
@pytest.mark.parametrize("env,var", [("live", "DHAN_LIVE_URL"), ("sandbox", "DHAN_SANDBOX_URL")])
def test_blank_value_uses_the_official_base(monkeypatch, raw, env, var):
    assert _load(monkeypatch, DHAN_ENV=env, **{var: raw}).DHAN_BASE_URL == _OFFICIAL


@pytest.mark.parametrize("env", ["live", "sandbox"])
def test_unset_value_uses_the_official_base(monkeypatch, env):
    assert _load(monkeypatch, DHAN_ENV=env).DHAN_BASE_URL == _OFFICIAL


@pytest.mark.parametrize("raw,expected", [
    ("https://sandbox.example.com/v2", "https://sandbox.example.com/v2"),
    ("  https://sandbox.example.com/v2/  ", "https://sandbox.example.com/v2"),
    ("HTTPS://api.dhan.co/v2", "HTTPS://api.dhan.co/v2"),
])
def test_real_https_values_are_trimmed_and_kept(monkeypatch, raw, expected):
    assert _load(monkeypatch, DHAN_ENV="live", DHAN_LIVE_URL=raw).DHAN_BASE_URL == expected


@pytest.mark.parametrize("raw", [
    "http://api.dhan.co/v2", "api.dhan.co/v2", "ftp://api.dhan.co", "https://", "https:///", "garbage",
])
def test_non_https_values_fall_back_to_the_official_base(monkeypatch, raw):
    assert _load(monkeypatch, DHAN_ENV="live", DHAN_LIVE_URL=raw).DHAN_BASE_URL == _OFFICIAL


def test_only_the_selected_environments_variable_is_used(monkeypatch):
    live = _load(monkeypatch, DHAN_ENV="live", DHAN_LIVE_URL="https://live.example.com",
                 DHAN_SANDBOX_URL="https://sbx.example.com")
    sbx = _load(monkeypatch, DHAN_ENV="sandbox", DHAN_LIVE_URL="https://live.example.com",
                DHAN_SANDBOX_URL="https://sbx.example.com")
    assert live.DHAN_BASE_URL == "https://live.example.com"
    assert sbx.DHAN_BASE_URL == "https://sbx.example.com"


@pytest.mark.parametrize("raw,expected", [
    (None, "sandbox"), ("", "sandbox"), ("   ", "sandbox"),
    ("live", "live"), (" LIVE ", "live"), ("Sandbox", "sandbox"),
])
def test_dhan_env_normalised(monkeypatch, raw, expected):
    env = {} if raw is None else {"DHAN_ENV": raw}
    assert _load(monkeypatch, **env).DHAN_ENV == expected


@pytest.mark.parametrize("raw", ["prod", "paper", "livee", "1"])
def test_unknown_dhan_env_fails_fast(monkeypatch, raw):
    with pytest.raises(ValueError, match="DHAN_ENV"):
        _load(monkeypatch, DHAN_ENV=raw)
