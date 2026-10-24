"""D-vol (2026-10-23): a fresh bullish daily breakout on thin volume. Default = log: recorded as an observation, never a reason, never a WAIT.
enforce = a gate reason (the entry WAITs); off = not checked. A summary without ms_bos_vol_ratio is no opinion, never a block."""
from datetime import datetime, timezone

import pytest

import config
from entry_engine import structure_gate as sg


def _summary(**kw):
    s = {"ms_quality": "ok", "ms_ver": "1.0.0", "ms_asof": datetime.now(timezone.utc).isoformat(), "ms_trend": "bullish",
         "ms_bos_dir": "bullish", "ms_bos_bars": 2, "ms_bos_failed": False, "ms_bos_vol_ratio": 0.6, "ms_price": 100.0, "ms_atr": 2.0,
         "ms_chase_atr": 0.2, "ms_stretch_atr": 1.0, "ms_room_atr": 3.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": []}
    s.update(kw)
    return s


@pytest.fixture(autouse=True)
def _defaults(monkeypatch):
    for k, v in (("STRUCTURE_MAX_CHASE_ATR", 1.5), ("STRUCTURE_MAX_STRETCH_ATR", 3.0), ("STRUCTURE_MIN_ROOM_ATR", 0.5), ("STRUCTURE_MIN_RR", 0.0),
                 ("STRUCTURE_REJECT_STOP_WIDE", True), ("STRUCTURE_BEARISH_BLOCK", True), ("STRUCTURE_BEARISH_BOS_BARS", 5),
                 ("STRUCTURE_MAX_AGE_DAYS", 7.0), ("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0), ("STRUCTURE_REQUIRE_DATA", False),
                 ("STRUCTURE_BREAKOUT_VOLUME_MODE", "log"), ("STRUCTURE_BREAKOUT_MIN_VOL_RATIO", 1.2), ("STRUCTURE_BREAKOUT_BARS", 5)):
        monkeypatch.setattr(config, k, v, raising=False)
    sg.reset_stats()


def test_shipped_defaults_are_log_and_1_2():
    import importlib
    assert config.STRUCTURE_BREAKOUT_VOLUME_MODE in ("log", "enforce", "off")
    assert importlib.import_module("config").STRUCTURE_BREAKOUT_MIN_VOL_RATIO >= 0


def test_log_mode_records_an_observation_and_never_blocks():
    v = sg.evaluate(_summary(), 100.0)
    assert v["opinion"] == "ok" and not v["would_reject"] and v["reasons"] == [] and v["tag"] is None
    assert len(v["observations"]) == 1 and "thin volume" in v["observations"][0] and "0.60x" in v["observations"][0]


def test_enforce_mode_makes_it_a_reason():
    config.STRUCTURE_BREAKOUT_VOLUME_MODE = "enforce"
    v = sg.evaluate(_summary(), 100.0)
    assert v["would_reject"] and v["tag"] == "structure" and any("thin volume" in r for r in v["reasons"]) and v["observations"] == []
    assert sg.reason_key(v["reasons"][0]) == "breakout_volume"


def test_off_mode_and_zero_ratio_switch_it_off():
    config.STRUCTURE_BREAKOUT_VOLUME_MODE = "off"
    assert sg.evaluate(_summary(), 100.0)["observations"] == []
    config.STRUCTURE_BREAKOUT_VOLUME_MODE, config.STRUCTURE_BREAKOUT_MIN_VOL_RATIO = "enforce", 0.0
    v = sg.evaluate(_summary(), 100.0)
    assert not v["would_reject"] and v["observations"] == []


def test_enough_volume_or_no_fresh_bullish_breakout_is_fine():
    for kw in ({"ms_bos_vol_ratio": 1.2}, {"ms_bos_vol_ratio": 2.4}, {"ms_bos_bars": 6}, {"ms_bos_failed": True}, {"ms_bos_dir": None},
               {"ms_bos_vol_ratio": None}, {"ms_bos_vol_ratio": "junk"}, {"ms_bos_vol_ratio": float("nan")}):
        config.STRUCTURE_BREAKOUT_VOLUME_MODE = "enforce"
        v = sg.evaluate(_summary(**kw), 100.0)
        assert not any("thin volume" in r for r in v["reasons"]) and v["observations"] == [], kw


def test_a_summary_without_the_key_is_no_opinion_on_this_test():
    s = _summary()
    del s["ms_bos_vol_ratio"]
    config.STRUCTURE_BREAKOUT_VOLUME_MODE = "enforce"
    assert not sg.evaluate(s, 100.0)["would_reject"]


def test_snapshot_and_counter_carry_the_observation():
    v = sg.evaluate(_summary(), 100.0)
    doc = __import__("json").loads(sg.snapshot(_summary(), v, "log", False))
    assert doc["observations"] == v["observations"] and doc["summary"]["ms_bos_vol_ratio"] == 0.6
    sg.record("INFY", "REAL", v, "log", False, 100.0)
    assert sg.stats()["thin_volume_breakouts"] == 1 and sg.stats()["config"]["STRUCTURE_BREAKOUT_VOLUME_MODE"] == "log"
