"""
Walk‑forward split with purging and embargo (lightweight, no big memory).
"""
import numpy as np
import pandas as pd
from dataclasses import dataclass
from typing import List, Optional, Dict

@dataclass
class Fold:
    train_start: int
    train_end: int
    val_start: int
    val_end: int
    embargo_start: int
    embargo_end: int

class WalkForwardSplitter:
    def __init__(self, train_window=126, val_window=21, step_size=None,
                 embargo_days=None, forecast_horizon=5, method='WalkForward'):
        self.train_window = train_window
        self.val_window = val_window
        self.step_size = step_size or val_window
        self.forecast_horizon = forecast_horizon
        self.embargo_days = max(embargo_days or forecast_horizon, forecast_horizon)
        self.method = method

    def split(self, data: pd.DataFrame) -> List[Fold]:
        n = len(data)
        if n < self.train_window + self.val_window + self.embargo_days:
            raise ValueError("Not enough data")
        folds = []
        start = 0
        fold_id = 0
        while True:
            train_end = start + self.train_window - 1
            if self.method == 'ExpandingWindow':
                train_end = start + self.train_window - 1 + fold_id * self.step_size
            embargo_start = train_end + 1
            embargo_end = embargo_start + self.embargo_days - 1
            val_start = embargo_end + 1
            val_end = val_start + self.val_window - 1
            if val_end >= n:
                break
            folds.append(Fold(train_start=start, train_end=train_end,
                              val_start=val_start, val_end=val_end,
                              embargo_start=embargo_start, embargo_end=embargo_end))
            if self.method == 'WalkForward':
                start += self.step_size
            else:
                start = 0
            fold_id += 1
        return folds

    def validate_fold(self, fold: Fold, n_samples: int):
        if not (fold.train_end < fold.embargo_start < fold.val_start < fold.val_end < n_samples):
            raise ValueError("Invalid fold chronology")
        return True