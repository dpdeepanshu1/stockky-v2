"""
group224 (item A3 of the 2026-10-07 open-market log review): a transient /history failure (timeout, 403/429/5xx)
reuses the last good daily candles for that symbol/period/interval instead of dropping the symbol for the cycle.

  * definite answers (empty answer, 404/400) never fall back
  * intraday intervals never fall back
  * a copy older than CANDIDATE_HISTORY_STALE_FALLBACK_S is not used; 0 turns the feature off
  * the failure reason is still recorded, so the cycle's alarm and the no-history pause logic are unchanged
No network: fake clients; the clock is patched where time matters.
"""
from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.environ.setdefault("DATABASE_URL", "sqlite:///:memory:")

import httpx

from candidate_engine import candidates as cd


def run(c):
    return asyncio.run(c)


class _Resp:
    def __init__(self, code, payload=None):
        self.status_code, self._p, self.text = code, payload, ""

    def json(self):
        return self._p


class _Client:
    """/history answers from a script: each item is candles (list), a status code (int) or an exception."""

    def __init__(self, script):
        self.script, self.calls = list(script), 0

    async def get(self, url, timeout=None, params=None):
        self.calls += 1
        h = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(h, Exception):
            raise h
        if isinstance(h, int):
            return _Resp(h, {})
        return _Resp(200, {"candles": h})


CANDLES = [{"close": 100 + i, "volume": 1000} for i in range(10)]


def test_timeout_after_good_answer_reuses_candles():
    c = _Client([CANDLES, httpx.ReadTimeout("")])
    assert run(cd._fetch_history(c, "DIVISLAB", "1mo", "1d")) == CANDLES
    again = run(cd._fetch_history(c, "DIVISLAB", "1mo", "1d"))
    assert again == CANDLES
    assert cd._HIST_REASON.get("DIVISLAB") == "ReadTimeout"      # the failure is still recorded
    assert cd._HISTORY_STALE_USED[0] == 1


def test_http_403_and_5xx_also_fall_back():
    for code in (403, 429, 503):
        cd.clear_history_state()
        c = _Client([CANDLES, code])
        run(cd._fetch_history(c, "X", "1mo", "1d"))
        assert run(cd._fetch_history(c, "X", "1mo", "1d")) == CANDLES, code


def test_definite_answers_never_fall_back():
    for second in (404, 400, []):
        cd.clear_history_state()
        c = _Client([CANDLES, second])
        run(cd._fetch_history(c, "X", "1mo", "1d"))
        assert run(cd._fetch_history(c, "X", "1mo", "1d")) == [], second


def test_no_prior_good_answer_returns_empty():
    c = _Client([httpx.ReadTimeout("")])
    assert run(cd._fetch_history(c, "NEW", "1mo", "1d")) == []


def test_intraday_interval_never_falls_back():
    c = _Client([CANDLES, httpx.ReadTimeout("")])
    run(cd._fetch_history(c, "X", "1d", "60m"))
    assert run(cd._fetch_history(c, "X", "1d", "60m")) == []


def test_other_period_or_symbol_is_not_mixed_up():
    c = _Client([CANDLES, httpx.ReadTimeout("")])
    run(cd._fetch_history(c, "A", "1mo", "1d"))
    assert run(cd._fetch_history(c, "B", "1mo", "1d")) == []
    c2 = _Client([CANDLES, httpx.ReadTimeout("")])
    run(cd._fetch_history(c2, "A", "1mo", "1d"))
    assert run(cd._fetch_history(c2, "A", "6mo", "1wk")) == []


def test_old_copy_is_not_used(monkeypatch):
    t = [1000.0]
    monkeypatch.setattr(cd.time, "monotonic", lambda: t[0])
    c = _Client([CANDLES, httpx.ReadTimeout("")])
    run(cd._fetch_history(c, "X", "1mo", "1d"))
    t[0] += cd.HISTORY_STALE_FALLBACK_S + 1
    assert run(cd._fetch_history(c, "X", "1mo", "1d")) == []


def test_zero_turns_it_off(monkeypatch):
    monkeypatch.setattr(cd, "HISTORY_STALE_FALLBACK_S", 0.0)
    c = _Client([CANDLES, httpx.ReadTimeout("")])
    run(cd._fetch_history(c, "X", "1mo", "1d"))
    assert run(cd._fetch_history(c, "X", "1mo", "1d")) == []
    assert not cd._HISTORY_LAST_GOOD


def test_returned_copy_is_independent():
    c = _Client([CANDLES, httpx.ReadTimeout("")])
    run(cd._fetch_history(c, "X", "1mo", "1d"))
    got = run(cd._fetch_history(c, "X", "1mo", "1d"))
    got.append({"close": 1})
    assert len(run(cd._fetch_history(c, "X", "1mo", "1d"))) == len(CANDLES)


def test_cache_is_bounded(monkeypatch):
    monkeypatch.setattr(cd, "_HISTORY_LAST_GOOD_MAX", 3)
    c = _Client([CANDLES])
    for i in range(6):
        run(cd._fetch_history(c, f"S{i}", "1mo", "1d"))
    assert len(cd._HISTORY_LAST_GOOD) <= 4


def test_clear_history_state_clears_it():
    c = _Client([CANDLES])
    run(cd._fetch_history(c, "X", "1mo", "1d"))
    assert cd._HISTORY_LAST_GOOD
    cd.clear_history_state()
    assert not cd._HISTORY_LAST_GOOD
