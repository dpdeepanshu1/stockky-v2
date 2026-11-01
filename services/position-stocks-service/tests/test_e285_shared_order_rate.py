"""E285 round 87: the account-wide Dhan order window shared by both services (execution/shared_rate_window.py) and its use by execution/order_rate.py.
A fake clock drives both modules; the shared file lives in tmp_path. No broker, no database."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from execution import order_rate as orr
from execution import shared_rate_window as srw

ROOT = Path(__file__).resolve().parent.parent


class Clock:
    def __init__(self):
        self.t = 1000.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


@pytest.fixture(autouse=True)
def env(monkeypatch, tmp_path):
    c = Clock()
    monkeypatch.setenv("ORDER_RATE_SHARED_FILE", str(tmp_path / "win.json"))
    monkeypatch.setattr(srw, "_clock", c.now)
    monkeypatch.setattr(orr, "_clock", c.now)
    monkeypatch.setattr(orr, "_sleep", c.sleep)
    monkeypatch.setattr(config, "ORDER_RATE_LIMIT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "ORDER_RATE_SHARED_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "ORDER_RATE_PER_S", 4, raising=False)
    monkeypatch.setattr(config, "ORDER_RATE_ACCOUNT_PER_S", 8, raising=False)
    orr.reset()
    return c


def test_both_services_calls_are_counted_together_and_expire_after_a_second(env):
    assert srw.record("rt", "place_order") and srw.record("ps", "cancel_order") and srw.record("rt", "cancel_order")
    assert srw.total_last_s() == (3, {"rt": 2, "ps": 1})
    env.t += 0.999
    assert srw.total_last_s()[0] == 3
    env.t += 0.002
    assert srw.total_last_s() == (0, {})


def test_a_corrupt_or_empty_file_is_an_empty_window_and_recording_repairs_it(env, tmp_path):
    Path(srw.path()).write_text("{not json")
    assert srw.total_last_s() == (0, {})
    assert srw.record("ps", "place_order") and srw.total_last_s() == (1, {"ps": 1})
    Path(srw.path()).write_text("")
    assert srw.total_last_s() == (0, {})


def test_an_unusable_file_fails_open_everywhere(monkeypatch, tmp_path):
    blocker = tmp_path / "file_not_dir"
    blocker.write_text("x")
    monkeypatch.setenv("ORDER_RATE_SHARED_FILE", str(blocker / "sub" / "win.json"))        # a parent that is a file: nothing can be created
    assert srw.record("ps", "place_order") is False
    assert srw.total_last_s() is None
    snap = srw.snapshot()
    assert snap["working"] is False and snap["calls_last_s"] is None


def test_a_modify_waits_while_the_account_window_is_full_and_others_do_not(env):
    for _ in range(8):
        srw.record("rt", "place_order")                                    # real-trade-service just made 8 calls in this second
    assert orr.acquire("place_order", wait=False) == 0.0                   # an entry is counted, never delayed
    w = orr.acquire("modify_super_order", wait=True)
    assert 0.0 < w <= 1.1                                                  # the modify waited for the window to drain
    assert srw.total_last_s()[1].get("ps", 0) >= 1
    snap = orr.snapshot()
    assert snap["account_waits"] >= 1 and snap["shared_recorded"] == 2 and snap["calls"] == 2


def test_a_modify_does_not_wait_when_the_account_has_room(env):
    for _ in range(3):
        srw.record("rt", "place_order")
    assert orr.acquire("modify_super_order", wait=True) == 0.0
    assert orr.snapshot()["account_waits"] == 0


def test_the_wait_is_capped_even_if_the_window_never_drains(env, monkeypatch):
    monkeypatch.setattr(srw, "total_last_s", lambda now=None: (99, {"rt": 99}))        # a window that always looks full
    w = orr.acquire("modify_super_order", wait=True)
    assert w <= orr.MAX_WAIT_S + 1e-9
    assert orr.snapshot()["forced"] == 1                                                  # let through after the cap: slowed, never starved


def test_switch_off_writes_no_file_and_does_not_wait_for_the_other_service(env, monkeypatch):
    for _ in range(9):
        srw.record("rt", "place_order")
    monkeypatch.setattr(config, "ORDER_RATE_SHARED_ENABLED", False, raising=False)
    assert orr.acquire("modify_super_order", wait=True) == 0.0                           # this process's own window is empty
    assert srw.total_last_s()[1].get("ps", 0) == 0                                        # nothing appended
    assert orr.snapshot()["shared_enabled"] is False


def test_an_unusable_file_falls_back_to_the_per_process_window(env, monkeypatch, tmp_path):
    blocker = tmp_path / "f"
    blocker.write_text("x")
    monkeypatch.setenv("ORDER_RATE_SHARED_FILE", str(blocker / "x" / "w.json"))
    got = [orr.acquire("modify_super_order", wait=True) for _ in range(5)]
    assert got[:4] == [0.0] * 4 and 0.0 < got[4] <= 1.0                                   # the E282 budget of 4 still holds
    assert orr.snapshot()["shared_unusable"] >= 5


def test_account_limit_is_clamped(monkeypatch):
    for raw, want in ((8, 8), (1, 2), (50, 10), ("junk", 8)):
        monkeypatch.setattr(config, "ORDER_RATE_ACCOUNT_PER_S", raw, raising=False)
        assert orr.account_per_s() == want


def test_the_window_file_never_grows_past_its_cap(env):
    for _ in range(srw.MAX_ENTRIES + 50):
        srw.record("rt", "place_order")
    assert len(json.loads(Path(srw.path()).read_text())) <= srw.MAX_ENTRIES


def test_the_two_services_ship_the_same_copy_and_the_catalog_has_the_switches():
    a = (ROOT / "execution" / "shared_rate_window.py").read_bytes()
    b = (ROOT.parent / "real-trade-service" / "execution" / "shared_rate_window.py").read_bytes()
    assert a == b
    from control.catalog import CATALOG
    by = {f.key: f for f in CATALOG}
    assert by["ORDER_RATE_SHARED_ENABLED"].shipped == "true" and by["ORDER_RATE_ACCOUNT_PER_S"].shipped == "8"
