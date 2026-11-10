"""E319: an unknown (NaN) input scores 0, never 100. clamp(nan) used to return nan and `max(0, min(100, nan))` is 100, so a NaN location score, sweep strength or ATR read as a perfect score."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from market_structure import location, scoring  # noqa: E402
from market_structure.ms_types import clamp  # noqa: E402
from market_structure.params import MSParams  # noqa: E402

NAN, INF = float("nan"), float("inf")
P = MSParams()


def _tqs(**kw):
    a = dict(trend="bullish", bos=None, choch=None, sweep=None, ob=None, fvg=None, location=None, rr=None)
    a.update(kw)
    return scoring.score_block(a["trend"], a["bos"], a["choch"], a["sweep"], a["ob"], a["fvg"], a["location"], a["rr"], "ok", P)


def test_clamp_reads_nan_as_the_lower_bound_and_is_unchanged_otherwise():
    assert clamp(NAN) == 0.0 and clamp(NAN, 2.0, 3.0) == 2.0
    assert (clamp(-1), clamp(0.4), clamp(7)) == (0.0, 0.4, 1.0) and clamp(INF) == 1.0 and clamp(-INF) == 0.0


def test_a_nan_group_input_scores_like_a_missing_one():
    base = _tqs()
    for kw in (dict(location={"entry_location_score": NAN}), dict(location={"entry_location_score": INF * 0}),
               dict(sweep={"direction": "bullish", "bars_since": 2, "strength": NAN}), dict(bos={"direction": "bullish", "strength": NAN})):
        r = _tqs(**kw)
        assert r["technical_quality_score"] < 60, (kw, r["technical_quality_score"])          # was 100
    assert _tqs(location={"entry_location_score": NAN})["technical_quality_score"] == _tqs(location={"entry_location_score": None})["technical_quality_score"]
    assert base["technical_quality_score"] is not None


def test_a_sweep_with_a_nan_age_is_not_a_fresh_sweep():
    assert scoring.liquidity_group({"direction": "bullish", "bars_since": NAN, "strength": 1.0}, P) == 0.0
    assert scoring.liquidity_group({"direction": "bullish", "bars_since": 2, "strength": 1.0}, P) == 1.0


def test_location_with_a_nan_atr_is_unknown_not_scored():
    out = location.location_block(100.0, NAN, None, None, {}, {}, None, {}, P)
    assert out["entry_location_score"] is None and out["flags"] == []
    ok = location.location_block(100.0, 2.0, None, None, {}, {}, None, {}, P)
    assert isinstance(ok["entry_location_score"], int)
