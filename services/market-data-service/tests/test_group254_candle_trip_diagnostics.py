"""group254: every real getCandleData send is counted, and each candle trip logs what was sent just before it.

Why: the 2026-10-08 pre-market log with GET /angelone/budget showed candle 403 trips although the group251 slowdown was
active (bucket 1.0/s, effective 0.5/s, throttle_events 0, waiters 0) at about 10-20 real candle calls a minute, far below
AngelOne's documented 3/s, 180/min, 5000/h. These counters separate "AngelOne blocks longer than our cooldown" from "too
many calls from us" from "another consumer of the same key or IP". Diagnostics only: no limit or cooldown changes.
"""
import asyncio
import logging
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import angelone_budget as b
import angelone_client as ac
import rate_limiter as rl


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    for k in ("ANGELONE_CANDLE_SLOWDOWN_S", "ANGELONE_CANDLE_SLOWDOWN_FACTOR", "ANGELONE_SPLIT_COOLDOWN", "ANGELONE_BUDGET",
              "ANGELONE_GLOBAL_COOLDOWN_S", "ANGELONE_GLOBAL_COOLDOWN_MAX_S"):
        monkeypatch.delenv(k, raising=False)
    rl._buckets.pop("angelone_candle", None)
    rl._buckets.pop("angelone_quote", None)
    b._reset()
    yield
    rl._buckets.pop("angelone_candle", None)
    rl._buckets.pop("angelone_quote", None)
    b._reset()


class _Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(b.time, "time", c)
    return c


def _ctx_lines(caplog):
    return [r.getMessage() for r in caplog.records if "candle 403 context" in r.getMessage()]


def test_sends_are_counted_with_windows(clock):
    for _ in range(3):
        b.note_candle_sent()
    clock.t += 30
    b.note_candle_sent()
    s = b.stats()["candle_calls"]
    assert s == {"sent_total": 4, "sent_last_10s": 1, "sent_last_60s": 4, "tripped_on_first_call_after_cooldown": 0}
    clock.t += 40
    s = b.stats()["candle_calls"]
    assert s["sent_last_60s"] == 1 and s["sent_total"] == 4


def test_first_trip_logs_counts_and_no_earlier_cooldown(clock, caplog):
    for _ in range(2):
        b.note_candle_sent()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        assert b.trip("getCandleData") > 0
    lines = _ctx_lines(caplog)
    assert len(lines) == 1
    assert "2 in the last 10s, 2 in the last 60s, 2 since start" in lines[0]
    assert "no earlier candle cooldown" in lines[0]
    assert b.stats()["candle_calls"]["tripped_on_first_call_after_cooldown"] == 0


def test_trip_right_after_cooldown_reports_first_call_after_it(clock, caplog):
    b.note_candle_sent()
    b.trip("getCandleData")                      # cooldown 30 s
    clock.t += 31                                # cooldown over
    b.note_candle_sent()                         # first send after it
    clock.t += 1.5
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        assert b.trip("getCandleData") > 0
    line = _ctx_lines(caplog)[0]
    assert "1.5s after the first candle call sent once the previous cooldown ended (1 call(s) sent since it ended)" in line
    assert b.stats()["candle_calls"]["tripped_on_first_call_after_cooldown"] == 1


def test_counts_calls_since_cooldown_end_not_before(clock, caplog):
    b.note_candle_sent()
    b.trip("getCandleData")
    clock.t += 31
    for _ in range(3):
        b.note_candle_sent()
        clock.t += 0.5
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        b.trip("getCandleData")
    assert "(3 call(s) sent since it ended)" in _ctx_lines(caplog)[0]


def test_sends_during_a_cooldown_are_counted_but_do_not_arm_the_after_cooldown_marker(clock, caplog):
    b.trip("getCandleData")
    clock.t += 5                                 # still inside the 30 s cooldown
    b.note_candle_sent()
    assert b.stats()["candle_calls"]["sent_total"] == 1
    clock.t += 30                                # cooldown over, nothing sent since
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        b.trip("getCandleData")
    assert "no earlier candle cooldown" in _ctx_lines(caplog)[0]


def test_late_403_inside_a_cooldown_logs_nothing_more(clock, caplog):
    b.trip("getCandleData")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        assert b.trip("getCandleData") == 0.0
    assert _ctx_lines(caplog) == []


def test_quote_trip_has_no_candle_context_and_does_not_count(clock, caplog):
    b.note_candle_sent()
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        assert b.trip("getMarketData") > 0
    assert _ctx_lines(caplog) == []
    assert b.stats()["candle_calls"]["tripped_on_first_call_after_cooldown"] == 0


def test_budget_off_trip_is_a_noop(clock, monkeypatch, caplog):
    monkeypatch.setenv("ANGELONE_BUDGET", "0")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        assert b.trip("getCandleData") == 0.0
    assert _ctx_lines(caplog) == []


def test_reset_clears_the_counters(clock):
    b.note_candle_sent()
    b.trip("getCandleData")
    clock.t += 31
    b.note_candle_sent()
    b.trip("getCandleData")
    assert b.stats()["candle_calls"]["tripped_on_first_call_after_cooldown"] == 1
    b._reset()
    assert b.stats()["candle_calls"] == {"sent_total": 0, "sent_last_10s": 0, "sent_last_60s": 0,
                                         "tripped_on_first_call_after_cooldown": 0}


def test_note_never_raises():
    class _Boom:
        def __enter__(self):
            raise RuntimeError("lock broke")

        def __exit__(self, *a):
            return False

    real = b._lock
    b._lock = _Boom()
    try:
        b.note_candle_sent()   # must not raise
    finally:
        b._lock = real          # restored before the autouse teardown calls _reset()


def test_client_wrapper_forwards_and_never_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(ac._budget, "note_candle_sent", lambda: calls.append(1))
    ac._budget_note_candle_sent()
    assert calls == [1]
    monkeypatch.setattr(ac._budget, "note_candle_sent", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    ac._budget_note_candle_sent()   # swallowed


class _Resp:
    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body if body is not None else {"data": [["t", 1, 2, 3, 4, 5]]}
        self.text = text or "ok"

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _FakeHttp:
    posts = 0
    resp = _Resp()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        _FakeHttp.posts += 1
        return _FakeHttp.resp


def _candles(monkeypatch):
    async def _noop(self):
        return None

    monkeypatch.setattr(ac.AngelOneSession, "ensure_session", _noop)
    monkeypatch.setattr(ac.AngelOneSession, "_headers", lambda self: {})
    monkeypatch.setattr(ac.httpx, "AsyncClient", _FakeHttp)
    monkeypatch.setattr(ac, "_rl_try_acquire", lambda *a, **k: True)
    _FakeHttp.posts = 0
    sess = ac.AngelOneSession.__new__(ac.AngelOneSession)
    return asyncio.run(sess.get_candles("NSE", "1", "ONE_DAY", "a", "b"))


def test_quote_sends_are_counted_and_shown_in_the_candle_trip_line(clock, caplog):
    for _ in range(5):
        b.note_quote_sent()
    clock.t += 30
    b.note_quote_sent()
    q = b.stats()["quote_calls"]
    assert q == {"sent_total": 6, "sent_last_10s": 1, "sent_last_60s": 6}
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="angelone-budget"):
        b.trip("getCandleData")
    assert "quote calls sent: 1 in the last 10s, 6 in the last 60s" in _ctx_lines(caplog)[0]
    b._reset()
    assert b.stats()["quote_calls"] == {"sent_total": 0, "sent_last_10s": 0, "sent_last_60s": 0}


def test_quote_note_never_raises():
    class _Boom:
        def __enter__(self):
            raise RuntimeError("lock broke")

        def __exit__(self, *a):
            return False

    real = b._lock
    b._lock = _Boom()
    try:
        b.note_quote_sent()
    finally:
        b._lock = real


def test_client_quote_wrapper_forwards_and_never_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(ac._budget, "note_quote_sent", lambda: calls.append(1))
    ac._budget_note_quote_sent()
    assert calls == [1]
    monkeypatch.setattr(ac._budget, "note_quote_sent", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    ac._budget_note_quote_sent()


def test_get_quotes_batch_counts_each_real_send(monkeypatch):
    async def _noop(self):
        return None

    monkeypatch.setattr(ac.AngelOneSession, "ensure_session", _noop)
    monkeypatch.setattr(ac.AngelOneSession, "_headers", lambda self: {})
    monkeypatch.setattr(ac.httpx, "AsyncClient", _FakeHttp)
    monkeypatch.setattr(ac, "_rl_acquire", lambda *a, **k: True)
    _FakeHttp.posts = 0
    _FakeHttp.resp = _Resp(body={"data": {"fetched": [{"x": 1}]}})
    sess = ac.AngelOneSession.__new__(ac.AngelOneSession)
    out = asyncio.run(sess.get_quotes_batch("NSE", ["1", "2"]))
    assert out == [{"x": 1}] and _FakeHttp.posts == 1
    assert b.stats()["quote_calls"]["sent_total"] == 1
    assert b.stats()["candle_calls"]["sent_total"] == 0


def test_get_candles_counts_each_real_send(monkeypatch):
    _FakeHttp.resp = _Resp()
    out = _candles(monkeypatch)
    assert out and _FakeHttp.posts == 1
    assert b.stats()["candle_calls"]["sent_total"] == 1


def test_get_candles_skipped_by_cooldown_is_not_counted(monkeypatch):
    b.trip("getCandleData")
    monkeypatch.setattr(ac, "_rl_in_cooldown", lambda p: False)   # leave only the budget's own skip active
    out = _candles(monkeypatch)
    assert out == [] and _FakeHttp.posts == 0
    assert b.stats()["candle_calls"]["sent_total"] == 0
