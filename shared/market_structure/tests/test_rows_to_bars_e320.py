"""E320: rows_to_bars drops a bar whose open or close is outside [low, high] (beyond the engine's tolerance), like prep._valid does."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from market_structure.params import MSParams  # noqa: E402
from market_structure.prep import _valid  # noqa: E402
from market_structure.timeframes import rows_to_bars  # noqa: E402


def _row(minute, o, h, l, c):
    return {"date": f"2026-11-09 09:{minute:02d}", "open": o, "high": h, "low": l, "close": c, "volume": 10}


def test_a_close_or_open_outside_the_range_is_dropped_and_good_rows_stay():
    rows = [_row(15, 100, 101, 99, 100.5), _row(16, 100.5, 101, 100, 140.0), _row(17, 60.0, 101, 100, 100.5), _row(18, 100.5, 101, 100, 100.8)]
    assert [b[4] for b in rows_to_bars(rows)] == [100.5, 100.8]


def test_a_missing_open_is_still_the_close_and_a_hair_outside_is_within_tolerance():
    out = rows_to_bars([_row(15, None, 101, 100, 100.5), _row(16, 100.5, 101.0, 100.0, 101.004)])
    assert len(out) == 2 and out[0][1] == 100.5


def test_it_agrees_with_the_engines_own_validity_rule():
    p = MSParams()
    cases = [(100, 101, 99, 100.5), (100.5, 101, 100, 140.0), (60.0, 101, 100, 100.5), (100.5, 101.0, 100.0, 101.004), (100.5, 101.0, 100.0, 101.2), (100.5, 101, 100, 99.9)]
    for k, (o, h, l, c) in enumerate(cases):
        kept = bool(rows_to_bars([_row(15 + k, o, h, l, c)]))
        assert kept == _valid((0.0, o, h, l, c, 1.0), p.bar_tolerance_pct), (o, h, l, c)


def test_structure_rr_with_a_nan_atr_is_invalid_not_computed():
    from market_structure.location import structure_rr
    out = structure_rr(100.0, float("nan"), None, [], None, {}, {}, {}, MSParams())
    assert out["flags"] == ["invalid"] and out["rr"] is None


def test_assert_comparable_survives_a_row_without_meta():
    from market_structure.ml_features import assert_comparable
    good = {"meta": {"engine_version": "1", "params_hash": "a", "columns_hash": "c"}}
    assert assert_comparable([good, good]) and not assert_comparable([good, {"meta": None}]) and not assert_comparable([good, {}])
