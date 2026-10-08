"""group255: candle cooldowns climb a longer ladder, and the budget logs how long AngelOne blocked the candle endpoint.

Why: the 2026-10-08 log (with the group254 counters) showed only 3 real candle calls since boot, 2 of them answered 403, the
second one 0.0 s after the 30 s cooldown ended, while quotes ran at about 1.6 calls a second. Our own rate cannot explain that;
AngelOne's block outlasts a 30-60 s cooldown. So candle trips now escalate 30, 60, 120, 240 ... up to
ANGELONE_CANDLE_COOLDOWN_MAX_S (default 600; 60 = the old ceiling), and the first candle call answered normally after a run of
403s logs how long the run lasted. Quote cooldowns are unchanged (ceiling 60 s).
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
    for k in ("ANGELONE_CANDLE_COOLDOWN_MAX_S", "ANGELONE_GLOBAL_COOLDOWN_S", "ANGELONE_GLOBAL_COOLDOWN_MAX_S",
              "ANGELONE_CANDLE_SLOWDOWN_S", "ANGELONE_SPLIT_COOLDOWN", "ANGELONE_BUDGET"):
        monkeypatch.delenv(k, raising=False)
    rl._buckets.pop("angelone_candle", None)
    b._reset()
    yield
    rl._buckets.pop("angelone_candle", None)
    b._reset()


class _Clock:
    def __init__(self, t=2_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


@pytest.fixture
def clock(monkeypatch):
    c = _Clock()
    monkeypatch.setattr(b.time, "time", c)
    return c


def _next_trip(clock, dur):
    clock.t += dur + 1          # the cooldown just ended; the next call is refused again
    return b.trip("getCandleData")


def test_candle_ladder_doubles_past_sixty(clock):
    d = [b.trip("getCandleData")]
    for _ in range(5):
        d.append(_next_trip(clock, d[-1]))
    assert d == [30.0, 60.0, 120.0, 240.0, 480.0, 600.0]
    assert _next_trip(clock, 600.0) == 600.0          # stays at the ceiling


def test_ladder_restarts_after_a_long_quiet_spell(clock):
    d1 = b.trip("getCandleData")
    d2 = _next_trip(clock, d1)
    assert d2 == 60.0
    clock.t += d2 + 5000                               # far outside the escalation window
    assert b.trip("getCandleData") == 30.0


def test_env_ceiling_restores_the_old_sixty(clock, monkeypatch):
    monkeypatch.setenv("ANGELONE_CANDLE_COOLDOWN_MAX_S", "60")
    d = [b.trip("getCandleData")]
    for _ in range(3):
        d.append(_next_trip(clock, d[-1]))
    assert d == [30.0, 60.0, 60.0, 60.0]


def test_bad_env_falls_back_to_the_default(clock, monkeypatch):
    monkeypatch.setenv("ANGELONE_CANDLE_COOLDOWN_MAX_S", "abc")
    d = [b.trip("getCandleData")]
    for _ in range(4):
        d.append(_next_trip(clock, d[-1]))
    assert d[-1] == 480.0


def test_quote_family_keeps_its_sixty_second_ceiling(clock):
    d = [b.trip("quote")]
    for _ in range(4):
        clock.t += d[-1] + 1
        d.append(b.trip("quote"))
    assert d == [30.0, 60.0, 60.0, 60.0, 60.0]


def test_recovery_is_logged_once_with_the_block_length(clock, caplog):
    b.trip("getCandleData")
    clock.t += 95
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="angelone-budget"):
        b.note_candle_ok()
        b.note_candle_ok()
    lines = [r.getMessage() for r in caplog.records if "answered again" in r.getMessage()]
    assert len(lines) == 1 and "95s after the first 403" in lines[0] and "trips so far: 1" in lines[0]
    assert b.stats()["candle_calls"]["last_block_lasted_s"] == 95.0


def test_recovery_run_spans_several_trips(clock, caplog):
    d1 = b.trip("getCandleData")
    _next_trip(clock, d1)                              # second trip, 31 s in
    clock.t += 70
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="angelone-budget"):
        b.note_candle_ok()
    line = [r.getMessage() for r in caplog.records if "answered again" in r.getMessage()][0]
    assert "101s after the first 403" in line and "trips so far: 2" in line


def test_ok_without_any_trip_logs_nothing(clock, caplog):
    caplog.clear()
    with caplog.at_level(logging.INFO, logger="angelone-budget"):
        b.note_candle_ok()
    assert [r for r in caplog.records if "answered again" in r.getMessage()] == []
    assert b.stats()["candle_calls"]["last_block_lasted_s"] is None


def test_a_new_run_starts_after_recovery(clock):
    b.trip("getCandleData")
    clock.t += 40
    b.note_candle_ok()
    clock.t += 10
    b.trip("getCandleData")                            # trips again; the run restarts
    clock.t += 20
    b.note_candle_ok()
    assert b.stats()["candle_calls"]["last_block_lasted_s"] == 20.0


def test_reset_clears_recovery_state(clock):
    b.trip("getCandleData")
    clock.t += 10
    b.note_candle_ok()
    b._reset()
    assert b.stats()["candle_calls"]["last_block_lasted_s"] is None


def test_note_ok_never_raises():
    class _Boom:
        def __enter__(self):
            raise RuntimeError("lock broke")

        def __exit__(self, *a):
            return False

    real = b._lock
    b._lock = _Boom()
    try:
        b.note_candle_ok()
    finally:
        b._lock = real


def test_client_wrapper_forwards_and_never_raises(monkeypatch):
    calls = []
    monkeypatch.setattr(ac._budget, "note_candle_ok", lambda: calls.append(1))
    ac._budget_note_candle_ok()
    assert calls == [1]
    monkeypatch.setattr(ac._budget, "note_candle_ok", lambda: (_ for _ in ()).throw(RuntimeError("x")))
    ac._budget_note_candle_ok()


class _Resp:
    def __init__(self, status=200, body=None, text="ok"):
        self.status_code = status
        self._body = body if body is not None else {"data": [["t", 1, 2, 3, 4, 5]]}
        self.text = text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class _FakeHttp:
    resp = _Resp()

    def __init__(self, *a, **k):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, *a, **k):
        return _FakeHttp.resp


def _get(monkeypatch):
    async def _noop(self):
        return None

    monkeypatch.setattr(ac.AngelOneSession, "ensure_session", _noop)
    monkeypatch.setattr(ac.AngelOneSession, "_headers", lambda self: {})
    monkeypatch.setattr(ac.httpx, "AsyncClient", _FakeHttp)
    monkeypatch.setattr(ac, "_rl_try_acquire", lambda *a, **k: True)
    sess = ac.AngelOneSession.__new__(ac.AngelOneSession)
    return asyncio.run(sess.get_candles("NSE", "1", "ONE_DAY", "a", "b"))


def test_a_good_candle_answer_closes_the_run(monkeypatch):
    b._candle_series_start = 1.0                       # a run of 403s is open
    _FakeHttp.resp = _Resp()
    assert _get(monkeypatch)
    assert b._candle_series_start == 0.0


def test_a_403_answer_does_not_close_the_run(monkeypatch):
    b._candle_series_start = 1.0
    _FakeHttp.resp = _Resp(status=403, text="Access denied because of exceeding access rate")
    monkeypatch.setattr(ac, "_is_rate_limit_response", lambda *a, **k: True)
    monkeypatch.setattr(ac, "_safe_json", lambda r: None)
    monkeypatch.setattr(ac, "_rl_set_cooldown", lambda *a, **k: None)   # keep the shared rate-limiter state clean
    assert _get(monkeypatch) == []
    assert b._candle_series_start != 0.0
