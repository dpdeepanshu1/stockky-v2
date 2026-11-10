"""Round 283: the runner's tick() and a manual square-off (request thread) never run an exit at the same time: one SELL, not two. Nothing talks to Dhan."""
from __future__ import annotations

import threading
import time
from datetime import timedelta

from fno import paper
from test_engine_paper import NOW
from test_real_partial import RB, db, real, entered, sells  # noqa: F401  (db is a fixture)


class SlowSell(RB):
    """The first LIMIT SELL call signals `started` and then waits for `release`, like a slow Dhan answer."""
    def __init__(self, net=None):
        super().__init__(net=net)
        self.started, self.release = threading.Event(), threading.Event()

    def place(self, payload):
        if payload["transactionType"] == "SELL" and payload["orderType"] == "LIMIT" and not self.started.is_set():
            self.started.set()
            self.release.wait(5.0)
        return super().place(payload)


def test_a_manual_square_off_during_a_running_exit_waits_and_sends_no_second_sell(db):
    b = SlowSell()
    e = real(db, b)
    p = entered(db, b, e)
    first = threading.Thread(target=lambda: e.squareoff_all(now=NOW + timedelta(seconds=30)))
    first.start()
    assert b.started.wait(3.0)                                                  # the first SELL is on its way
    second = threading.Thread(target=lambda: e.squareoff_all(now=NOW + timedelta(seconds=31)))
    second.start()
    time.sleep(0.3)                                                             # without the lock the second thread would have sent its SELL by now
    n_during = len([o for o in b.sent if o["transactionType"] == "SELL" and o["orderType"] == "LIMIT"])
    b.release.set()
    first.join(5.0)
    second.join(5.0)
    assert n_during <= 1 and len(sells(b)) == 1                                 # exactly one SELL in the end
    assert db.get_position(p.id).status == "CLOSED" and db.get_position(p.id).exit_reason == paper.MANUAL


def test_the_lock_is_re_entrant_and_tick_still_works(db):
    b = RB()
    e = real(db, b)
    p = entered(db, b, e)
    e.tick(NOW + timedelta(seconds=5))                                          # a normal cycle after the entry: no deadlock
    assert db.get_position(p.id) is not None and e._lock.acquire(blocking=False) and e._lock.acquire(blocking=False)
