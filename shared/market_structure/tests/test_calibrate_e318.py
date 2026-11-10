"""E318: forward_outcome survives bars without a finite high / low; walk_forward `stable` needs two folds big enough to count; a split fold vote does not move the threshold."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from market_structure import backtest as B  # noqa: E402
from market_structure import observation as O  # noqa: E402

NAN = float("nan")


def test_forward_outcome_with_no_finite_high_or_low_does_not_raise():
    bars = [[i, 100.0, NAN, NAN, 101.0, 0.0] for i in range(1, 4)]             # T O H L C V
    out = B.forward_outcome(100.0, 95.0, 110.0, bars)
    assert out["bars_seen"] == 3 and out["mfe_pct"] is None and out["mae_pct"] is None and out["first_touch"] is None
    assert out["ret_1d"] == 1.0                                                  # the closes are fine and still count


def test_forward_outcome_with_one_good_bar_still_reports_mfe_mae():
    bars = [[1, 100.0, NAN, NAN, 100.0, 0.0], [2, 100.0, 104.0, 98.0, 101.0, 0.0]]
    out = B.forward_outcome(100.0, None, None, bars)
    assert out["mfe_pct"] == 4.0 and out["mae_pct"] == -2.0


def _samples(blocks):
    """blocks: list of (n_rejected, n_passed, level); rejected does worse by 1.0 inside each block (edge +1)."""
    out, ts = [], 0
    for nrej, nok, lvl in blocks:
        for i in range(nrej + nok):
            ts += 1
            rej = i < nrej
            out.append({"ts": ts, "summary": {"flag": rej}, "outcome": {"ret_10d": lvl - (1.0 if rej else 0.0)}})
    return out


def test_two_tiny_folds_are_not_stable(monkeypatch):
    monkeypatch.setattr(B, "rule_flags", lambda summary, th=None: {**{r: False for r in B.RULES}, "chase": bool(summary["flag"])})
    # block 0 trains (plenty of rejections); folds 1-2 have ONE rejected sample each (< min_n) with a positive edge; folds 3-4 have enough rejections
    blocks = [(30, 30, 0.0), (1, 59, 0.0), (1, 59, 0.0), (30, 30, 0.0), (30, 30, 0.0)]
    out = B.walk_forward(_samples(blocks), "chase", [1.0], folds=4, horizon=10, min_train=10, min_n=10)
    big = [f for f in out["folds"] if f["test_rejected_n"] >= 10]
    assert len(big) == 2 and out["stable"] is True                              # two real folds: fine

    blocks = [(30, 30, 0.0), (1, 59, 0.0), (1, 59, 0.0), (1, 59, 0.0), (30, 30, 0.0)]
    out = B.walk_forward(_samples(blocks), "chase", [1.0], folds=4, horizon=10, min_train=10, min_n=10)
    assert len([f for f in out["folds"] if f["test_rejected_n"] >= 10]) == 1 and out["stable"] is False   # one real fold + tiny ones: no longer "stable"


def test_a_split_vote_keeps_the_shipped_threshold_or_the_closest_tied_one():
    assert O._vote([1.5, 1.5, 2.0, 2.0], 1.5) == 1.5                            # tie that includes the current one: no change
    assert O._vote([2.0, 2.0, 1.0, 1.0], 1.5) == 1.0                            # tie of two others: the tie-break is deterministic (equal distance -> smaller)
    assert O._vote([2.5, 2.5, 2.0, 2.0], 1.5) == 2.0                            # closer to the shipped value
    assert O._vote([2.0, 2.0, 2.0, 1.0], 1.5) == 2.0                            # a clear majority still wins
