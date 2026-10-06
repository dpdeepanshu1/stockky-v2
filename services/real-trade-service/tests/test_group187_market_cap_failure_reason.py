"""
group187 (item 5 of the 2026-10-06 boot-log list): the standard track's market-cap fetch.

  * the INFO line names why it failed (ReadTimeout, HTTP 500, no market_cap) - httpx timeouts stringify to ''
  * the last good value (CANDIDATE_MCAP_STALE_TTL_S, default 24 h, 0 = off) is used when a later fetch fails, so a
    timeout no longer drops the market-cap floor for that candidate
  * CANDIDATE_MCAP_TIMEOUT_S sets the request timeout (default 12 s)
No network: fake client.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import pytest

import config  # noqa: F401
from candidate_engine import candidates as cd


def run(c):
    return asyncio.run(c)


class _Resp:
    def __init__(self, code=200, payload=None):
        self.status_code, self._p = code, payload

    def json(self):
        return self._p


class _Client:
    def __init__(self, outcome):
        self.outcome, self.timeouts = outcome, []

    async def get(self, url, timeout=None, params=None):
        self.timeouts.append(timeout)
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


class _Timeout(Exception):
    """str() is empty, like httpx.ReadTimeout."""
    def __str__(self):
        return ""


@pytest.fixture(autouse=True)
def _clean():
    cd._MCAP_LAST_GOOD.clear()
    yield
    cd._MCAP_LAST_GOOD.clear()


class TestReason:
    def test_timeout_names_the_exception_class(self, caplog):
        with caplog.at_level(logging.INFO):
            assert run(cd._fetch_market_cap_cr(_Client(_Timeout()), "HFCL")) is None
        assert "market_cap fetch failed for HFCL (_Timeout)" in caplog.text
        assert "()" not in caplog.text

    def test_http_error_status_is_named(self, caplog):
        with caplog.at_level(logging.INFO):
            run(cd._fetch_market_cap_cr(_Client(_Resp(500)), "HFCL"))
        assert "(HTTP 500)" in caplog.text

    def test_answer_without_market_cap_is_named(self, caplog):
        with caplog.at_level(logging.INFO):
            run(cd._fetch_market_cap_cr(_Client(_Resp(200, {"raw": {}})), "HFCL"))
        assert "no market_cap in the answer" in caplog.text

    def test_exception_with_a_message_keeps_it(self, caplog):
        with caplog.at_level(logging.INFO):
            run(cd._fetch_market_cap_cr(_Client(ValueError("bad json")), "HFCL"))
        assert "ValueError: bad json" in caplog.text


class TestLastGood:
    def test_good_value_converted_and_remembered(self):
        assert run(cd._fetch_market_cap_cr(_Client(_Resp(200, {"market_cap": 2e9})), "TCS")) == 200.0
        assert cd._mcap_stale("TCS") == 200.0

    def test_failure_after_success_uses_the_remembered_value(self, caplog):
        run(cd._fetch_market_cap_cr(_Client(_Resp(200, {"market_cap": 2e9})), "TCS"))
        with caplog.at_level(logging.INFO):
            got = run(cd._fetch_market_cap_cr(_Client(_Timeout()), "TCS"))
        assert got == 200.0 and "using last known" in caplog.text

    def test_remembered_value_expires(self, monkeypatch):
        run(cd._fetch_market_cap_cr(_Client(_Resp(200, {"market_cap": 2e9})), "TCS"))
        cd._MCAP_LAST_GOOD["TCS"] = (time.time() - cd._MCAP_STALE_TTL_S - 5, 200.0)
        assert run(cd._fetch_market_cap_cr(_Client(_Timeout()), "TCS")) is None

    def test_other_symbols_do_not_borrow_it(self):
        run(cd._fetch_market_cap_cr(_Client(_Resp(200, {"market_cap": 2e9})), "TCS"))
        assert run(cd._fetch_market_cap_cr(_Client(_Timeout()), "HFCL")) is None

    def test_ttl_zero_turns_it_off(self, monkeypatch):
        monkeypatch.setattr(cd, "_MCAP_STALE_TTL_S", 0.0)
        run(cd._fetch_market_cap_cr(_Client(_Resp(200, {"market_cap": 2e9})), "TCS"))
        assert run(cd._fetch_market_cap_cr(_Client(_Timeout()), "TCS")) is None

    def test_never_seen_symbol_still_returns_none(self):
        assert run(cd._fetch_market_cap_cr(_Client(_Timeout()), "NEW")) is None


class TestTimeout:
    def test_request_uses_the_configured_timeout(self, monkeypatch):
        monkeypatch.setattr(cd, "_MCAP_TIMEOUT_S", 25.0)
        c = _Client(_Resp(200, {"market_cap": 1e9}))
        run(cd._fetch_market_cap_cr(c, "TCS"))
        assert c.timeouts == [25.0]
