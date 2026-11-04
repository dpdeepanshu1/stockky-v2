"""Round 201: /check overlaps fresh fetches (CHECK_WORKERS, default 3) without losing order, cursor or the budget stop."""
from __future__ import annotations

import os
import sys
import threading
import time as _real_time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
sys.path.insert(0, _SERVICE)
sys.path.insert(0, os.path.join(_SERVICE, "event"))

import pytest

import main as em  # noqa: E402  (event/main.py)


class _Time:
    """Real clock, but the 1 s stagger / 0.5 s yields do not really sleep."""
    slept = 0.0

    def monotonic(self):
        return _real_time.monotonic()

    def sleep(self, s):
        _Time.slept += s

    def __getattr__(self, n):
        return getattr(_real_time, n)


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for d in (em._mem, em._mem_exp):
        d.clear()
    monkeypatch.setattr(em, "_redis", None)
    monkeypatch.setattr(em, "_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.delenv("CHECK_MAX_SECONDS", raising=False)
    monkeypatch.delenv("CHECK_WORKERS", raising=False)
    monkeypatch.setattr(em, "time", _Time())
    _Time.slept = 0.0
    em._live_requests[0] = 0
    yield
    em._live_requests[0] = 0
    for d in (em._mem, em._mem_exp):
        d.clear()


def _setup(monkeypatch, symbols, hold=0.03):
    stats = {"now": 0, "max": 0, "order": []}
    lock = threading.Lock()

    def fake_fetch(sym, force=False):
        with lock:
            stats["now"] += 1
            stats["max"] = max(stats["max"], stats["now"])
            stats["order"].append(sym)
        _real_time.sleep(hold)
        with lock:
            stats["now"] -= 1
        return {"symbol": sym, "recent_news": [], "next_earnings_date": None}

    monkeypatch.setattr(em, "_fetch_events", fake_fetch)
    em.subscribe(em.SubscribeRequest(symbols=list(symbols)))
    return stats


def test_fetches_overlap_up_to_the_worker_count_and_all_are_kept(monkeypatch):
    syms = [f"s{i}" for i in range(12)]
    stats = _setup(monkeypatch, syms)
    out = em.check_for_changes()
    assert out["checked"] == 12 and out["complete"] is True and out["next_cursor"] == 0
    assert 1 < stats["max"] <= 3
    assert set(em._load_state()["last_known"]) == {f"S{i}.NS" for i in range(12)}


def test_workers_one_is_the_old_one_at_a_time_loop(monkeypatch):
    monkeypatch.setenv("CHECK_WORKERS", "1")
    stats = _setup(monkeypatch, [f"s{i}" for i in range(6)])
    out = em.check_for_changes()
    assert out["checked"] == 6 and stats["max"] == 1 and stats["order"] == [f"S{i}.NS" for i in range(6)]


def test_fresh_fetch_starts_are_spaced_by_the_one_second_stagger(monkeypatch):
    _setup(monkeypatch, ["a", "b", "c", "d"], hold=0.0)
    em.check_for_changes()
    assert _Time.slept >= 2.0          # 4 fresh starts: at least the gaps between them are asked for (sleep itself is faked)


def test_budget_stop_keeps_a_contiguous_prefix_and_moves_the_cursor_past_it(monkeypatch):
    monkeypatch.setenv("CHECK_MAX_SECONDS", "0.05")
    syms = [f"s{i:02d}" for i in range(30)]
    stats = _setup(monkeypatch, syms, hold=0.04)
    out = em.check_for_changes()
    n = out["checked"]
    assert 0 < n < 30 and out["complete"] is False and out["next_cursor"] == n % 30
    kept = set(em._load_state()["last_known"])
    assert kept == {f"S{i:02d}.NS" for i in range(n)}          # exactly the first n, nothing started-but-dropped, nothing skipped


def test_changes_are_reported_through_the_pool(monkeypatch):
    _setup(monkeypatch, ["a", "b"], hold=0.0)
    em.check_for_changes()
    seq = iter([{"symbol": "X", "recent_news": [{"title": "new headline", "link": "l", "published": "2026-11-04"}], "next_earnings_date": None}] * 2)
    monkeypatch.setattr(em, "_fetch_events", lambda sym, force=False: next(seq))
    out = em.check_for_changes()
    assert out["checked"] == 2 and isinstance(out["changes"], list)


def test_a_failing_fetch_does_not_hang_the_pool(monkeypatch):
    _setup(monkeypatch, ["a", "b", "c"], hold=0.0)

    def boom(sym, force=False):
        raise RuntimeError("yahoo down")

    monkeypatch.setattr(em, "_fetch_events", boom)
    with pytest.raises(RuntimeError):
        em.check_for_changes()
