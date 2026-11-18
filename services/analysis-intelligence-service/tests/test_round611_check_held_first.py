"""Round 611: `/check?first=A,B` puts the named (held) symbols in front of every other name, before the user group too.

Plain runner: `python tests/test_round611_check_held_first.py` (also collected by pytest)."""
from __future__ import annotations

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import test_round603_check_priority as t603  # noqa: E402  (same fake clock / fake fetch)

em = t603.em
_setup = t603._setup


def _more_env():
    for k in ("CHECK_HELD_FIRST", "CHECK_HELD_MAX"):
        os.environ.pop(k, None)


def test_held_names_are_checked_before_user_and_auto_names():
    _more_env()
    ft, fetched = _setup(["zz"], ["a", "b", "c", "d", "e"])
    r = em.check_for_changes(first="d,e")
    assert fetched[:3] == ["D.NS", "E.NS", "ZZ.NS"], fetched
    assert r["held_first"] == 2 and r["checked"] == 3


def test_held_names_do_not_move_the_rotating_cursor():
    _more_env()
    ft, fetched = _setup(["zz"], ["a", "b", "c", "d", "e"])
    em.check_for_changes(first="e")             # E, ZZ, A
    assert fetched == ["E.NS", "ZZ.NS", "A.NS"], fetched
    fetched.clear()
    em.check_for_changes(first="e")             # E, ZZ, B  (E is not part of the rotation)
    assert fetched == ["E.NS", "ZZ.NS", "B.NS"], fetched


def test_unknown_and_repeated_names_are_ignored():
    _more_env()
    ft, fetched = _setup([], ["a", "b", "c"])
    r = em.check_for_changes(first="nope, B ,b,,c.ns")
    assert fetched[:2] == ["B.NS", "C.NS"], fetched
    assert r["held_first"] == 2


def test_without_the_parameter_nothing_changes():
    _more_env()
    ft, fetched = _setup(["zz"], ["a", "b", "c", "d", "e"])
    r = em.check_for_changes()
    assert fetched == ["ZZ.NS", "A.NS", "B.NS"], fetched
    assert r["held_first"] == 0


def test_switch_off_and_cap():
    _more_env()
    ft, fetched = _setup([], ["a", "b", "c", "d"], env={"CHECK_HELD_FIRST": "0"})
    em.check_for_changes(first="d")
    assert fetched[0] == "A.NS", fetched
    _more_env()
    ft, fetched = _setup([], ["a", "b", "c", "d"], cost=1.0, budget="1000", env={"CHECK_HELD_MAX": "1"})
    r = em.check_for_changes(first="d,c")
    assert fetched[0] == "D.NS" and r["held_first"] == 1, (fetched, r)


def test_every_symbol_held_keeps_everything_checked():
    _more_env()
    ft, fetched = _setup([], ["a", "b"], cost=1.0, budget="1000")
    r = em.check_for_changes(first="a,b")
    assert sorted(fetched) == ["A.NS", "B.NS"] and r["complete"] is True, (fetched, r)


if __name__ == "__main__":
    n = 0
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            n += 1
            print("ok", name)
    print(n, "passed")
