"""group424: the structure R:R floor (P4_RR, ratio part only) is OFF by default; its replay edge flipped sign between seeds (+0.39 / -0.19).
The structural-target and stop-width parts of P4_RR stay live, and the old floor comes back with rr_min=1.2."""
from __future__ import annotations

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import structure_policy as sp  # noqa: E402


def _sum(**kw):
    d = {"ms_quality": "ok", "ms_trend": "bullish", "ms_price": 100.0, "ms_atr": 2.0, "ms_chase_atr": 0.2, "ms_stretch_atr": 1.0,
         "ms_room_atr": 3.0, "ms_rr": 2.0, "ms_stop": 96.0, "ms_target": 108.0, "ms_rr_flags": [], "ms_tqs": 70, "ms_groups": 3, "ms_loc_score": 70,
         "ms_bos_dir": "bullish", "ms_bos_bars": 2, "ms_bos_failed": False}
    d.update(kw)
    return d


def _ids(r):
    return [x["id"] for x in r["structure"]["rules"]]


def test_default_floor_is_off():
    assert sp.DEFAULTS["rr_min"] == 0.0
    # reward 3% above price (passes P4_REWARD), stop 3% below -> R:R about 1.0, which the old 1.2 floor rejected
    r = sp.apply_policy("BUY NOW", _sum(ms_rr=1.0, ms_target=103.0, ms_stop=97.0), 100.0)
    assert "P4_RR" not in _ids(r) and r["decision"] == "BUY NOW"


def test_old_floor_comes_back_with_the_param(monkeypatch):
    monkeypatch.setenv("STRUCTURE_POLICY_PARAMS_JSON", json.dumps({"rr_min": 1.2}))
    r = sp.apply_policy("BUY NOW", _sum(ms_rr=1.0, ms_target=103.0, ms_stop=97.0), 100.0)
    assert "P4_RR" in _ids(r) and r["decision"] == "WAIT"


def test_no_target_and_wide_stop_still_fire_with_the_floor_off():
    r = sp.apply_policy("BUY NOW", _sum(ms_rr_flags=["no_target"]), 100.0)
    assert "P4_RR" in _ids(r) and r["decision"] == "WAIT"
    r = sp.apply_policy("BUY NOW", _sum(ms_rr_flags=["stop_too_wide"]), 100.0)
    assert "P4_RR" in _ids(r) and r["decision"] == "WAIT"


def test_chase_still_live_next_to_the_off_floor():
    r = sp.apply_policy("BUY NOW", _sum(ms_chase_atr=2.4, ms_rr=0.5), 100.0)
    assert _ids(r) == ["P2_CHASE"] and r["decision"] == "WAIT"
