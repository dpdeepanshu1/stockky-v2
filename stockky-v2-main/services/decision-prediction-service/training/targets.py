"""
Financial target generation for time-series forecasting.
Supports Log Return, Percentage Return, and Directional Classification.
[reference:16]
"""
import numpy as np
import pandas as pd
from typing import Optional, Tuple

class TargetGenerator:
    def __init__(
        self,
        target_type: str = 'Log_Return',
        forecast_horizon: int = 5,
        classification_thresholds: Optional[dict] = None,
        price_col: str = 'close'
    ):
        self.target_type = target_type
        self.forecast_horizon = forecast_horizon
        self.price_col = price_col
        if classification_thresholds is None:
            classification_thresholds = {'buy': 0.015, 'sell': -0.015}
        self.classification_thresholds = classification_thresholds

    def generate(self, data: pd.DataFrame, inplace: bool = False):
        """Generate targets from price data."""
        df = data.copy()
        
        # Percentage Return: (P[t+h] / P[t]) - 1
        df['pct_return'] = df[self.price_col].pct_change(self.forecast_horizon).shift(-self.forecast_horizon)
        
        # Log Return: log(P[t+h] / P[t])
        df['log_return'] = np.log(df[self.price_col].shift(-self.forecast_horizon) / df[self.price_col])
        
        # Directional: BUY (1), HOLD (0), SELL (-1)
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