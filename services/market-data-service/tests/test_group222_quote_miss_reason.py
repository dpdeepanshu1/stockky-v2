"""tests/test_group222_quote_miss_reason.py — GET /quote logs WHY AngelOne-first did not price a symbol (group 222).

The 2026-10-07 boot log printed the same text for every miss ("rate bucket busy, rate-limit cooldown or no quote for the
token"), so a burst could not be diagnosed. AngelOneSession.get_quote now records the specific cause on the calling thread
(`last_quote_miss_reason()`), and the AngelOne-first helper in main.py logs it. Behaviour is unchanged: still {} / None.

Run from services/market-data-service:
    python -m pytest tests/test_group222_quote_miss_reason.py -v
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import time
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import pytest

import angelone_budget as b
import angelone_client as ac
import angelone_scrip_master
import main
import rate_limiter as rl


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    b._reset()
    rl._buckets.clear()
    ac._note_quote_miss(None)
    yield
    b._reset()
    rl._buckets.clear()
    ac._note_quote_miss(None)


class _Resp:
    def __init__(self, status_code=200, body=None):
        self.status_code, self._body, self.text = status_code, body or {}, str(body)

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


class _Client:
    def __init__(self, resp):
        self.resp = resp

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        pass

    async def post(self, url, **kw):
        return self.resp


def _session(monkeypatch, resp=None, cooldown=False, try_acquire=True):
    s = ac.AngelOneSession()
    s.client_id, s.mpin, s.api_key, s.totp_secret = "C1", "1234", "KEY", "JBSWY3DPEHPK3PXP"
    s.token = "tok"
    s.token_expiry = datetime.utcnow() + timedelta(hours=1)
    monkeypatch.setattr(ac, "_resolve_client_public_ip", lambda: "1.2.3.4")
    monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: cooldown)
    monkeypatch.setattr(ac, "_rl_acquire", lambda *a, **k: 0.0)
    monkeypatch.setattr(ac, "_rl_try_acquire", lambda *a, **k: try_acquire)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _Client(resp or _Resp(200, {"data": {"fetched": []}})))
    return s


# ── AngelOneSession.get_quote records the cause ──────────────────────────────
class TestRecordedReason:
    def test_no_reason_before_any_call(self):
        assert ac.last_quote_miss_reason() is None

    def test_a_good_answer_leaves_no_reason(self, monkeypatch):
        s = _session(monkeypatch, _Resp(200, {"data": {"fetched": [{"ltp": 5}]}}))
        assert run(s.get_quote("NSE", "1")) == {"ltp": 5}
        assert ac.last_quote_miss_reason() is None

    def test_rate_limit_cooldown(self, monkeypatch):
        s = _session(monkeypatch, cooldown=True)
        assert run(s.get_quote("NSE", "1")) == {}
        assert "cooldown" in ac.last_quote_miss_reason()

    def test_global_403_cooldown(self, monkeypatch):
        s = _session(monkeypatch)
        b.trip("elsewhere")
        assert run(s.get_quote("NSE", "1", lane=b.POSITION)) == {}
        assert "global AngelOne cooldown" in ac.last_quote_miss_reason()

    def test_bucket_had_no_token(self, monkeypatch):
        s = _session(monkeypatch, try_acquire=False)
        assert run(s.get_quote("NSE", "1", max_wait=2.0)) == {}
        assert "no token within 2.0s" in ac.last_quote_miss_reason()

    def test_lane_budget_shed(self, monkeypatch):
        s = _session(monkeypatch)
        bucket = rl._get_bucket("angelone_quote")
        bucket.rps, bucket.tokens = 0.0001, 1.0
        bucket.updated = time.time()
        assert run(s.get_quote("NSE", "1", max_wait=0.0, lane=b.CANDIDATE)) == {}
        assert "lane budget" in ac.last_quote_miss_reason()

    def test_rate_limited_answer(self, monkeypatch):
        s = _session(monkeypatch, _Resp(403, {"message": "Access denied because of exceeding access rate"}))
        assert run(s.get_quote("NSE", "1")) == {}
        assert "rate-limited" in ac.last_quote_miss_reason()

    def test_empty_fetched_list(self, monkeypatch):
        s = _session(monkeypatch)
        assert run(s.get_quote("NSE", "1")) == {}
        assert ac.last_quote_miss_reason() == "AngelOne returned no quote for this token"

    def test_a_later_good_call_clears_an_earlier_reason(self, monkeypatch):
        s = _session(monkeypatch)
        run(s.get_quote("NSE", "1"))
        assert ac.last_quote_miss_reason() is not None
        monkeypatch.setattr(httpx, "AsyncClient",
                            lambda **kw: _Client(_Resp(200, {"data": {"fetched": [{"ltp": 9}]}})))
        run(s.get_quote("NSE", "1"))
        assert ac.last_quote_miss_reason() is None

    def test_reason_is_per_thread(self):
        ac._note_quote_miss("main thread reason")
        seen = []
        t = threading.Thread(target=lambda: seen.append(ac.last_quote_miss_reason()))
        t.start(); t.join()
        assert seen == [None] and ac.last_quote_miss_reason() == "main thread reason"


# ── the AngelOne-first helper logs it ────────────────────────────────────────
class _FakeSession:
    def __init__(self, reason):
        self.reason = reason

    def is_configured(self):
        return True

    async def get_quote(self, exchange, token, max_wait=20.0, lane=None):
        ac._note_quote_miss(self.reason)
        return {}


@pytest.fixture
def first(monkeypatch):
    monkeypatch.setattr(angelone_scrip_master, "get_token", lambda base: "764885")
    monkeypatch.setattr(rl, "in_cooldown", lambda p: False)
    monkeypatch.setattr(main, "_waterfall_equity_base", lambda s: str(s).upper().replace(".NS", ""))
    monkeypatch.delenv("QUOTE_ANGELONE_FIRST", raising=False)
    main._AO_MISS_LOG.clear()


def _miss_lines(caplog, sym):
    return [r.getMessage() for r in caplog.records if f"AngelOne-first did not price {sym}" in r.getMessage()]


def test_the_specific_reason_is_in_the_log_line(first, monkeypatch, caplog):
    monkeypatch.setattr(ac, "get_session", lambda: _FakeSession("angelone_quote rate bucket had no token within 2.0s"))
    with caplog.at_level(logging.INFO, logger="market-data-service"):
        assert main._angelone_rest_quote_first("PNB.NS") is None
    lines = _miss_lines(caplog, "PNB.NS")
    assert len(lines) == 1
    assert "empty answer: angelone_quote rate bucket had no token within 2.0s" in lines[0]


def test_without_a_recorded_reason_the_old_text_is_kept(first, monkeypatch, caplog):
    monkeypatch.setattr(ac, "get_session", lambda: _FakeSession(None))
    with caplog.at_level(logging.INFO, logger="market-data-service"):
        main._angelone_rest_quote_first("ONGC.NS")
    lines = _miss_lines(caplog, "ONGC.NS")
    assert len(lines) == 1 and "empty answer: rate bucket busy, rate-limit cooldown or no quote for the token" in lines[0]


def test_a_stale_reason_from_an_earlier_call_does_not_label_this_miss(first, monkeypatch, caplog):
    ac._note_quote_miss("left over from a previous call")

    class Silent(_FakeSession):          # a session that records nothing (older build / fake)
        async def get_quote(self, exchange, token, max_wait=20.0, lane=None):
            return {}

    monkeypatch.setattr(ac, "get_session", lambda: Silent(None))
    with caplog.at_level(logging.INFO, logger="market-data-service"):
        main._angelone_rest_quote_first("ITC.NS")
    assert "left over" not in " ".join(_miss_lines(caplog, "ITC.NS"))


def test_still_returns_none_and_logs_once_per_window(first, monkeypatch, caplog):
    monkeypatch.setattr(ac, "get_session", lambda: _FakeSession("AngelOne returned no quote for this token"))
    with caplog.at_level(logging.INFO, logger="market-data-service"):
        assert main._angelone_rest_quote_first("SRF.NS") is None
        assert main._angelone_rest_quote_first("SRF.NS") is None
    assert len(_miss_lines(caplog, "SRF.NS")) == 1
