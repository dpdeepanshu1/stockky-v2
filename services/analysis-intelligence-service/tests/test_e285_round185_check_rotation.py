"""Round 185: /check is a ROTATING pass.

4 Nov log: "check_for_changes: stopped after 242s at 73/462 symbols (CHECK_MAX_SECONDS=240); the rest are next pass".
Every pass began at index 0, so the same first symbols were re-checked and the tail of the list was never diffed.
Now the pass resumes at `check_cursor` (saved with the state), and a symbol already in the event cache skips the 1 s stagger."""
from __future__ import annotations

import os
import sys
import time as _real_time
import types

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
    monkeypatch.setattr(em, "_STATE_FILE", str(tmp_path / "state.json"))   # never touch the real /data volume
    monkeypatch.delenv("CHECK_MAX_SECONDS", raising=False)
    yield
    for d in (em._mem, em._mem_exp):
        d.clear()


class _FakeTime:
    """em.time with a clock the test moves: every sleep / fetch advances it, everything else is the real module."""

    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, s):
        self.sleeps.append(s)
        self.now += s

    def __getattr__(self, name):
        return getattr(_real_time, name)


def _setup(monkeypatch, symbols, cost_per_fetch=100.0, budget="250"):
    ft = _FakeTime()
    monkeypatch.setattr(em, "time", ft)
    monkeypatch.setenv("CHECK_MAX_SECONDS", budget)
    fetched = []

    def fake_fetch(sym, force=False):
        fetched.append(sym)
        ft.now += cost_per_fetch
        return {"symbol": sym, "recent_news": [], "next_earnings_date": None}

    monkeypatch.setattr(em, "_fetch_events", fake_fetch)
    em.subscribe(em.SubscribeRequest(symbols=list(symbols)))
    return ft, fetched


def test_pass_cut_short_by_budget_resumes_where_it_stopped(monkeypatch):
    ft, fetched = _setup(monkeypatch, ["a", "b", "c", "d", "e", "f"], cost_per_fetch=100.0, budget="250")
    first = em.check_for_changes()
    assert first["total"] == 6 and first["checked"] == 3 and first["complete"] is False
    assert first["next_cursor"] == 3
    assert fetched == ["A.NS", "B.NS", "C.NS"]
    assert em._load_state()["check_cursor"] == 3

    fetched.clear()
    second = em.check_for_changes()
    # the second pass starts at D, NOT at A again (the bug)
    assert fetched == ["D.NS", "E.NS", "F.NS"]
    # a pass only covers 3 of 6 (so "complete" is False), but the cursor has wrapped: the two passes together covered the whole list
    assert second["checked"] == 3 and second["next_cursor"] == 0 and second["complete"] is False


def test_every_symbol_is_reached_over_successive_passes(monkeypatch):
    syms = [f"s{i}" for i in range(10)]
    ft, fetched = _setup(monkeypatch, syms, cost_per_fetch=100.0, budget="350")
    seen = set()
    for _ in range(6):
        em.check_for_changes()
        seen.update(fetched)
        if len(seen) == 10:
            break
    assert seen == {f"S{i}.NS" for i in range(10)}


def test_full_pass_leaves_cursor_at_zero_and_reports_complete(monkeypatch):
    ft, fetched = _setup(monkeypatch, ["a", "b"], cost_per_fetch=1.0, budget="240")
    out = em.check_for_changes()
    assert out["checked"] == 2 and out["total"] == 2 and out["complete"] is True and out["next_cursor"] == 0
    assert fetched == ["A.NS", "B.NS"]


def test_cursor_past_the_end_after_unsubscribe_wraps(monkeypatch):
    ft, fetched = _setup(monkeypatch, ["a", "b", "c"], cost_per_fetch=1.0)
    state = em._load_state()
    state["check_cursor"] = 17                     # list shrank since the cursor was saved
    em._save_state(state)
    em.check_for_changes()
    assert fetched == ["C.NS", "A.NS", "B.NS"]     # 17 % 3 == 2


def test_junk_cursor_is_treated_as_zero(monkeypatch):
    ft, fetched = _setup(monkeypatch, ["a", "b"], cost_per_fetch=1.0)
    state = em._load_state()
    state["check_cursor"] = "nope"
    em._save_state(state)
    em.check_for_changes()
    assert fetched == ["A.NS", "B.NS"]


def test_no_subscriptions_is_still_clean():
    out = em.check_for_changes()
    assert out["checked"] == 0 and out["total"] == 0 and out["complete"] is True and out["changes"] == []


def test_cached_symbol_skips_the_one_second_stagger(monkeypatch):
    ft, fetched = _setup(monkeypatch, ["a", "b", "c"], cost_per_fetch=0.0)
    # A and B are in the event cache (no network call), C is not
    for s in ("A.NS", "B.NS"):
        em._redis_set(f"{em.EVENT_CACHE_PREFIX}{s}", {"symbol": s, "recent_news": []}, ttl=100)
    em.check_for_changes()
    # sleeps happen only BEFORE a symbol whose predecessor needed the network: A cached -> none before B, B cached -> none before C
    assert ft.sleeps == []


def test_uncached_symbols_are_still_staggered(monkeypatch):
    ft, fetched = _setup(monkeypatch, ["a", "b", "c"], cost_per_fetch=0.0)
    em.check_for_changes()
    assert ft.sleeps == [1, 1]                     # between symbols, not before the first


def test_more_cached_symbols_fit_in_one_pass(monkeypatch):
    """The 1 s stagger alone capped a pass at ~240 symbols: with 300 cached symbols the whole list is now checked in one pass."""
    syms = [f"x{i}" for i in range(300)]
    ft, fetched = _setup(monkeypatch, syms, cost_per_fetch=0.0, budget="240")
    for i in range(300):
        em._redis_set(f"{em.EVENT_CACHE_PREFIX}X{i}.NS", {"symbol": f"X{i}.NS", "recent_news": []}, ttl=100)
    out = em.check_for_changes()
    assert out["checked"] == 300 and out["complete"] is True
