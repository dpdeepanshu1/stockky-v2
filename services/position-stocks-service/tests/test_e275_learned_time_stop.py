"""E275 round 38: learned time stop (exit_adaptive.learned_time_stop) and its wiring into the stagnation exit."""
import json
import pathlib
from types import SimpleNamespace

import config
from orders import exit_adaptive as ea
from orders import slot_ladder

ROOT = pathlib.Path(__file__).resolve().parents[1]
POS = SimpleNamespace(flow_json=json.dumps({"setup": {"id": "S1_CATALYST_UP", "slot": "0930"}}))


def cell(ttp, ttp_n=30, n=100):
    return {"n": n, "ttp_n": ttp_n, "ttp_min": ttp, "gain_n": 0, "typical_gain_pct": None}


def with_cell(monkeypatch, c):
    monkeypatch.setattr(slot_ladder, "cells", lambda db: {"S1_CATALYST_UP|0930": c} if c else {})


def test_fixed_numbers_without_a_learned_cell(monkeypatch):
    for c in (None, cell(10.0, n=29), cell(10.0, ttp_n=9)):
        with_cell(monkeypatch, c)
        assert ea.learned_time_stop(None, POS) == {"nft_min": 20.0, "stag_min": 30.0, "hard_max_min": 90.0, "source": "fixed"}
    with_cell(monkeypatch, cell(10.0))
    assert ea.learned_time_stop(None, SimpleNamespace(flow_json=None))["source"] == "fixed"          # no setup stamp
    monkeypatch.setattr(config, "TIME_STOP_LEARNED_ENABLED", False, raising=False)
    assert ea.learned_time_stop(None, POS)["source"] == "fixed"


def test_learned_minutes_follow_the_setups_time_to_peak(monkeypatch):
    monkeypatch.setattr(config, "TIME_STOP_LEARNED_ENABLED", True, raising=False)
    with_cell(monkeypatch, cell(10.0))                      # w = 0.5 -> ttp 15; margin max(5, 3.75) = 5 -> nft 20; stag 30; hard 60
    r = ea.learned_time_stop(None, POS)
    assert (r["nft_min"], r["stag_min"], r["hard_max_min"]) == (20.0, 30.0, 60.0) and r["source"].startswith("learned")
    with_cell(monkeypatch, cell(4.0, ttp_n=270))            # w = 0.9 -> ttp 5.6 -> nft 10.6 (floor 10) ...
    r = ea.learned_time_stop(None, POS)
    assert r["nft_min"] == 10.6 and r["stag_min"] == 15.9 and r["hard_max_min"] == 31.8
    with_cell(monkeypatch, cell(120.0, ttp_n=270))          # a slow setup: bounded 40 / 60 / configured hard maximum
    r = ea.learned_time_stop(None, POS)
    assert (r["nft_min"], r["stag_min"], r["hard_max_min"]) == (40.0, 60.0, 90.0)


def test_the_configured_hard_maximum_is_a_ceiling(monkeypatch):
    monkeypatch.setattr(config, "EXIT_HARD_MAX_MINUTES", 50.0, raising=False)
    with_cell(monkeypatch, cell(120.0, ttp_n=270))
    assert ea.learned_time_stop(None, POS)["hard_max_min"] == 50.0
    monkeypatch.setattr(config, "EXIT_HARD_MAX_MINUTES", 10.0, raising=False)
    assert ea.learned_time_stop(None, POS)["hard_max_min"] == 30.0                                     # never under the 30 minute floor


def test_a_failing_read_is_the_fixed_numbers(monkeypatch):
    monkeypatch.setattr(slot_ladder, "cells", lambda db: (_ for _ in ()).throw(RuntimeError("boom")))
    assert ea.learned_time_stop(None, POS)["source"] == "fixed"


def test_stagnation_decision_uses_the_learned_hard_maximum():
    assert ea.stagnation_decision(age_min=45, fading=False, pressure=None)[0] is False
    assert ea.stagnation_decision(age_min=45, fading=False, pressure=None, hard_max_min=40.0) == (True, "hard maximum 45m")
    assert ea.stagnation_decision(age_min=45, fading=False, pressure=None, hard_max_min=None)[0] is False       # old call shape unchanged


def test_stagnation_exit_reads_the_learned_numbers():
    src = (ROOT / "orders" / "eod_squareoff.py").read_text(encoding="utf-8")
    assert "exit_adaptive.learned_time_stop(db, pos)" in src and "stag_age_ok = age_min >= stag_min" in src and 'hard_max_min=_ts["hard_max_min"]' in src
    assert "age_min >= config.STAGNATION_EXIT_MINUTES" not in src
