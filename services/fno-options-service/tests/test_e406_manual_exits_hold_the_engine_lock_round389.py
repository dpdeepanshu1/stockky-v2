"""Round 389 (E406): close_position and squareoff_all hold the engine lock again (round 387 moved the decorator onto is_paused); is_paused stays lock-free so /fno/overview never waits behind a slow SELL."""
from __future__ import annotations

import threading
import time
from datetime import timedelta

from fno import paper
from test_engine_paper import NOW
from test_real_partial import RB, db, real, entered, sells  # noqa: F401  (db is a fixture)
from test_round283_engine_lock import SlowSell


def _is_locked(fn) -> bool:
    return hasattr(fn, "__wrapped__")                      # _locked uses functools.wraps


def test_every_exit_entry_point_is_locked_and_is_paused_is_not():
    from fno.engine import Engine
    for name in ("tick", "squareoff_all", "close_position", "clear_exit_doubt", "clear_stop_doubt"):
        assert _is_locked(getattr(Engine, name)), name
    assert not _is_locked(Engine.is_paused)


def test_manual_close_of_one_position_during_a_running_exit_sends_no_second_sell(db):
    b = SlowSell()
    e = real(db, b)
    p = entered(db, b, e)
    first = threading.Thread(target=lambda: e.squareoff_all(now=NOW + timedelta(seconds=30)))
    first.start()
    assert b.started.wait(3.0)
    second = threading.Thread(target=lambda: e.close_position(p.id, now=NOW + timedelta(seconds=31)))
    second.start()
    time.sleep(0.3)
    n_during = len(sells(b))
    b.release.set()
    first.join(5.0)
    second.join(5.0)
    assert n_during <= 1 and len(sells(b)) == 1
    assert db.get_position(p.id).status == "CLOSED" and db.get_position(p.id).exit_reason == paper.MANUAL


def test_is_paused_answers_while_an_exit_is_in_flight(db):
    b = SlowSell()
    e = real(db, b)
    p = entered(db, b, e)
    t = threading.Thread(target=lambda: e.squareoff_all(now=NOW + timedelta(seconds=30)))
    t.start()
    assert b.started.wait(3.0)
    got = []
    r = threading.Thread(target=lambda: got.append(e.is_paused()))
    r.start()
    r.join(1.0)
    assert got == [False]                                  # not blocked by the running exit
    b.release.set()
    t.join(5.0)
