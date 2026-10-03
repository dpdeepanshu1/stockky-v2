"""
tests/test_walk_forward.py — chronology / purge / embargo guarantees of prediction/pred_walk_forward.py.

This used to `from walk_forward import ...`, the TRAINING service's module name (training/walk_forward.py).
The prediction service ships its own copy as pred_walk_forward.py, so collection failed with
ModuleNotFoundError whenever it ran from this folder. It now imports the module it is meant to test and puts
the prediction dir on sys.path itself, so it works from any cwd.

Run from services/decision-prediction-service/prediction:  python3 -m pytest tests/test_walk_forward.py
"""
from __future__ import annotations

import os
import sys

import numpy as np
import pandas as pd
import pytest

_PRED_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PRED_DIR not in sys.path:
    sys.path.insert(0, _PRED_DIR)

from pred_walk_forward import Fold, WalkForwardSplitter  # noqa: E402


def _df(n=500):
    return pd.DataFrame({"price": np.arange(n)})


def test_split_chronology():
    df = _df()
    splitter = WalkForwardSplitter(train_window=100, val_window=20, step_size=20, embargo_days=5)
    folds = splitter.split(df)
    assert folds, "expected at least one fold"
    for fold in folds:
        assert fold.train_end < fold.embargo_start
        assert fold.embargo_end < fold.val_start
        assert fold.val_end < len(df)


def test_embargo_is_at_least_the_forecast_horizon():
    # an embargo shorter than the label horizon would leak the validation labels into training
    s = WalkForwardSplitter(train_window=100, val_window=20, embargo_days=2, forecast_horizon=5)
    assert s.embargo_days == 5
    assert WalkForwardSplitter(embargo_days=9, forecast_horizon=5).embargo_days == 9
    assert WalkForwardSplitter(forecast_horizon=7).embargo_days == 7   # default embargo = horizon


def test_embargo_gap_between_train_and_validation_matches_setting():
    folds = WalkForwardSplitter(train_window=100, val_window=20, step_size=20, embargo_days=5).split(_df())
    for f in folds:
        assert f.embargo_end - f.embargo_start + 1 == 5
        assert f.val_start - f.train_end - 1 == 5      # exactly the embargo rows sit between them


def test_walk_forward_windows_roll_by_step_size_with_constant_train_length():
    folds = WalkForwardSplitter(train_window=100, val_window=20, step_size=30, embargo_days=5).split(_df())
    assert len(folds) > 1
    assert [b.train_start - a.train_start for a, b in zip(folds, folds[1:])] == [30] * (len(folds) - 1)
    assert {f.train_end - f.train_start + 1 for f in folds} == {100}
    assert {f.val_end - f.val_start + 1 for f in folds} == {20}


def test_expanding_window_keeps_anchor_and_grows_training_set():
    folds = WalkForwardSplitter(train_window=100, val_window=20, step_size=20, embargo_days=5,
                                method="ExpandingWindow").split(_df())
    assert len(folds) > 1
    assert {f.train_start for f in folds} == {0}
    sizes = [f.train_end - f.train_start + 1 for f in folds]
    assert sizes == sorted(sizes) and sizes[0] < sizes[-1]
    for f in folds:
        assert f.train_end < f.embargo_start <= f.embargo_end < f.val_start <= f.val_end < 500


def test_default_step_is_the_validation_window():
    assert WalkForwardSplitter(train_window=100, val_window=20).step_size == 20


def test_too_little_data_raises():
    # needs train + val + embargo rows
    with pytest.raises(ValueError):
        WalkForwardSplitter(train_window=100, val_window=20, embargo_days=5).split(_df(124))
    WalkForwardSplitter(train_window=100, val_window=20, embargo_days=5).split(_df(126))  # enough, no raise


def test_validate_fold_accepts_good_and_rejects_bad_chronology():
    s = WalkForwardSplitter(train_window=100, val_window=20, embargo_days=5)
    good = Fold(train_start=0, train_end=99, val_start=105, val_end=124, embargo_start=100, embargo_end=104)
    assert s.validate_fold(good, 200) is True
    overlapping = Fold(train_start=0, train_end=99, val_start=99, val_end=124, embargo_start=100, embargo_end=104)
    with pytest.raises(ValueError):
        s.validate_fold(overlapping, 200)
    with pytest.raises(ValueError):
        s.validate_fold(good, 124)   # validation window runs past the data


if __name__ == "__main__":
    test_split_chronology()
    print("Chronology test passed.")
