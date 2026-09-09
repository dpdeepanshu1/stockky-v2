"""
Walk-forward validation with purging and embargo for time-series data.
[reference:17]
"""
from dataclasses import dataclass
from typing import List, Optional
import pandas as pd

@dataclass
class Fold:
    train_start: int
    train_end: int
    val_start: int
    val_end: int
    embargo_start: int
    embargo_end: int

class WalkForwardSplitter:
    def __init__(
        self,
        train_window: int = 252,
        val_window: int = 63,
        step_size: Optional[int] = None,
        embargo_days: Optional[int] = None,
        forecast_horizon: int = 5,
        method: str = 'WalkForward'
    ):
        self.train_window = train_window
        self.val_window = val_window
        self.step_size = step_size or val_window
        self.forecast_horizon = forecast_horizon
        # Embargo must be >= forecast_horizon to prevent overlap
        self.embargo_days = max(embargo_days or forecast_horizon, forecast_horizon)
        self.method = method

    def split(self, data: pd.DataFrame) -> List[Fold]:
        """Generate chronological train/validation splits with embargo."""
        n = len(data)
        if n < self.train_window + self.val_window + self.embargo_days:
            raise ValueError("Insufficient data for walk-forward validation")

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

            folds.append(Fold(
                train_start=start,
                train_end=train_end,
                val_start=val_start,
                val_end=val_end,
                embargo_start=embargo_start,
                embargo_end=embargo_end
            ))

            if self.method == 'WalkForward':
                start += self.step_size
            else:
                start = 0
            fold_id += 1

        return folds