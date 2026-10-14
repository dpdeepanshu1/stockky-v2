"""WS-0 golden baseline of the daily structure gate verdict (position-stocks-service). Fixed summaries x live prices -> opinion / would_reject / reasons / live
metrics. Any threshold or wording change fails here until the golden file is regenerated on purpose:
    python3 tests/test_group356_structure_gate_golden.py --update      (from the service dir)
Thresholds are pinned below so an env override cannot change the baseline."""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import config
from orders import structure_gate as sg

GOLDEN = os.path.join(HERE, "golden", "structure_gate_golden.json")
PINNED = {"STRUCTURE_MAX_CHASE_ATR": 1.5, "STRUCTURE_MAX_STRETCH_ATR": 3.0, "STRUCTURE_MIN_ROOM_ATR": 0.5, "STRUCTURE_MIN_RR": 1.2,
          "STRUCTURE_REJECT_STOP_WIDE": True, "STRUCTURE_BEARISH_BLOCK": True, "STRUCTURE_BEARISH_BOS_BARS": 5, "STRUCTURE_MAX_AGE_DAYS": 7.0,
          "STRUCTURE_MAX_PRICE_DEV_PCT": 15.0}
BASE = {"ms_quality": "ok", "ms_price": 100.0, "ms_atr": 2.0, "ms_trend": "bullish", "ms_chase_atr": 0.4, "ms_stretch_atr": 1.0, "ms_room_atr": 3.0,
        "ms_stop": 95.0, "ms_target": 112.0, "ms_rr_flags": []}
CASES = {
    "clean": ({}, 100.0),
    "chase": ({"ms_chase_atr": 2.5}, 100.0),
    "stretch": ({"ms_stretch_atr": 3.6}, 100.0),
    "no_room": ({"ms_room_atr": 0.2}, 100.0),
    "poor_rr": ({"ms_stop": 90.0, "ms_target": 104.0}, 100.0),
    "stop_wide": ({"ms_rr_flags": ["stop_too_wide"]}, 100.0),
    "bearish_trend": ({"ms_trend": "bearish"}, 100.0),
    "fresh_bearish_bos": ({"ms_bos_dir": "bearish", "ms_bos_bars": 2, "ms_bos_failed": False}, 100.0),
    "old_bearish_bos": ({"ms_bos_dir": "bearish", "ms_bos_bars": 9, "ms_bos_failed": False}, 100.0),
    "live_moved_up": ({}, 103.0),
    "price_far_from_close": ({}, 140.0),
    "insufficient": ({"ms_quality": "insufficient"}, 100.0),
    "no_atr": ({"ms_atr": None}, 100.0),
    "no_price": ({}, None),
}


def _apply_pins():
    old = {k: getattr(config, k, None) for k in PINNED}
    for k, v in PINNED.items():
        setattr(config, k, v)
    return old


def _run():
    old = _apply_pins()
    try:
        out = {}
        for name, (patch, px) in CASES.items():
            s = {**BASE, "ms_asof": datetime.now(timezone.utc).isoformat(), **patch}
            v = sg.evaluate(s, px)
            out[name] = {"opinion": v["opinion"], "would_reject": v["would_reject"], "reasons": v["reasons"], "metrics": v.get("metrics") or {},
                         "no_data": v.get("no_data")}
        return out
    finally:
        for k, v in old.items():
            if v is None:
                try:
                    delattr(config, k)
                except AttributeError:
                    pass
            else:
                setattr(config, k, v)


def test_gate_verdicts_match_the_golden_baseline():
    got = json.loads(json.dumps(_run()))
    want = json.load(open(GOLDEN))
    assert got == want


def test_golden_cases_cover_both_outcomes():
    want = json.load(open(GOLDEN))
    assert want["clean"]["would_reject"] is False and want["clean"]["opinion"] == "ok"
    assert want["bearish_trend"]["would_reject"] is True and want["insufficient"]["opinion"] == "no_opinion"


if __name__ == "__main__":
    if "--update" in sys.argv:
        os.makedirs(os.path.dirname(GOLDEN), exist_ok=True)
        json.dump(json.loads(json.dumps(_run())), open(GOLDEN, "w"), indent=1, sort_keys=True)
        print("golden written:", GOLDEN)
    else:
        test_gate_verdicts_match_the_golden_baseline()
        test_golden_cases_cover_both_outcomes()
        print("ok")
