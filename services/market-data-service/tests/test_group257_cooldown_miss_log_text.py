"""group257: the "AngelOne-first did not price X (angelone_quote cooldown)" log line must not say "using the Yahoo path"
while group256 keeps those symbols off Yahoo. Run from services/market-data-service:
    python3 -m pytest tests/test_group257_cooldown_miss_log_text.py -v
"""
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import main as md


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    md._AO_MISS_LOG.clear()
    for k in ("QUOTE_COOLDOWN_SERVE_STALE", "QUOTE_COOLDOWN_SKIP_YAHOO", "QUOTE_COOLDOWN_STALE_MAX_AGE_S"):
        monkeypatch.delenv(k, raising=False)
    yield
    md._AO_MISS_LOG.clear()


def _line(caplog, reason, sym="ABC"):
    caplog.clear()
    with caplog.at_level(logging.INFO):
        assert md._ao_first_miss(sym, reason) is None
    msgs = [r.getMessage() for r in caplog.records if "did not price" in r.getMessage()]
    assert len(msgs) == 1
    return msgs[0]


def test_cooldown_default_text(caplog):
    m = _line(caplog, "angelone_quote cooldown")
    assert "serving a cached price <= 180s old, else no price (Yahoo skipped)" in m
    assert "using the Yahoo path" not in m


def test_cooldown_skip_yahoo_off(caplog, monkeypatch):
    monkeypatch.setenv("QUOTE_COOLDOWN_SKIP_YAHOO", "0")
    m = _line(caplog, "angelone_quote cooldown")
    assert m.endswith("else the Yahoo path")


def test_serve_stale_off_keeps_old_text(caplog, monkeypatch):
    monkeypatch.setenv("QUOTE_COOLDOWN_SERVE_STALE", "0")
    assert _line(caplog, "angelone_quote cooldown").endswith("using the Yahoo path")


def test_custom_age_and_other_reasons(caplog, monkeypatch):
    monkeypatch.setenv("QUOTE_COOLDOWN_STALE_MAX_AGE_S", "90")
    assert "<= 90s old" in _line(caplog, "angelone_quote cooldown")
    assert _line(caplog, "no scrip-master token", "XYZ").endswith("using the Yahoo path")


def test_blank_env_is_safe(caplog, monkeypatch):
    monkeypatch.setenv("QUOTE_COOLDOWN_STALE_MAX_AGE_S", "")
    assert "<= 180s old" in _line(caplog, "angelone_quote cooldown")
