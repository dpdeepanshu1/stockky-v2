"""Round 197: a cut-short /check pass says where its budget went.

4 Nov VM log: "check_for_changes: stopped after 241s at 60/469 symbols (CHECK_MAX_SECONDS=240)" = ~4 s per symbol, with no way to tell the
fresh fetch from the 1 s stagger or the yield to live requests. The warning now ends with a Breakdown sentence. Evidence only: the rotation,
the budget, the stagger and the yield are unchanged (tests/test_e285_round185_check_rotation.py still pins them)."""
from __future__ import annotations

import logging
import os
import sys
import time as _real_time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
sys.path.insert(0, _SERVICE)
sys.path.insert(0, os.path.join(_SERVICE, "event"))

import pytest

import main as em  # noqa: E402  (event/main.py)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for d in (em._mem, em._mem_exp):
        d.clear()
    monkeypatch.setattr(em, "_redis", None)
    monkeypatch.setattr(em, "_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.delenv("CHECK_MAX_SECONDS", raising=False)
    monkeypatch.setenv("CHECK_WORKERS", "1")      # round 201: these cases pin the one-at-a-time loop (fake clock)
    em._live_requests[0] = 0
    yield
    em._live_requests[0] = 0
    for d in (em._mem, em._mem_exp):
        d.clear()


class _FakeTime:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.now += s

    def __getattr__(self, name):
        return getattr(_real_time, name)


def _setup(monkeypatch, symbols, cost=3.0, budget="10"):
    ft = _FakeTime()
    monkeypatch.setattr(em, "time", ft)
    monkeypatch.setenv("CHECK_MAX_SECONDS", budget)

    def fake_fetch(sym, force=False):
        ft.now += cost
        return {"symbol": sym, "recent_news": [], "next_earnings_date": None}

    monkeypatch.setattr(em, "_fetch_events", fake_fetch)
    em.subscribe(em.SubscribeRequest(symbols=list(symbols)))
    return ft


def _cut_warning(caplog):
    return [r.getMessage() for r in caplog.records if "stopped after" in r.getMessage()]


def test_cut_short_pass_warning_carries_a_breakdown(monkeypatch, caplog):
    _setup(monkeypatch, ["a", "b", "c", "d", "e"], cost=3.0, budget="10")
    with caplog.at_level(logging.WARNING):
        out = em.check_for_changes()
    assert out["complete"] is False
    msgs = _cut_warning(caplog)
    assert len(msgs) == 1
    m = msgs[0]
    # still carries the old text (the ops runbook greps it) ...
    assert "the rest are next pass (resumes at index" in m
    # ... plus the new sentence: 3 fresh fetches (9 s), 3 staggers (3 s, the last one before the symbol where the budget check stops the pass), nothing cached, nobody waited
    assert "Breakdown: 3 fresh fetch(es) 9s summed (3.0s each), 0 cached" in m
    assert "3s of 1 s stagger" in m
    assert "0s waiting for live requests (0 symbol(s) had to wait)" in m


def test_cached_symbols_are_counted_apart_from_fresh_ones(monkeypatch, caplog):
    _setup(monkeypatch, ["a", "b", "c", "d"], cost=3.0, budget="7")
    em._redis_set(f"{em.EVENT_CACHE_PREFIX}A.NS", {"symbol": "A.NS", "recent_news": []}, ttl=600)
    with caplog.at_level(logging.WARNING):
        em.check_for_changes()
    m = _cut_warning(caplog)[0]
    assert "1 cached" in m            # A was cached: no stagger after it
    assert "fresh fetch(es)" in m and "2 fresh fetch(es)" in m


def test_wait_for_live_requests_is_measured(monkeypatch, caplog):
    ft = _setup(monkeypatch, ["a", "b", "c", "d"], cost=3.0, budget="8")
    real_sleep = ft.sleep
    state = {"half_waits": 0}

    def sleep(s):
        real_sleep(s)
        if s == 0.5:                      # a live request is in flight for the first two half-second waits only
            state["half_waits"] += 1
            if state["half_waits"] >= 2:
                em._live_requests[0] = 0

    ft.sleep = sleep
    em._live_requests[0] = 1
    with caplog.at_level(logging.WARNING):
        em.check_for_changes()
    m = _cut_warning(caplog)[0]
    assert "1s waiting for live requests (1 symbol(s) had to wait)" in m


def test_breakdown_never_raises_on_junk():
    assert em._check_breakdown({}).startswith("Breakdown: 0 fresh fetch(es)")
    assert em._check_breakdown({"fresh": "x"}) == "Breakdown: unavailable"
    assert em._check_breakdown(None) == "Breakdown: unavailable"


def test_rotation_and_response_shape_are_unchanged(monkeypatch):
    _setup(monkeypatch, ["a", "b", "c", "d", "e"], cost=3.0, budget="10")
    out = em.check_for_changes()
    assert set(out) == {"checked", "total", "complete", "next_cursor", "changes", "checked_at"}
    assert out["checked"] == 3 and out["next_cursor"] == 3
