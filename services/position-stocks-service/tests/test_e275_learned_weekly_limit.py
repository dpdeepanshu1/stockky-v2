"""E275 round 39: a learned exit number changes by at most 25 % a week (orders/learned_limit.py) and both consumers use it."""
import json
import pathlib
from types import SimpleNamespace

import pytest

import config
from orders import exit_adaptive as ea
from orders import learned_limit as ll
from orders import momentum_hook as mh
from orders import slot_ladder

DAY = 24 * 3600.0
T0 = 1_800_000_000.0
POS = SimpleNamespace(flow_json=json.dumps({"setup": {"id": "S1_CATALYST_UP", "slot": "0930"}}))


@pytest.fixture(autouse=True)
def limit_on(monkeypatch):
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_PCT", 25.0, raising=False)


def test_clamp_step_holds_a_value_within_the_band():
    assert ll.clamp_step(20.0, 5.0, 0.25) == 15.0
    assert ll.clamp_step(20.0, 90.0, 0.25) == 25.0
    assert ll.clamp_step(20.0, 22.0, 0.25) == 22.0
    assert ll.clamp_step(0.0, 7.0, 0.25) == 7.0                       # no base for a percentage: the value passes


def test_first_week_starts_from_the_old_constant():
    v, hit = ll.limit("k", 5.6, start=20.0, now_ts=T0)
    assert (v, hit) == (15.0, True)
    v, hit = ll.limit("k", 22.0, start=20.0, now_ts=T0 + DAY)
    assert (v, hit) == (22.0, False)


def test_the_anchor_moves_after_a_week_so_a_real_change_is_followed():
    assert ll.limit("k", 5.0, start=20.0, now_ts=T0)[0] == 15.0         # week 1: 20 -> 15 at most
    assert ll.limit("k", 5.0, start=20.0, now_ts=T0 + 6 * DAY)[0] == 15.0
    v, hit = ll.limit("k", 5.0, start=20.0, now_ts=T0 + 7 * DAY)          # week 2: anchor 15 -> 11.25
    assert (v, hit) == (11.25, True)
    assert ll.limit("k", 5.0, start=20.0, now_ts=T0 + 14 * DAY)[0] == 8.4375


def test_without_a_constant_the_first_value_is_the_anchor():
    assert ll.limit("g", 1.2, start=None, now_ts=T0) == (1.2, False)
    assert ll.limit("g", 3.0, start=None, now_ts=T0 + DAY) == (1.5, True)


def test_switch_off_and_missing_values_pass_unchanged(monkeypatch):
    assert ll.limit("k", None, 20.0, T0) == (None, False)
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_ENABLED", False, raising=False)
    assert ll.limit("k", 1.0, 20.0, T0) == (1.0, False)
    assert ll.snapshot() == {}


def test_limit_pct_is_bounded(monkeypatch):
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_PCT", 0.0, raising=False)
    assert ll.limit_frac() == 0.01
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_PCT", 500.0, raising=False)
    assert ll.limit_frac() == 1.0
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_PCT", "abc", raising=False)
    assert ll.limit_frac() == 0.25


def test_anchors_survive_a_restart(monkeypatch):
    ll.limit("k", 5.0, 20.0, T0)
    ll.flush()
    data = json.loads(pathlib.Path(config.LEARNED_LIMIT_FILE).read_text())
    assert data["anchors"]["k"]["anchor"] == 20.0
    ll.reset()                                                           # a new process
    assert ll.limit("k", 5.0, 99.0, T0 + DAY)[0] == 15.0                 # the stored anchor 20 wins over the new start


def test_an_unreadable_file_starts_from_the_constants():
    pathlib.Path(config.LEARNED_LIMIT_FILE).write_text("{not json")
    assert ll.limit("k", 5.0, 20.0, T0)[0] == 15.0


def test_a_failing_write_never_raises(monkeypatch):
    monkeypatch.setattr(config, "LEARNED_LIMIT_FILE", "/proc/nope/x.json", raising=False)
    assert ll.limit("k", 5.0, 20.0, T0)[0] == 15.0
    ll.flush()


def test_exit_numbers_only_limit_learned_parts():
    fb = {"ttp_min": 20.0, "typical_gain_pct": 2.0, "ttp_source": "fallback", "gain_source": "fallback"}
    assert ll.limit_exit_numbers("c", "time_stop", fb, 20.0, T0) == fb
    num = {"ttp_min": 5.0, "typical_gain_pct": 0.8, "ttp_source": "learned n=30 w=0.50", "gain_source": "learned n=30 w=0.50"}
    out = ll.limit_exit_numbers("c", "time_stop", num, 20.0, T0)
    assert out["ttp_min"] == 15.0 and out["ttp_source"].endswith("capped 25%/wk")
    assert out["typical_gain_pct"] == 0.8 and out["gain_source"] == "learned n=30 w=0.50"     # first gain seen = its own anchor
    assert num["ttp_min"] == 5.0                                           # the input is not changed


def test_consumers_keep_separate_anchors():
    num = {"ttp_min": 5.0, "typical_gain_pct": None, "ttp_source": "learned n=30 w=0.50", "gain_source": "fallback"}
    ll.limit_exit_numbers("c", "time_stop", num, 20.0, T0)
    out = ll.limit_exit_numbers("c", "momentum", num, 10.0, T0)            # momentum's own constant is 10 -> floor 7.5
    assert out["ttp_min"] == 7.5


def _cell(ttp, ttp_n=270, n=300):
    return {"n": n, "ttp_n": ttp_n, "ttp_min": ttp, "gain_n": 0, "typical_gain_pct": None}


def test_time_stop_follows_the_limited_time_to_peak(monkeypatch):
    monkeypatch.setattr(config, "TIME_STOP_LEARNED_ENABLED", True, raising=False)
    monkeypatch.setattr(slot_ladder, "cells", lambda db: {"S1_CATALYST_UP|0930": _cell(4.0)})
    r = ea.learned_time_stop(None, POS)          # unlimited ttp 5.6 -> limited to 15 -> nft 20 (15 + 5), stag 30, hard 60
    assert (r["nft_min"], r["stag_min"], r["hard_max_min"]) == (20.0, 30.0, 60.0)
    assert "capped 25%/wk" in r["source"]


def test_momentum_numbers_follow_the_limited_values(monkeypatch):
    monkeypatch.setattr(config, "MOMENTUM_LEARNED_ENABLED", True, raising=False)
    monkeypatch.setattr(config, "MOMENTUM_NO_NEW_BEST_MIN", 20.0, raising=False)
    monkeypatch.setattr(slot_ladder, "cells", lambda db: {"S1_CATALYST_UP|0930": _cell(4.0)})
    out = mh.learned_numbers(None, POS, 2.0)
    assert out["ttp_min"] == 15.0 and "capped" in out["ttp_source"]


def test_switch_off_gives_the_unlimited_learned_value(monkeypatch):
    monkeypatch.setattr(config, "LEARNED_WEEKLY_LIMIT_ENABLED", False, raising=False)
    monkeypatch.setattr(config, "TIME_STOP_LEARNED_ENABLED", True, raising=False)
    monkeypatch.setattr(slot_ladder, "cells", lambda db: {"S1_CATALYST_UP|0930": _cell(4.0)})
    assert ea.learned_time_stop(None, POS)["nft_min"] == 10.6
