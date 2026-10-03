"""scheduler/rate_limiter.py: a bad YFINANCE_HARD_TIMEOUT_SEC / YFINANCE_POOL_WORKERS must not stop the service.

Both used to be parsed at import with float()/int() and no guard, so one typo in .env ("18s", "many") or an
unusable value (0 workers, a zero/negative/NaN/inf timeout) raised out of module import. They now log a WARNING
and fall back to the default (18s / 8 workers). The module is loaded fresh from its file for every case (it reads
the env at import) so nothing leaks between tests. The analysis-intelligence copy of this block is covered in
that service's tests/test_rate_limiter.py; this is the notification-scheduler-service copy.

Run from services/notification-scheduler-service:
    python -m pytest tests -v
"""
from __future__ import annotations

import importlib.util
import itertools
import logging
import os
import sys

import pytest

_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scheduler", "rate_limiter.py")
_counter = itertools.count()
_ENV = ("YFINANCE_HARD_TIMEOUT_SEC", "YFINANCE_POOL_WORKERS")


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for k in _ENV:
        monkeypatch.delenv(k, raising=False)


def _load():
    name = f"nss_rate_limiter_{next(_counter)}"
    spec = importlib.util.spec_from_file_location(name, _PATH)
    mod = importlib.util.module_from_spec(spec)
    # @dataclass (with `from __future__ import annotations`) looks the module up in sys.modules while
    # the class body runs, so it must be registered before exec_module.
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(name, None)
    return mod


def _warned(caplog, name):
    return any(f"ignoring invalid {name}" in r.getMessage() for r in caplog.records)


def test_defaults():
    rl = _load()
    assert rl.YFINANCE_HARD_TIMEOUT_SEC == 18.0
    assert rl._yf_hardcap_pool._max_workers == 8
    assert rl._yf_hardcap_pool._thread_name_prefix == "yf-hardcap"


def test_valid_env_overrides_with_whitespace_and_fractions(monkeypatch, caplog):
    monkeypatch.setenv("YFINANCE_HARD_TIMEOUT_SEC", " 7.5 ")
    monkeypatch.setenv("YFINANCE_POOL_WORKERS", " 3 ")
    with caplog.at_level(logging.WARNING, logger="rate-limiter"):
        rl = _load()
    assert rl.YFINANCE_HARD_TIMEOUT_SEC == 7.5 and rl._yf_hardcap_pool._max_workers == 3
    assert not _warned(caplog, "YFINANCE_HARD_TIMEOUT_SEC") and not _warned(caplog, "YFINANCE_POOL_WORKERS")


@pytest.mark.parametrize("bad", ["18s", "abc", "0", "-5", "nan", "inf", "-inf", "1e999"])
def test_bad_timeout_falls_back_and_warns(monkeypatch, caplog, bad):
    monkeypatch.setenv("YFINANCE_HARD_TIMEOUT_SEC", bad)
    with caplog.at_level(logging.WARNING, logger="rate-limiter"):
        rl = _load()
    assert rl.YFINANCE_HARD_TIMEOUT_SEC == 18.0
    assert _warned(caplog, "YFINANCE_HARD_TIMEOUT_SEC")


@pytest.mark.parametrize("bad", ["many", "0", "-2", "3.5", "1e2"])
def test_bad_worker_count_falls_back_and_warns(monkeypatch, caplog, bad):
    monkeypatch.setenv("YFINANCE_POOL_WORKERS", bad)
    with caplog.at_level(logging.WARNING, logger="rate-limiter"):
        rl = _load()
    assert rl._yf_hardcap_pool._max_workers == 8
    assert _warned(caplog, "YFINANCE_POOL_WORKERS")


def test_one_bad_value_does_not_discard_the_other(monkeypatch):
    monkeypatch.setenv("YFINANCE_HARD_TIMEOUT_SEC", "18s")
    monkeypatch.setenv("YFINANCE_POOL_WORKERS", "3")
    rl = _load()
    assert rl.YFINANCE_HARD_TIMEOUT_SEC == 18.0 and rl._yf_hardcap_pool._max_workers == 3
    monkeypatch.setenv("YFINANCE_HARD_TIMEOUT_SEC", "5")
    monkeypatch.setenv("YFINANCE_POOL_WORKERS", "many")
    rl = _load()
    assert rl.YFINANCE_HARD_TIMEOUT_SEC == 5.0 and rl._yf_hardcap_pool._max_workers == 8


@pytest.mark.parametrize("blank", ["", "   "])
def test_blank_env_means_default_without_a_warning(monkeypatch, caplog, blank):
    monkeypatch.setenv("YFINANCE_HARD_TIMEOUT_SEC", blank)
    monkeypatch.setenv("YFINANCE_POOL_WORKERS", blank)
    with caplog.at_level(logging.WARNING, logger="rate-limiter"):
        rl = _load()
    assert rl.YFINANCE_HARD_TIMEOUT_SEC == 18.0 and rl._yf_hardcap_pool._max_workers == 8
    assert not [r for r in caplog.records if "ignoring invalid" in r.getMessage()]
