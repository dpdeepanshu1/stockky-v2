"""E440 (round 422): `_board_rank_key` counts a NaN / inf horizon score or combined score as 0, like `_select_top_picks` (E395)."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import pytest  # noqa: E402

from pick_ranking import _board_rank_key as key, _value_adjusted_score  # noqa: E402

NAN, INF = float("nan"), float("inf")


@pytest.mark.parametrize("bad", [NAN, INF, float("-inf"), "nan", "inf", None, "x"])
def test_non_finite_or_junk_horizon_score_counts_as_zero(bad):
    assert key({"_hz_score": bad}) == 0


@pytest.mark.parametrize("bad", [NAN, INF, float("-inf"), "n/a", None])
def test_non_finite_combined_score_gives_a_finite_key(bad):
    k = key({"_hz_score": 60, "combined_score": bad, "close": 100, "fundamental_score": 60})
    assert k == pytest.approx(60 + (1 - 100 / 2000) * 8)


def test_the_bonus_matches_the_verdict_ranking_for_a_normal_row():
    row = {"_hz_score": 60, "combined_score": 70, "close": 200, "fundamental_score": 60}
    assert key(row) == pytest.approx(60 + _value_adjusted_score(row)[0] - 70)


def test_a_nan_score_row_sorts_last_not_randomly():
    rows = [{"_hz_score": NAN, "s": "n"}, {"_hz_score": 50, "s": "a"}, {"_hz_score": 60, "s": "b"}]
    assert [r["s"] for r in sorted(rows, key=key, reverse=True)] == ["b", "a", "n"]
