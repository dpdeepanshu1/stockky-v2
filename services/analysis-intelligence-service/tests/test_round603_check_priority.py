"""Round 603: /check runs the user-watchlist names first, rotates the dynamic-universe names behind them, and CHECK_STAGGER_S sets the fresh-fetch spacing.

Plain runner: `python tests/test_round603_check_priority.py` (also collected by pytest)."""
from __future__ import annotations

import os
import sys
import tempfile
import time as _real_time

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVICE = os.path.dirname(_HERE)
sys.path.insert(0, _SERVICE)
sys.path.insert(0, os.path.join(_SERVICE, "event"))

import main as em  # noqa: E402  (event/main.py)


class _FakeTime:
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


def _setup(user, auto, cost=100.0, budget="250", env=None):
    for d in (em._mem, em._mem_exp):
        d.clear()
    em._redis = None
    em._STATE_FILE = os.path.join(tempfile.mkdtemp(), "state.json")
    for k in ("CHECK_MAX_SECONDS", "CHECK_STAGGER_S", "CHECK_PRIORITY_MAX", "CHECK_PRIORITY_FIRST"):
        os.environ.pop(k, None)
    os.environ["CHECK_WORKERS"] = "1"
    os.environ["CHECK_MAX_SECONDS"] = budget
    for k, v in (env or {}).items():
        os.environ[k] = v
    ft = _FakeTime()
    em.time = ft
    fetched = []

    def fake_fetch(sym, force=False):
        fetched.append(sym)
        ft.now += cost
        return {"symbol": sym, "recent_news": [], "next_earnings_date": None}

    em._fetch_events = fake_fetch
    if auto:
        em.subscribe(em.SubscribeRequest(symbols=auto, source="auto"))
    if user:
        em.subscribe(em.SubscribeRequest(symbols=user, source="user"))
    return ft, fetched


def test_user_names_come_first_every_pass():
    ft, fetched = _setup(["zz"], ["a", "b", "c", "d", "e"])
    r1 = em.check_for_changes()
    assert fetched[0] == "ZZ.NS", fetched
    assert r1["checked"] == 3
    fetched.clear()
    em.check_for_changes()
    assert fetched[0] == "ZZ.NS", fetched


def test_auto_names_still_rotate_behind_the_front_group():
    ft, fetched = _setup(["zz"], ["a", "b", "c", "d", "e"])
    em.check_for_changes()                      # ZZ, A, B
    assert fetched == ["ZZ.NS", "A.NS", "B.NS"], fetched
    fetched.clear()
    em.check_for_changes()                      # ZZ, C, D  (not ZZ, A, B again)
    assert fetched == ["ZZ.NS", "C.NS", "D.NS"], fetched
    fetched.clear()
    em.check_for_changes()                      # ZZ, E, A
    assert fetched == ["ZZ.NS", "E.NS", "A.NS"], fetched


def test_only_user_names_keeps_the_plain_rotation():
    ft, fetched = _setup(["a", "b", "c", "d", "e", "f"], [])
    r = em.check_for_changes()
    assert fetched == ["A.NS", "B.NS", "C.NS"], (r, fetched)
    fetched.clear()
    em.check_for_changes()
    assert fetched == ["D.NS", "E.NS", "F.NS"], fetched


def test_priority_first_can_be_switched_off():
    ft, fetched = _setup(["zz"], ["a", "b", "c"], env={"CHECK_PRIORITY_FIRST": "0"})
    r = em.check_for_changes()
    assert fetched == ["A.NS", "B.NS", "C.NS"], (r, fetched)


def test_priority_group_is_capped():
    ft, fetched = _setup(["u1", "u2", "u3"], ["a", "b"], cost=1.0, budget="1000", env={"CHECK_PRIORITY_MAX": "2"})
    r = em.check_for_changes()
    assert r["complete"] is True and sorted(fetched) == ["A.NS", "B.NS", "U1.NS", "U2.NS", "U3.NS"], (r, fetched)


def test_stagger_setting_is_used():
    ft, fetched = _setup([], ["a", "b", "c"], cost=0.0, budget="1000", env={"CHECK_STAGGER_S": "0.25"})
    em.check_for_changes()
    assert ft.sleeps == [0.25, 0.25], ft.sleeps       # one gap between each pair of fresh fetches
    ft, fetched = _setup([], ["a", "b", "c"], cost=0.0, budget="1000")
    em.check_for_changes()
    assert ft.sleeps == [1.0, 1.0], ft.sleeps         # default unchanged


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
