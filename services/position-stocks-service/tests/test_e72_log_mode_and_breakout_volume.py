"""E72: Position Stocks structure gate gets a `log` mode (evaluate + record, never refuse; default stays enforce) and the D-vol breakout-volume
OBSERVATION (log|off only - never a refusal reason). A shared STRUCTURE_GATE_MODE=log (Real Trade's switch) must NOT loosen the scalper."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import config
from orders import structure_gate as sg

ROOT = Path(__file__).resolve().parent.parent


def _sum(**kw):
    d = {"ms_quality": "ok", "ms_asof": datetime.now(timezone.utc).isoformat(), "ms_price": 100.0, "ms_atr": 2.0, "ms_trend": "bullish",
         "ms_bos_dir": "bullish", "ms_bos_bars": 2, "ms_bos_failed": False, "ms_bos_vol_ratio": 0.6,
         "ms_chase_atr": 0.5, "ms_stretch_atr": 1.0, "ms_room_atr": 3.0}
    d.update(kw)
    return d


@pytest.fixture(autouse=True)
def _cfg(monkeypatch):
    for k, v in (("STRUCTURE_GATE_MODE", "enforce"), ("STRUCTURE_REQUIRE_DATA", False), ("STRUCTURE_BEARISH_BLOCK", True),
                 ("STRUCTURE_BEARISH_BOS_BARS", 5), ("STRUCTURE_MAX_CHASE_ATR", 4.0), ("STRUCTURE_MAX_STRETCH_ATR", 5.0),
                 ("STRUCTURE_MIN_ROOM_ATR", 0.3), ("STRUCTURE_MAX_AGE_DAYS", 7.0), ("STRUCTURE_MAX_PRICE_DEV_PCT", 15.0),
                 ("STRUCTURE_BREAKOUT_VOLUME_MODE", "log"), ("STRUCTURE_BREAKOUT_MIN_VOL_RATIO", 1.2), ("STRUCTURE_BREAKOUT_BARS", 5)):
        monkeypatch.setattr(config, k, v, raising=False)
    sg.reset()
    yield
    sg.reset()


# ── log mode ────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
def test_mode_values(monkeypatch):
    for raw, want in (("enforce", "enforce"), ("log", "log"), ("off", "off"), ("LOG", "log"), ("shadow", "enforce"), ("", "enforce"), (None, "enforce")):
        monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", raw)
        assert sg.mode() == want, raw


def test_log_mode_evaluates_and_records_but_never_refuses(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "log")
    monkeypatch.setattr(sg, "fetch_summary", lambda s: _sum(ms_trend="bearish"))
    reason, snap = sg.check("ABC", 100.0)
    assert reason is None
    doc = json.loads(snap)
    assert doc["gate_mode"] == "log" and doc["would_reject"] is True and doc["enforced"] is False and "bearish" in " ".join(doc["reasons"])
    st = sg.stats()
    assert st["would_reject"] == 1 and st["enforced"] == 0 and st["recent"][0]["enforced"] is False


def test_log_mode_does_not_refuse_no_data_even_when_required(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "log")
    monkeypatch.setattr(config, "STRUCTURE_REQUIRE_DATA", True)
    monkeypatch.setattr(sg, "fetch_summary", lambda s: None)
    reason, snap = sg.check("ABC", 100.0)
    assert reason is None and json.loads(snap)["opinion"] == "no_opinion"


def test_log_mode_capture_still_fills_for_the_observation_row(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "log")
    monkeypatch.setattr(sg, "fetch_summary", lambda s: _sum(ms_trend="bearish"))
    cap: dict = {}
    sg.check("ABC", 100.0, capture=cap)
    assert cap["gate_mode"] == "log" and cap["enforced"] is False and cap["verdict"]["would_reject"] is True


def test_enforce_and_off_are_unchanged(monkeypatch):
    monkeypatch.setattr(sg, "fetch_summary", lambda s: _sum(ms_trend="bearish"))
    assert sg.check("ABC", 100.0)[0].startswith("STRUCTURE_BAD_LOCATION:")
    monkeypatch.setattr(config, "STRUCTURE_GATE_MODE", "off")
    assert sg.check("ABC", 100.0) == (None, None)


def _cfg_value(env: dict, name: str = "STRUCTURE_GATE_MODE") -> str:
    base = {k: v for k, v in os.environ.items() if not k.startswith(("STRUCTURE_", "PS_"))}
    base.update(env)
    base["CONTROL_DIR"] = os.environ.get("TMPDIR", "/tmp")
    r = subprocess.run([sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, '.'); import config; print('V=' + str(config.{name}))"],
                       cwd=str(ROOT), env=base, capture_output=True, text=True, timeout=120)
    line = next((x for x in r.stdout.splitlines() if x.startswith("V=")), None)
    assert line, r.stderr[-600:]
    return line[2:]


def test_config_default_is_enforce_and_only_the_ps_name_can_select_log():
    assert _cfg_value({}) == "enforce"
    assert _cfg_value({"PS_STRUCTURE_GATE_MODE": "log"}) == "log"
    assert _cfg_value({"PS_STRUCTURE_GATE_MODE": "off"}) == "off"
    # Real Trade's shared switch must not loosen the scalper
    assert _cfg_value({"STRUCTURE_GATE_MODE": "log"}) == "enforce"
    assert _cfg_value({"STRUCTURE_GATE_MODE": "off"}) == "off"
    assert _cfg_value({"STRUCTURE_GATE_MODE": "log", "PS_STRUCTURE_GATE_MODE": "enforce"}) == "enforce"


def test_config_breakout_volume_defaults_and_clamp():
    assert _cfg_value({}, "STRUCTURE_BREAKOUT_VOLUME_MODE") == "enforce"                                              # E73: live by default
    assert _cfg_value({"STRUCTURE_BREAKOUT_VOLUME_MODE": "log"}, "STRUCTURE_BREAKOUT_VOLUME_MODE") == "enforce"       # Real Trade's shared name cannot loosen the scalper
    assert _cfg_value({"PS_STRUCTURE_BREAKOUT_VOLUME_MODE": "log"}, "STRUCTURE_BREAKOUT_VOLUME_MODE") == "log"
    assert _cfg_value({"PS_STRUCTURE_BREAKOUT_VOLUME_MODE": "off"}, "STRUCTURE_BREAKOUT_VOLUME_MODE") == "off"
    assert _cfg_value({}, "STRUCTURE_BREAKOUT_MIN_VOL_RATIO") == "1.2"


# ── breakout volume observation ─────────────────────────────────────────────────────────────────────────────────────────────────────
def test_thin_breakout_is_an_observation_not_a_reason():
    v = sg.evaluate(_sum(), 100.0)
    assert v["opinion"] == "ok" and not v["would_reject"] and v["reasons"] == []
    assert len(v["observations"]) == 1 and "thin volume" in v["observations"][0] and "0.60x" in v["observations"][0]


def test_log_volume_mode_never_refuses_and_enforce_does(monkeypatch):
    monkeypatch.setattr(sg, "fetch_summary", lambda s: _sum())
    monkeypatch.setattr(config, "STRUCTURE_BREAKOUT_VOLUME_MODE", "log")
    assert sg.check("ABC", 100.0)[0] is None
    assert not sg.evaluate(_sum(), 100.0)["would_reject"]
    monkeypatch.setattr(config, "STRUCTURE_BREAKOUT_VOLUME_MODE", "enforce")      # E73: live
    v = sg.evaluate(_sum(), 100.0)
    assert v["would_reject"] and v["observations"] == [] and "thin volume" in v["reasons"][0]
    reason, _snap = sg.check("ABC", 100.0)
    assert reason is not None and "thin volume" in reason
    sg.reset_stats()
    sg.check("ABC", 100.0)
    st = sg.stats()
    assert st["thin_volume_breakouts"] == 1 and st["by_reason"].get("volume") == 1


def test_off_and_zero_ratio_switch_it_off(monkeypatch):
    monkeypatch.setattr(config, "STRUCTURE_BREAKOUT_VOLUME_MODE", "off")
    assert sg.evaluate(_sum(), 100.0)["observations"] == []
    monkeypatch.setattr(config, "STRUCTURE_BREAKOUT_VOLUME_MODE", "log")
    monkeypatch.setattr(config, "STRUCTURE_BREAKOUT_MIN_VOL_RATIO", 0.0)
    assert sg.evaluate(_sum(), 100.0)["observations"] == []


@pytest.mark.parametrize("kw", [{"ms_bos_vol_ratio": 1.2}, {"ms_bos_vol_ratio": 2.4}, {"ms_bos_bars": 6}, {"ms_bos_failed": True}, {"ms_bos_dir": "bearish"},
                                {"ms_bos_dir": None}, {"ms_bos_vol_ratio": None}, {"ms_bos_vol_ratio": "junk"}, {"ms_bos_vol_ratio": float("nan")}])
def test_enough_volume_or_no_fresh_bullish_breakout_is_no_observation(kw):
    assert sg.evaluate(_sum(**kw), 100.0)["observations"] == [], kw


def test_summary_without_the_key_is_no_opinion_on_this_test():
    s = _sum()
    del s["ms_bos_vol_ratio"]
    v = sg.evaluate(s, 100.0)
    assert v["observations"] == [] and not v["would_reject"]


def test_no_data_summary_has_no_observation():
    assert sg.evaluate(None, 100.0)["observations"] == []
    assert sg.evaluate(_sum(ms_quality="unavailable"), 100.0)["observations"] == []


def test_snapshot_counter_and_ring_carry_it(monkeypatch):
    monkeypatch.setattr(sg, "fetch_summary", lambda s: _sum())
    reason, snap = sg.check("INFY", 100.0)
    assert reason is None
    doc = json.loads(snap)
    assert len(doc["observations"]) == 1 and doc["summary"]["ms_bos_vol_ratio"] == 0.6
    st = sg.stats()
    assert st["thin_volume_breakouts"] == 1 and st["recent"][0]["observations"] == doc["observations"]
    assert st["config"]["STRUCTURE_BREAKOUT_VOLUME_MODE"] == "log" and st["config"]["STRUCTURE_BREAKOUT_MIN_VOL_RATIO"] == 1.2


def test_a_clean_candidate_counts_nothing():
    sg.record("INFY", sg.evaluate(_sum(ms_bos_vol_ratio=2.0), 100.0), "enforce", False, 100.0)
    assert sg.stats()["thin_volume_breakouts"] == 0


def test_catalog_has_the_new_switches():
    from control.catalog import CATALOG
    by = {f.key: f for f in CATALOG}
    assert by["PS_STRUCTURE_GATE_MODE"].choices == ("enforce", "log", "off") and by["PS_STRUCTURE_GATE_MODE"].shipped == "enforce"
    assert by["PS_STRUCTURE_BREAKOUT_VOLUME_MODE"].choices == ("enforce", "log", "off") and by["PS_STRUCTURE_BREAKOUT_VOLUME_MODE"].shipped == "enforce"
    assert by["PS_STRUCTURE_BREAKOUT_MIN_VOL_RATIO"].shipped == "1.2"
