"""group365: (1) the legacy Market-Board maths is frozen by a golden file (it is the fallback when the engine is off or errors);
(2) the engine adapter is compared with that same frozen legacy output on the same 41 series; (3) PS_* env overrides for the shared STRUCTURE_* names."""
from __future__ import annotations

import importlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SVC = os.path.dirname(HERE)
ROOT = os.path.dirname(os.path.dirname(SVC))
sys.path.insert(0, SVC)
sys.path.insert(0, os.path.join(ROOT, "scripts"))

import gen_ms_golden as gen  # noqa: E402
from screening import market_structure as ms  # noqa: E402
from stockky_ms import compat_board as cb  # noqa: E402

GOLDEN = os.path.join(HERE, "golden", "market_structure_v0.json")


def test_legacy_board_output_equals_the_frozen_golden():
    want = json.load(open(GOLDEN, encoding="utf-8"))
    got = json.loads(json.dumps({"schema": 1, "n_series": len(gen.series()), "series": gen.snapshot()}, sort_keys=True))
    assert want["schema"] == 1 and want["n_series"] == got["n_series"] == 41
    for name in want["series"]:
        assert got["series"][name] == want["series"][name], f"legacy board maths drifted on series {name}"


def test_golden_is_not_empty_and_covers_edge_series():
    want = json.load(open(GOLDEN, encoding="utf-8"))["series"]
    assert sum(1 for v in want.values() if v["zones"]) >= 25
    assert sum(1 for v in want.values() if v["swings"]) >= 25
    for k in ("short0", "short1", "short4", "flat", "zero_volume", "plateau0", "gap0"):
        assert k in want
    assert want["short0"]["zones"] == [] and want["short0"]["atr"] is None


def test_engine_adapter_vs_frozen_legacy_on_the_same_series():
    """Swings and ATR identical; the adapter never invents a zone; where it differs it is only the documented one-bar rule."""
    compared = same = 0
    for name, bars in gen.series().items():
        if len(bars) < 30:
            continue
        assert cb.swings(bars) == ms.swings(bars), name
        assert cb.legacy_atr(bars) == ms.atr(bars), name
        for full in (False, True):
            old = {z["idx"]: z for z in ms.find_zones(bars, full_range=full, max_zones=-1)}
            new = {z["idx"]: z for z in cb.find_zones(bars, full_range=full, max_zones=-1)}
            assert set(new) <= set(old), (name, full, "engine invented a zone")
            compared += 1
            same += int(set(new) == set(old) and all(new[k]["status"] == old[k]["status"] for k in new))
            for k, z in new.items():
                if z["status"] != old[k]["status"]:
                    assert (old[k]["status"], z["status"]) == ("fresh", "tested"), (name, k)
    assert compared >= 60 and same / compared > 0.5


def _reload_config(env):
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import config
        from types import SimpleNamespace
        return SimpleNamespace(**{k: v for k, v in vars(importlib.reload(config)).items() if k.isupper()})
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import config
        importlib.reload(config)


def test_ps_prefix_wins_over_the_shared_generic_name():
    c = _reload_config({"STRUCTURE_MAX_CHASE_ATR": "1.5", "PS_STRUCTURE_MAX_CHASE_ATR": "4.5", "STRUCTURE_GATE_MODE": "shadow", "PS_STRUCTURE_GATE_MODE": "enforce"})
    assert c.STRUCTURE_MAX_CHASE_ATR == 4.5 and c.STRUCTURE_GATE_MODE == "enforce"


def test_generic_name_still_works_without_a_ps_override_and_blank_ps_is_ignored():
    c = _reload_config({"STRUCTURE_MAX_STRETCH_ATR": "6.5", "PS_STRUCTURE_MAX_STRETCH_ATR": "   ", "STRUCTURE_MIN_ROOM_ATR": None, "PS_STRUCTURE_MIN_ROOM_ATR": None})
    assert c.STRUCTURE_MAX_STRETCH_ATR == 6.5 and c.STRUCTURE_MIN_ROOM_ATR == 0.3


def test_defaults_are_live_and_edge_floor_is_100():
    c = _reload_config({k: None for k in ("STRUCTURE_GATE_MODE", "PS_STRUCTURE_GATE_MODE", "STRUCTURE_EDGE_MIN_N", "BOARD_USE_MS_ENGINE", "BOARD_GATE_MODE")})
    assert c.STRUCTURE_GATE_MODE == "enforce" and c.STRUCTURE_EDGE_MIN_N == 100 and c.BOARD_USE_MS_ENGINE is True and c.BOARD_GATE_MODE == "enforce"
    c2 = _reload_config({"STRUCTURE_EDGE_MIN_N": "250"})
    assert c2.STRUCTURE_EDGE_MIN_N == 250
