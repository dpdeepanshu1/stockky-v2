"""group147: the feed-universe refresh warning names the exception type, so an httpx error with an
empty str() (e.g. ReadTimeout) no longer logs "fetch failed, keeping existing feed: " with nothing after it."""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
import main as m


def test_empty_message_exception_still_shows_its_type():
    assert str(httpx.ReadTimeout("")) == ""
    assert m._exc_detail(httpx.ReadTimeout("")) == "ReadTimeout"


def test_message_is_appended_when_present():
    assert m._exc_detail(ValueError("boom")) == "ValueError: boom"
    assert m._exc_detail(RuntimeError("  spaced  ")) == "RuntimeError: spaced"


def test_refresh_warning_is_not_blank_after_the_colon(monkeypatch, caplog):
    monkeypatch.setenv("API_GATEWAY_URL", "http://gw.invalid")
    monkeypatch.setenv("FEED_UNIVERSE_INITIAL_DELAY_S", "0")

    class _Client:
        def __init__(self, *a, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def get(self, url):
            raise httpx.ReadTimeout("")

    calls = {"n": 0}

    async def _sleep(_d):
        calls["n"] += 1
        if calls["n"] > 1:
            raise asyncio.CancelledError()

    monkeypatch.setattr(m.httpx, "AsyncClient", _Client)
    monkeypatch.setattr(m.asyncio, "sleep", _sleep)
    with caplog.at_level(logging.WARNING):
        try:
            asyncio.run(m._refresh_feed_universe_loop())
        except asyncio.CancelledError:
            pass
    msgs = [r.getMessage() for r in caplog.records if "fetch failed" in r.getMessage()]
    assert msgs, "expected the fetch-failed warning"
    assert msgs[0].endswith("keeping existing feed: ReadTimeout")
