"""E282 round 77: the per-second Dhan order budget (execution/order_rate.py). A fake clock: nothing really sleeps."""
import re
from pathlib import Path

import pytest

import config
from execution import order_rate as orr

ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    c = Clock()
    monkeypatch.setattr(orr, "_clock", c.now)
    monkeypatch.setattr(orr, "_sleep", c.sleep)
    monkeypatch.setattr(config, "ORDER_RATE_LIMIT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "ORDER_RATE_PER_S", 4, raising=False)
    orr.reset()
    return c


def _admit_times(clock, n, kind="modify_super_order", wait=True):
    out = []
    for _ in range(n):
        w = orr.acquire(kind, wait=wait)
        out.append((clock.t, w))
    return out


def test_calls_under_the_budget_never_wait(fresh):
    got = _admit_times(fresh, 4)
    assert all(w == 0.0 for _, w in got) and orr.snapshot()["calls"] == 4


def test_the_fifth_modify_in_a_second_waits_for_the_window(fresh):
    got = _admit_times(fresh, 5)
    assert got[3][1] == 0.0 and 0.0 < got[4][1] <= 1.0
    assert got[4][0] - got[0][0] >= 1.0 - 1e-9                          # the 5th is admitted a full second after the 1st


def test_worst_case_twenty_modifies_at_once_never_exceed_the_budget_in_any_second(fresh):
    """10 open positions x (stop + target) all modified in the same instant: spread out, at most 4 admitted in any sliding second, nobody waits more than a second."""
    got = _admit_times(fresh, 20)
    times = [t for t, _ in got]
    for i, t in enumerate(times):
        assert sum(1 for u in times if t - 1.0 < u <= t) <= 4, f"more than 4 calls in the second ending at call {i}"
    assert max(w for _, w in got) <= 1.0 + 1e-9
    assert abs((times[-1] - times[0]) - 4.0) < 1e-6                     # 20 calls at 4 a second: admitted at 0, 1, 2, 3 and 4 s, four each
    snap = orr.snapshot()
    assert snap["peak_in_window"] <= 4 and snap["forced"] == 0 and snap["waits"] == 4 and abs(snap["waited_s"] - 4.0) < 1e-6


def test_other_order_calls_are_counted_but_never_delayed(fresh):
    got = _admit_times(fresh, 8, kind="place_order", wait=False)        # entries / exits / cancels burst of 8 in one instant
    assert all(w == 0.0 for _, w in got) and fresh.t == 1000.0
    assert orr.snapshot()["peak_in_window"] == 8 and orr.snapshot()["by_kind"] == {"place_order": 8}


def test_a_modify_yields_to_counted_calls(fresh):
    _admit_times(fresh, 4, kind="cancel_order", wait=False)             # an exit burst uses the whole budget
    w = orr.acquire("modify_super_order", wait=True)
    assert 0.0 < w <= 1.0


def test_the_wait_is_capped_and_a_stuck_window_lets_the_call_through(monkeypatch, fresh):
    monkeypatch.setattr(orr, "WINDOW_S", 100.0)                         # a window that never frees: only the hard cap can release the call
    _admit_times(fresh, 4, kind="cancel_order", wait=False)
    w = orr.acquire("modify_super_order", wait=True)
    assert abs(w - orr.MAX_WAIT_S) < 1e-9 and orr.snapshot()["forced"] == 1


def test_switch_off_counts_but_never_waits(monkeypatch, fresh):
    monkeypatch.setattr(config, "ORDER_RATE_LIMIT_ENABLED", False, raising=False)
    got = _admit_times(fresh, 10)
    assert all(w == 0.0 for _, w in got) and fresh.t == 1000.0 and orr.snapshot()["calls"] == 10


def test_budget_is_read_from_config_and_bounded(monkeypatch, fresh):
    monkeypatch.setattr(config, "ORDER_RATE_PER_S", 2, raising=False)
    assert orr.per_second() == 2
    monkeypatch.setattr(config, "ORDER_RATE_PER_S", 50, raising=False)
    assert orr.per_second() == 9                                         # never the whole account limit
    monkeypatch.setattr(config, "ORDER_RATE_PER_S", "junk", raising=False)
    assert orr.per_second() == 4


def test_a_broken_clock_fails_open(monkeypatch, fresh):
    def boom():
        raise RuntimeError("clock")
    monkeypatch.setattr(orr, "_clock", boom)
    assert orr.acquire("modify_super_order", wait=True) == 0.0


def test_every_dhan_order_call_in_the_client_is_counted_and_only_modify_waits():
    """Read-only calls (get_*) are the data API and not counted. Every order call is preceded by an order_rate.acquire, except the dhanhq <2.1 `tag` retry, which is the same order."""
    src = (ROOT / "execution" / "dhan_client.py").read_text(encoding="utf-8")
    lines = src.split("\n")
    order_call = re.compile(r"^\s*resp = (?:client\.(?:place_order|cancel_order|place_super_order|modify_super_order|cancel_super_order)\(|dhan_http\.post\()")
    sites = [i for i, l in enumerate(lines) if order_call.match(l)]
    assert len(sites) == 11, len(sites)
    uncounted = [i + 1 for i in sites if "order_rate.acquire(" not in lines[i - 1]]
    assert len(uncounted) == 1 and "place_super_order" in lines[uncounted[0] - 1], uncounted     # only the `tag` retry
    waits = re.findall(r"order_rate\.acquire\([^)]*wait=True", src)
    assert len(waits) == 1 and 'acquire("modify_super_order", wait=True)' in src


def test_the_switches_are_on_the_calibration_page_and_protected():
    cat = (ROOT / "control" / "catalog.py").read_text(encoding="utf-8")
    auto = (ROOT.parent / "calibration-runner-service" / "autocalib.py").read_text(encoding="utf-8")
    cfg = (ROOT / "config.py").read_text(encoding="utf-8")
    for k in ("ORDER_RATE_LIMIT_ENABLED", "ORDER_RATE_PER_S"):
        assert f'Flag("{k}"' in cat and f'"{k}":' in auto and re.search(rf"^{k} = ", cfg, re.M)
