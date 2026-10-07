"""
group225 (item 3 of the 2026-10-07 list): the AngelOne feed kept retrying after a 403.

The boot log showed "AngelOne quote batch (x-y) failed" for every batch of every ~3 s cycle. A batch only raises
there when raise_for_status() fired, i.e. _is_rate_limit_response() said "not a rate limit" - so neither the endpoint
cooldown nor the group211 global cooldown ever started. AngelOne's gateway answers the limit with a PLAIN-TEXT body
(r.json() fails -> body None), which the JSON-only classifier could not see. Pinned here:
  * the classifier also reads the raw response text (same wording as the JSON message check)
  * a plain-text 403 on quote, batch and candles starts the cooldown, returns the empty answer and does not raise
  * a 403 with other wording still raises from the client, and the feed then trips the shared cooldown itself
    (_trip_on_http_denied) so it stops retrying every ~3 s
  * a poll cycle that sees the cooldown start mid-cycle stops walking the remaining batches
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import pytest

import angelone_budget as b
import angelone_client as ac
from test_group211_angelone_budget import (  # noqa: F401  (fixtures + harness reused)
    _clean, feed, _Session, _Client, _session, _one_cycle, run,
)
import test_group211_angelone_budget as t211

PLAIN = "Access denied because of exceeding access rate"


class _TextResp:
    """A response whose body is NOT json (r.json() raises), like AngelOne's gateway 403."""

    def __init__(self, status_code=403, text=PLAIN):
        self.status_code = status_code
        self.text = text

    def json(self):
        raise ValueError("not json")

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("error", request=None, response=self)


def test_classifier_reads_the_raw_text_too():
    assert ac._is_rate_limit_response(403, None, PLAIN) is True
    assert ac._is_rate_limit_response(403, None, "ACCESS DENIED for this IP") is True
    assert ac._is_rate_limit_response(403, None, "forbidden") is False
    assert ac._is_rate_limit_response(403, None, None) is False
    assert ac._is_rate_limit_response(403, None) is False                    # old two-argument form unchanged
    assert ac._is_rate_limit_response(200, None, PLAIN) is False
    assert ac._is_rate_limit_response(429, None, "") is True


@pytest.mark.parametrize("call", ["quote", "batch", "candles"])
def test_a_plain_text_403_starts_the_cooldown_and_returns_empty(monkeypatch, call):
    s = _session(monkeypatch, _TextResp())
    if call == "quote":
        assert run(s.get_quote("NSE", "1")) == {}
    elif call == "batch":
        assert run(s.get_quotes_batch("NSE", ["1"])) == []
    else:
        assert run(s.get_candles("NSE", "1", "ONE_DAY", "a", "b")) == []
    assert b.in_global_cooldown()
    sent = len(_Client.sent)
    assert run(s.get_quotes_batch("NSE", ["1"])) == []                       # nothing more goes out
    assert len(_Client.sent) == sent


def test_a_403_with_another_text_is_still_an_error(monkeypatch):
    s = _session(monkeypatch, _TextResp(403, "Invalid session"))
    with pytest.raises(Exception):
        run(s.get_quotes_batch("NSE", ["1"]))
    assert not b.in_global_cooldown()


def _http_error(code):
    req = httpx.Request("POST", "http://x")
    return httpx.HTTPStatusError("e", request=req, response=httpx.Response(code, request=req))


def test_cooldown_running_helper_never_raises(feed, monkeypatch):
    assert feed._cooldown_running() is False
    b.trip("quote")
    assert feed._cooldown_running() is True
    monkeypatch.setitem(sys.modules, "angelone_budget", None)                # import fails -> False
    assert feed._cooldown_running() is False


def test_trip_on_http_denied_trips_for_403_and_429_only(feed):
    assert feed._trip_on_http_denied(_http_error(500)) is False
    assert feed._trip_on_http_denied(RuntimeError("boom")) is False          # no .response
    assert not b.in_global_cooldown()
    assert feed._trip_on_http_denied(_http_error(403)) is True
    assert b.in_global_cooldown() and b.stats()["last_trip_endpoint"] == "quote(batch)"
    assert feed._trip_on_http_denied(_http_error(429)) is False              # already cooling down: no escalation


def test_trip_on_http_denied_429_trips_and_never_raises(feed, monkeypatch):
    assert feed._trip_on_http_denied(_http_error(429)) is True
    b._reset()
    monkeypatch.setitem(sys.modules, "angelone_budget", None)                # import fails -> False, no raise
    assert feed._trip_on_http_denied(_http_error(403)) is False


def test_a_cycle_stops_when_the_cooldown_starts_after_its_first_batch(feed, monkeypatch):
    class _Tripping(_Session):
        async def get_quotes_batch(self, exchange, tokens, lane=None):
            self.calls.append((list(tokens), lane))
            b.trip("quote(batch)")                                           # the 403 of the first batch
            return []

    monkeypatch.setattr(t211, "_Session", _Tripping)                         # _one_cycle builds its own session
    sess = _one_cycle(feed, monkeypatch, 7)
    assert len(sess.calls) == 1                                              # 7 tokens / batches of 2: only 1 sent


def test_an_unrecognised_403_in_a_batch_backs_the_feed_off(feed, monkeypatch):
    class _Denied(_Session):
        async def get_quotes_batch(self, exchange, tokens, lane=None):
            self.calls.append((list(tokens), lane))
            raise _http_error(403)                                           # body not recognised -> raises

    monkeypatch.setattr(t211, "_Session", _Denied)
    sess = _one_cycle(feed, monkeypatch, 7)
    assert len(sess.calls) == 1                                              # first batch tripped it, rest skipped
    assert b.in_global_cooldown()
