"""group362: A/B parity comparison between the legacy board maths and the new engine adapter (observational; what is served never changes)."""
from __future__ import annotations

import copy
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from screening import ms_engine_switch as sw
from tests.test_group349_compat_board import _walk

_ORIG = {}


def setup_function(_):
    for k in ("BOARD_USE_MS_ENGINE", "BOARD_AB_ENABLED", "BOARD_AB_MAX_COMPARES"):
        _ORIG.setdefault(k, getattr(config, k, None))
    sw.reset_stats()
    sw.reset_ab()
    config.BOARD_USE_MS_ENGINE = True
    config.BOARD_AB_ENABLED = True
    config.BOARD_AB_MAX_COMPARES = 100000


def teardown_function(_):
    for k, v in _ORIG.items():          # put the process-wide flags back exactly as found
        setattr(config, k, v)


def _z(idx, side="bull", status="fresh", top=101.0, bottom=100.0, **kw):
    z = {"side": side, "top": top, "bottom": bottom, "t": 1000 + idx, "idx": idx, "status": status, "bos": True, "fvg": False, "valid": True,
         "impulse": 1.2, "atr": 0.5}
    z.update(kw)
    return z


def _s(idx, kind="high", price=100.0):
    return {"idx": idx, "t": 1000 + idx, "kind": kind, "price": price}


# ---------------------------------------------------------------- compare (pure)
def test_compare_identical_and_float_noise_is_not_a_difference():
    a = [_z(3), _z(7, "bear", top=99.0, bottom=98.0)]
    b = copy.deepcopy(a)
    b[0]["top"] += 1e-12
    r = sw.compare(a, b, [_s(1), _s(2, "low", 99.0)], [_s(1), _s(2, "low", 99.0)])
    assert r["identical"] is True and not any(r[k] for k in ("dropped", "added", "status", "bounds", "other")) and r["swings_differ"] is False


def test_compare_names_every_kind_of_difference():
    legacy = [_z(1), _z(2), _z(3), _z(4), _z(5)]
    engine = [_z(2, status="tested"), _z(3, top=105.0), _z(4, bos=False), _z(5), _z(9)]
    r = sw.compare(legacy, engine, [_s(1)], [_s(1)])
    assert r["identical"] is False
    assert r["dropped"] == [("bull", 1)] and r["added"] == [("bull", 9)] and r["status"] == [("bull", 2)]
    assert r["bounds"] == [("bull", 3)] and r["other"] == [("bull", 4)] and r["swings_differ"] is False


def test_compare_same_idx_other_side_is_a_different_zone_and_swings_are_checked():
    r = sw.compare([_z(3, "bull")], [_z(3, "bear")], [_s(1)], [_s(1)])
    assert r["dropped"] == [("bull", 3)] and r["added"] == [("bear", 3)]
    assert sw.compare([], [], [_s(1)], [])["swings_differ"] is True
    assert sw.compare([], [], [_s(1, "high")], [_s(1, "low")])["swings_differ"] is True
    assert sw.compare([], [], [_s(1, price=100.0)], [_s(1, price=100.5)])["swings_differ"] is True
    assert sw.compare([], [], [], [])["identical"] is True


# ---------------------------------------------------------------- the switch
def test_engine_output_is_served_unchanged_and_the_comparison_is_counted():
    b = _walk(3)
    expect = (sw._cb.find_zones(b, atr_mult=1.5, full_range=False), sw._cb.swings(b))
    out = sw.zones_and_swings(b, 1.5, False, symbol="TCS")
    assert out == expect
    ab = sw.ab_report()
    assert ab["compared"] == 1 and ab["errors"] == 0 and ab["identical"] + ab["zones_differ"] + ab["swings_differ"] >= 1
    assert sw.stats()["engine_calls"] == 1 and sw.stats()["legacy_calls"] == 0 and sw.stats()["ab"]["compared"] == 1


def test_real_engine_vs_legacy_on_synthetic_days_never_adds_a_zone_and_swings_agree():
    for seed in range(40):
        sw.zones_and_swings(_walk(seed), 1.5, False, symbol=f"S{seed}")
        sw.zones_and_swings(_walk(seed), 1.0, True, symbol=f"S{seed}")
    ab = sw.ab_report()
    assert ab["compared"] == 80 and ab["errors"] == 0
    assert ab["engine_added"] == 0 and ab["swings_differ"] == 0           # the documented contract
    assert ab["bounds_changed"] == 0                                      # same bounds mode on both sides
    assert ab["cap_knockon"] <= ab["served_differs"] <= ab["compared"]
    assert ab["zones_differ"] == ab["compared"] - ab["identical"]          # every non-identical comparison is a zone difference here


def test_cap_knockon_is_not_a_contract_break():
    """Legacy has 7 zones 1..7, the engine dropped 7 (a bar closed through it): the engine's newest 6 are 1..6, legacy's are 2..7. The board user
    sees a different zone set, but nothing was ADDED before the cap... except the older zone 1 that slid into the engine's window."""
    zs = [_z(i) for i in range(1, 8)]

    class FakeMs:
        @staticmethod
        def find_zones(bars, atr_mult=1.5, max_zones=6, full_range=False):
            return copy.deepcopy(zs)[-max_zones:]

        swings = staticmethod(lambda bars: [])

    class FakeCb:
        @staticmethod
        def find_zones(bars, atr_mult=1.5, max_zones=6, full_range=False):
            return copy.deepcopy(zs[:-1])[-max_zones:]

        swings = staticmethod(lambda bars: [])

    real_ms, real_cb = sw.ms, sw._cb
    sw.ms, sw._cb = FakeMs, FakeCb
    try:
        served = (FakeCb.find_zones(None), [])
        assert [z["idx"] for z in served[0]] == [1, 2, 3, 4, 5, 6]
        sw._ab_compare("X", [(1_790_000_000, 1, 2, 0.5, 1.5, 1)], 1.5, False, served)
    finally:
        sw.ms, sw._cb = real_ms, real_cb
    ab = sw.ab_report()
    assert ab["compared"] == 1 and ab["engine_dropped"] == 1 and ab["engine_added"] == 0 and ab["served_differs"] == 1 and ab["cap_knockon"] == 1
    assert ab["samples"][0]["cap_knockon"] is True and ab["samples"][0]["dropped"] == [["bull", 7]] and ab["samples"][0]["added"] == []


def test_comparison_does_not_mutate_what_is_served():
    b = _walk(5)
    first = sw.zones_and_swings(b, 1.5, False)
    snap = copy.deepcopy(first)
    second = sw.zones_and_swings(b, 1.5, False)
    assert first == snap and second == snap


def test_off_flag_and_legacy_path_run_no_comparison():
    config.BOARD_AB_ENABLED = False
    sw.zones_and_swings(_walk(1), 1.5, False)
    assert sw.ab_report()["compared"] == 0 and sw.ab_report()["verdict"] == "disabled" and sw.stats()["engine_calls"] == 1
    config.BOARD_AB_ENABLED = True
    config.BOARD_USE_MS_ENGINE = False
    sw.zones_and_swings(_walk(1), 1.5, False)
    assert sw.ab_report()["compared"] == 0 and sw.stats()["legacy_calls"] == 1


def test_cap_stops_further_comparisons():
    config.BOARD_AB_MAX_COMPARES = 3
    for seed in range(6):
        sw.zones_and_swings(_walk(seed), 1.5, False)
    assert sw.ab_report()["compared"] == 3 and sw.stats()["engine_calls"] == 6
    config.BOARD_AB_MAX_COMPARES = 0                                        # 0 = no cap
    sw.zones_and_swings(_walk(9), 1.5, False)
    assert sw.ab_report()["compared"] == 4


def test_legacy_failure_inside_the_comparison_is_counted_and_never_reaches_the_board():
    class Boom:
        @staticmethod
        def find_zones(*a, **k):
            raise ValueError("legacy exploded")

        swings = staticmethod(lambda *a, **k: [])

    real = sw.ms
    sw.ms = Boom
    try:
        out = sw.zones_and_swings(_walk(2), 1.5, False)
    finally:
        sw.ms = real
    assert out == (sw._cb.find_zones(_walk(2), atr_mult=1.5, full_range=False), sw._cb.swings(_walk(2)))
    ab = sw.ab_report()
    assert ab["errors"] == 1 and "legacy exploded" in ab["last_error"] and ab["compared"] == 0 and sw.stats()["fallbacks"] == 0


def test_engine_failure_still_falls_back_to_legacy_without_a_comparison():
    real = sw._cb

    class Boom:
        @staticmethod
        def find_zones(*a, **k):
            raise RuntimeError("engine down")

    sw._cb = Boom
    try:
        out = sw.zones_and_swings(_walk(4), 1.5, False)
    finally:
        sw._cb = real
    assert out[0] == sw.ms.find_zones(_walk(4), atr_mult=1.5, full_range=False)
    assert sw.stats()["fallbacks"] == 1 and sw.ab_report()["compared"] == 0


# ---------------------------------------------------------------- report / verdict
def _fill(**kw):
    sw.reset_ab()
    with sw._ab_lock:
        sw._ab.update(kw)


def test_verdicts():
    assert sw.ab_report()["verdict"] == "insufficient_sample"
    _fill(compared=500, identical=480, zones_differ=20, status_changed=20, engine_dropped=3)
    r = sw.ab_report()
    assert r["verdict"] == "parity_ok" and r["pct"]["identical"] == 96.0 and r["pct"]["status_changed"] == 4.0 and r["pct"]["engine_added"] == 0.0
    _fill(compared=500, identical=499, zones_differ=1, engine_added=1)
    assert sw.ab_report()["verdict"] == "contract_break"
    _fill(compared=500, identical=499, swings_differ=1)
    assert sw.ab_report()["verdict"] == "contract_break"
    _fill(compared=500, identical=500, errors=2)
    assert sw.ab_report()["verdict"] == "contract_break"
    config.BOARD_AB_ENABLED = False
    assert sw.ab_report()["verdict"] == "disabled"


def test_samples_are_newest_first_bounded_and_day_stats_kept():
    for i in range(70):
        res = sw.compare([_z(i)], [_z(i, status="tested")], [], [])
        sw._record_ab(f"S{i}", [(1_790_000_000 + i, 1, 2, 0.5, 1.5, 1)], res, 1, 1)
    r = sw.ab_report()
    assert len(r["samples"]) == 50 and r["samples"][0]["symbol"] == "S69" and r["samples"][-1]["symbol"] == "S20"
    assert r["samples"][0]["status"] == [["bull", 69]] and sum(d["compared"] for d in r["days"].values()) == 70
    assert r["status_changed"] == 70 and r["zones_differ"] == 70 and r["identical"] == 0


def test_status_block_and_wiring():
    assert "ab" in sw.stats() and sw.stats()["ab"]["verdict"] == "insufficient_sample"
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "screening", "board.py")).read()
    assert "zones_and_swings(bars, config.BOARD_ZONE_ATR_MULT, config.BOARD_ZONE_FULL_RANGE, symbol=symbol)" in src
    assert isinstance(config.BOARD_AB_ENABLED, bool) and isinstance(config.BOARD_AB_MAX_COMPARES, int)
