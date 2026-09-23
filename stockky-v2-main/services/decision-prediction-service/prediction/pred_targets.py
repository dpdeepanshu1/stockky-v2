"""
Lightweight target generation for financial time series.
"""
import numpy as np
import pandas as pd
from typing import Optional, Union, Tuple

class TargetGenerator:
    def __init__(self, target_type='Log_Return', forecast_horizon=5,
                 classification_thresholds=None, price_col='close'):
        self.target_type = target_type
        self.forecast_horizon = forecast_horizon
        self.price_col = price_col
        if classification_thresholds is None:
            classification_thresholds = {'buy': 0.015, 'sell': -0.015}
        self.classification_thresholds = classification_thresholds

    def generate(self, data: pd.DataFrame, inplace=False):
        df = data.copy()
        # Compute percentage return (future / current - 1)
        df['pct_return'] = df[self.price_col].pct_change(self.forecast_horizon).shift(-self.forecast_horizon)
        df['log_return'] = np.log(df[self.price_col].shift(-self.forecast_horizon) / df[self.price_col])
        # Directional
        df['directional'] = 0
        df.loc[df['pct_return'] > self.classification_thresholds['buy'], 'directional'] = 1
        df.loc[df['pct_return'] < self.classification_thresholds['sell'], 'directional'] = -1

        if self.target_type == 'Log_Return':
            target = df['log_return']
        elif self.target_type == 'Percentage_Return':
            target = df['pct_return']
        elif self.target_type == 'Directional':
            target = df['directional']
        else:
            raise ValueError(f"Unsupported target type: {self.target_type}")
        if inplace:
            df['target'] = target
            return df
        return target, df

    def validate(self, data):
        if data.empty:
            raise ValueError("Data is empty")
        if self.price_col not in data.columns:
            raise ValueError(f"Column {self.price_col} not found")
        if len(data) < self.forecast_horizon + 1:
            raise ValueError("Insufficient rows")
        return True