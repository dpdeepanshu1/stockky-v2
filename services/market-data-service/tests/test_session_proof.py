"""group319: price-based proof that the exchange traded today (no move 09:15-10:00 IST = closed)."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import session_proof as sp

IST = ZoneInfo("Asia/Kolkata")


def at(h, m, s=0, day=12):  # Monday 2026-10-12
    return datetime(2026, 10, day, h, m, s, tzinfo=IST)


@pytest.fixture(autouse=True)
def _clean():
    sp.reset_state()
    yield
    sp.reset_state()


def _feed(n, price_fn, when):
    for i in range(n):
        sp.observe(f"SYM{i}", price_fn(i), when)


def test_frozen_prices_close_the_day_after_ten():
    _feed(20, lambda i: 100 + i, at(9, 16))
    _feed(20, lambda i: 100 + i, at(9, 45))
    assert sp.status(at(9, 59))["status"] == "pending"
    st = sp.status(at(10, 0))
    assert st["status"] == "closed" and st["closed"] is True
    assert sp.is_closed_today(at(11, 0))


def test_any_move_inside_window_proves_open():
    _feed(20, lambda i: 100 + i, at(9, 16))
    sp.observe("SYM3", 103.05, at(9, 40))
    assert sp.status(at(10, 5))["status"] == "open"
    assert not sp.is_closed_today(at(10, 5))


def test_too_few_symbols_is_unknown_not_closed():
    _feed(3, lambda i: 100 + i, at(9, 16))
    st = sp.status(at(10, 5))
    assert st["status"] == "unknown" and st["closed"] is False


def test_nifty_alone_is_enough_to_close_and_to_prove_open():
    sp.observe("NIFTY", 25000.0, at(9, 16))
    sp.observe("NIFTY", 25000.0, at(9, 33))
    sp.observe("NIFTY", 25000.0, at(9, 50))
    assert sp.status(at(10, 1))["closed"] is True
    sp.reset_state()
    sp.observe("NIFTY 50", 25000.0, at(9, 16))
    sp.observe("NIFTY 50", 25010.5, at(9, 50))
    st = sp.status(at(10, 1))
    assert st["status"] == "open" and st["nifty_moved"] is True


def test_closed_verdict_self_heals_on_first_real_move():
    _feed(20, lambda i: 100 + i, at(9, 16))
    assert sp.is_closed_today(at(10, 10))
    sp.observe("SYM1", 111.0, at(10, 20))   # first symbol seen in the window moves later
    assert not sp.is_closed_today(at(10, 21))


def test_late_start_feed_never_closes_the_day():
    # nothing observed in the window (feed started at 10:30) -> unknown, and ticks after the window are no baseline
    _feed(20, lambda i: 100 + i, at(10, 30))
    st = sp.status(at(10, 31))
    assert st["status"] == "unknown" and st["closed"] is False


def test_late_restart_proves_open_from_a_later_move():
    # group446: service restarted at 12:46, nothing recorded 09:15-10:00. The first price after the restart is the baseline;
    # a later DIFFERENT price is a real move and proves the exchange is open (the 10:15 live check used to FAIL all day).
    _feed(20, lambda i: 100 + i, at(12, 46))
    assert sp.status(at(12, 47))["status"] == "unknown"
    sp.observe("SYM4", 104.0, at(12, 50))                     # same price again: not a move
    assert sp.status(at(12, 51))["status"] == "unknown"
    sp.observe("SYM4", 104.35, at(12, 55))
    st = sp.status(at(12, 56))
    assert st["status"] == "open" and st["closed"] is False and st["late_baseline_symbols"] == 20


def test_late_restart_can_never_prove_closed_and_stops_at_the_close():
    for i in range(20):
        sp.observe(f"SYM{i}", 100 + i, at(12, 46))
        sp.observe(f"SYM{i}", 100 + i, at(13, 30))             # frozen prices after a late restart: still unknown, never closed
    st = sp.status(at(14, 0))
    assert st["status"] == "unknown" and st["closed"] is False
    sp.reset_state()
    sp.observe("SYM1", 100.0, at(16, 0))                       # after 15:30 the late baseline is not taken at all
    sp.observe("SYM1", 101.0, at(16, 5))
    assert sp.status(at(16, 6))["status"] == "unknown"


def test_in_window_ticks_still_win_over_the_late_baseline():
    _feed(20, lambda i: 100 + i, at(9, 16))
    sp.observe("SYM3", 103.0, at(11, 0))                      # unchanged vs the in-window price: no move
    assert sp.status(at(11, 1))["closed"] is True


def test_ticks_before_the_bell_are_ignored():
    _feed(20, lambda i: 100 + i, at(9, 10))
    assert sp.status(at(10, 5))["status"] == "unknown"


def test_weekend_and_bad_prices_are_ignored():
    _feed(20, lambda i: 100 + i, at(9, 20, day=11))  # Sunday 2026-10-11
    assert sp.status(at(10, 5, day=11))["closed"] is False
    for bad in (None, "x", 0, -1, float("nan"), float("inf")):
        sp.observe("BAD", bad, at(9, 20))
    assert sp.status(at(9, 30))["observed_symbols"] == 0


def test_new_day_resets_state():
    _feed(20, lambda i: 100 + i, at(9, 16))
    assert sp.is_closed_today(at(10, 5))
    assert sp.status(at(9, 20, day=13))["status"] == "pending"


def test_disabled_never_closes(monkeypatch):
    monkeypatch.setattr(sp, "ENABLED", False)
    _feed(20, lambda i: 100 + i, at(9, 16))
    assert sp.status(at(10, 5)) == {"status": "disabled", "closed": False}


def test_observe_never_raises():
    sp.observe(None, None, "not-a-datetime")


# ---- group320: Nifty feed -------------------------------------------------------------------------------------------

def test_one_or_two_unchanged_nifty_readings_never_close_the_day():
    sp.observe("NIFTY", 25000.0, at(9, 16))
    assert sp.status(at(10, 1))["status"] == "unknown"
    sp.observe("NIFTY", 25000.0, at(9, 50))
    st = sp.status(at(10, 1))
    assert st["status"] == "unknown" and st["closed"] is False and st["nifty_readings"] == 2


def test_nifty_readings_after_the_window_do_not_count_toward_closing():
    sp.observe("NIFTY", 25000.0, at(9, 16))
    sp.observe("NIFTY", 25000.0, at(10, 5))
    sp.observe("NIFTY", 25000.0, at(10, 6))
    assert sp.status(at(10, 7))["closed"] is False


class _Fetcher:
    def __init__(self, prices):
        self.prices, self.calls = list(prices), 0

    def __call__(self):
        self.calls += 1
        return self.prices.pop(0) if self.prices else self.prices_last

    prices_last = 25000.0


@pytest.fixture
def fetcher():
    sp.reset_nifty_probe()
    f = _Fetcher([])
    sp.register_nifty_fetcher(f)
    yield f
    sp.register_nifty_fetcher(None)
    sp.reset_nifty_probe()


def test_probe_nifty_feeds_observe_and_is_rate_limited(fetcher):
    assert sp.probe_nifty(at(9, 20), mono=1000.0) is True
    assert sp.probe_nifty(at(9, 20, 30), mono=1030.0) is False          # < NIFTY_PROBE_S since the last one
    assert fetcher.calls == 1
    assert sp.probe_nifty(at(9, 21), mono=1000.0 + sp.NIFTY_PROBE_S) is True
    assert fetcher.calls == 2
    assert sp.status(at(9, 22))["nifty_readings"] == 2


def test_probe_nifty_three_flat_readings_close_the_day_and_a_move_reopens_it(fetcher):
    for i, (h, m) in enumerate([(9, 16), (9, 40), (9, 58)]):
        assert sp.probe_nifty(at(h, m), mono=1000.0 + 100 * i) is True
    assert sp.status(at(10, 1))["closed"] is True
    fetcher.prices = [25040.0]
    assert sp.probe_nifty(at(10, 30), mono=2000.0) is True            # still probed while closed
    st = sp.status(at(10, 31))
    assert st["status"] == "open" and st["nifty_moved"] is True


def test_probe_nifty_does_nothing_when_it_cannot_matter(fetcher):
    assert sp.probe_nifty(at(9, 10), mono=1.0) is False                    # before the bell
    assert sp.probe_nifty(at(10, 30, day=17), mono=2.0) is False           # Saturday 2026-10-17
    sp.observe("SYM1", 100.0, at(9, 16))
    sp.observe("SYM1", 100.5, at(9, 30))                                   # proven open by a stock move
    assert sp.probe_nifty(at(9, 40), mono=3.0) is False
    assert fetcher.calls == 0


def test_probe_nifty_ignores_bad_or_failing_fetchers():
    sp.reset_nifty_probe()
    sp.register_nifty_fetcher(lambda: None)
    assert sp.probe_nifty(at(9, 20), mono=1.0) is False
    sp.reset_nifty_probe()
    sp.register_nifty_fetcher(lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert sp.probe_nifty(at(9, 20), mono=1.0) is False
    sp.register_nifty_fetcher(None)
    assert sp.probe_nifty(at(9, 20), mono=1.0) is False
    sp.reset_nifty_probe()
